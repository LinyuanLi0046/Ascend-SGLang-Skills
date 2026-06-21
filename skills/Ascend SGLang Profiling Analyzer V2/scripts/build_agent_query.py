from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from agent_contracts import AGENT_CONFIG, effective_agent_config, resolve_workspace_paths
from write_agent_task_input import TASK_FILENAME_BY_AGENT, build_payload as build_task_input_payload
from workflow_common import AGENT_NAMES, compute_sha256, ensure_parent, load_state, now_iso, read_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成子 agent query 并记录审计日志。")
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument("--agent-name", required=True, choices=sorted(AGENT_NAMES))
    return parser


def render_bullets(paths: list[Path]) -> str:
    return "\n".join(f"- {path}" for path in paths)


def render_text_bullets(items: list[str]) -> str:
    if not items:
        return "- <none>"
    return "\n".join(f"- {item}" for item in items)


def summarize_task_payload(workspace_dir: Path, agent_name: str) -> list[str]:
    if agent_name not in TASK_FILENAME_BY_AGENT:
        return []
    _task_path, payload = build_task_input_payload(workspace_dir, agent_name)
    summary_lines: list[str] = []
    goal = str(payload.get("goal", "")).strip()
    if goal:
        summary_lines.append("任务目标:")
        summary_lines.append(f"- {goal}")
    required_reference_files = [
        str(item).strip() for item in payload.get("required_reference_files", []) if str(item).strip()
    ]
    if required_reference_files:
        summary_lines.append("必读参考文件:")
        summary_lines.extend(f"- {item}" for item in required_reference_files)
    appendix_read_contract = [
        str(item).strip() for item in payload.get("appendix_read_contract", []) if str(item).strip()
    ]
    if appendix_read_contract:
        summary_lines.append("附录读取合同:")
        summary_lines.extend(f"- {item}" for item in appendix_read_contract)
    allowed_scripts = [str(item).strip() for item in payload.get("allowed_official_scripts", []) if str(item).strip()]
    summary_lines.append("允许调用的正式脚本:")
    summary_lines.extend(f"- {item}" for item in (allowed_scripts or ["<none>"]))
    must_not_do = [str(item).strip() for item in payload.get("must_not_do", []) if str(item).strip()]
    if must_not_do:
        summary_lines.append("禁止事项:")
        summary_lines.extend(f"- {item}" for item in must_not_do)
    acceptance_checks = [str(item).strip() for item in payload.get("acceptance_checks", []) if str(item).strip()]
    if acceptance_checks:
        summary_lines.append("验收检查:")
        summary_lines.extend(f"- {item}" for item in acceptance_checks)
    required_post_checks = [str(item).strip() for item in payload.get("required_post_checks", []) if str(item).strip()]
    if required_post_checks:
        summary_lines.append("结果后验检查:")
        summary_lines.extend(f"- {item}" for item in required_post_checks)
    analysis_requirements = payload.get("analysis_requirements", {})
    if isinstance(analysis_requirements, dict) and analysis_requirements:
        summary_lines.append("分析要求:")
        if "graph_sequence_analysis_required" in analysis_requirements:
            summary_lines.append(
                f"- graph_sequence_analysis_required={bool(analysis_requirements.get('graph_sequence_analysis_required'))}"
            )
        for item in analysis_requirements.get("sequence_analysis_steps", []):
            normalized = str(item).strip()
            if normalized:
                summary_lines.append(f"- sequence step: {normalized}")
        for item in analysis_requirements.get("sequence_evidence_checks", []):
            normalized = str(item).strip()
            if normalized:
                summary_lines.append(f"- sequence check: {normalized}")
    graph_skeleton_scope = payload.get("graph_skeleton_scope", {})
    if isinstance(graph_skeleton_scope, dict) and graph_skeleton_scope:
        semantic = graph_skeleton_scope.get("semantic_skeleton", {})
        operator = graph_skeleton_scope.get("operator_skeleton", {})
        summary_lines.append("Graph skeleton 边界:")
        if isinstance(semantic, dict):
            summary_lines.append(
                "- semantic skeleton = "
                f"{str(semantic.get('definition', 'graph_mapping_targets.json.rows[*].span_id')).strip()}"
            )
            summary_lines.append(
                "- semantic counts: "
                f"frozen={len(semantic.get('frozen_graph_span_ids', []))}, "
                f"formal_targets={int(semantic.get('formal_graph_target_count', 0) or 0)}, "
                f"inventory={len(semantic.get('inventory_graph_span_ids', []))}, "
                f"phase_window_inventory={len(semantic.get('phase_window_inventory_span_ids', []))}"
            )
            if isinstance(semantic.get("counts_by_phase"), dict):
                summary_lines.append(f"- formal target counts by phase: {semantic.get('counts_by_phase', {})}")
            if isinstance(semantic.get("counts_by_semantic_class"), dict):
                summary_lines.append(
                    f"- formal target counts by semantic_class: {semantic.get('counts_by_semantic_class', {})}"
                )
        if isinstance(operator, dict):
            summary_lines.append(
                "- operator skeleton = "
                f"{str(operator.get('definition', 'graph_operator_spans.json.rows[*].graph_operator_span_id')).strip()}"
            )
            summary_lines.append(
                f"- operator count: frozen_graph_operator_span_ids={len(operator.get('frozen_graph_operator_span_ids', []))}"
            )
    return summary_lines


def build_query_bundle(workspace_dir: Path, agent_name: str) -> dict[str, Any]:
    state = load_state(workspace_dir)
    skill_dir = Path(state["skill_dir"])
    config = effective_agent_config(agent_name, int(state["current_step"]))
    prompt_path = skill_dir / config["prompt_file"]
    guide_path = skill_dir / config["guide_file"]
    contract_schema_path = skill_dir / config["contract_schema_file"] if config.get("contract_schema_file") else None
    contract_example_paths = [
        skill_dir / item
        for item in config.get("contract_example_files", [])
        if str(item).strip()
    ]
    prompt_text = read_text(prompt_path)
    input_files = resolve_workspace_paths(workspace_dir, config["input_files"])
    output_files = resolve_workspace_paths(workspace_dir, config["output_files"])
    task_summary_lines = summarize_task_payload(workspace_dir, agent_name)
    query_text = "\n".join(
        [
            "=== PREAMBLE ===",
            f"Agent: {agent_name}",
            f"Subagent Type: {config['subagent_type']}",
            f"Description: {config['description']}",
            f"Current Step: {config['step']}",
            f"Contract Schema: {config.get('contract_schema', 'default')}",
            f"Workspace: {workspace_dir}",
            f"Skill Dir: {skill_dir}",
            "",
            "唯一必读操作手册:",
            f"- {guide_path}",
            "",
            "结构化合同文件:",
            *( [f"- {contract_schema_path}"] if contract_schema_path else ["- <none>"] ),
            "",
            "参考示例文件:",
            *( [f"- {path}" for path in contract_example_paths] if contract_example_paths else ["- <none>"] ),
            "",
            "本次正式输入文件:",
            render_bullets(input_files),
            "",
            "本次正式输出文件:",
            render_bullets(output_files),
            "",
            "本次任务合同摘要:",
            *(task_summary_lines or ["- <no task summary>"]),
            "",
            "执行约束:",
            "- 先完整阅读唯一必读操作手册；如手册要求，再按其中附录索引继续 Read 其他参考文档",
            "- 若任务合同摘要中列出了必读参考文件或附录读取合同，必须在开始正式分析与写 JSON 前先读完，不能把它们当成可选建议",
            "- 若提供了结构化合同文件或示例文件，必须在写正式 JSON 前逐项对照；禁止只按自然语言理解自行发挥 schema",
            "- 禁止越权扫描整个 workspace 或跳过手册要求的前置检查",
            "- 证据不足时必须返回 partial / failed / fix_available 等契约内状态，不能编造结论",
            "- 必须先写正式 JSON，再写辅助 Markdown 报告",
            "- graph 相关 payload 若需要列表包装，必须使用 {'status': ..., 'row_count': N, 'rows': [...]} 形式，不能提交 phase-dict 代替 rows",
            "- 禁止在 workspace 内生成或执行新的 _*.py、tmp*.py、debug_*.py、temp*.py 临时脚本",
            "- 若输入损坏、模型上下文缺失或证据不足，只能返回契约内 partial/blocked/failed，不得通过回写工件伪装完成",
            "=== PREAMBLE END ===",
            "",
            prompt_text.strip(),
            "",
        ]
    )
    return {
        "state": state,
        "skill_dir": skill_dir,
        "config": config,
        "prompt_path": prompt_path,
        "guide_path": guide_path,
        "input_files": input_files,
        "output_files": output_files,
        "query_text": query_text,
    }


def write_query_artifacts(workspace_dir: Path, agent_name: str, query_text: str, config: dict[str, Any]) -> dict[str, str]:
    query_path = workspace_dir / "input" / f"query_{agent_name}.txt"
    ensure_parent(query_path)
    query_path.write_text(query_text, encoding="utf-8", newline="\n")

    snapshot_path = workspace_dir / "logs" / "agent_calls" / f"{agent_name}_{now_iso().replace(':', '-')}.txt"
    ensure_parent(snapshot_path)
    snapshot_path.write_text(query_text, encoding="utf-8", newline="\n")

    record = {
        "at": now_iso(),
        "agent_name": agent_name,
        "subagent_type": config["subagent_type"],
        "query_path": str(query_path),
        "snapshot_path": str(snapshot_path),
        "snapshot_sha256": compute_sha256(snapshot_path),
    }
    index_path = workspace_dir / "logs" / "agent_calls" / "index.jsonl"
    ensure_parent(index_path)
    with index_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {
        "query_path": str(query_path),
        "snapshot_path": str(snapshot_path),
        "snapshot_sha256": record["snapshot_sha256"],
    }


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    bundle = build_query_bundle(workspace_dir, args.agent_name)
    write_query_artifacts(workspace_dir, args.agent_name, bundle["query_text"], bundle["config"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
