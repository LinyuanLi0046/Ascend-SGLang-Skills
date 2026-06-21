from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from workflow_common import dump_json, load_json, load_state, save_state


KNOWLEDGE_FILES = [
    "references/knowledge/sglang_path_map.md",
    "references/knowledge/forward_analysis_rules.md",
    "references/knowledge/model_config_and_launch_fields.md",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建 Step 5 graph seed context。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def _safe_load_json(path_str: str) -> dict[str, Any]:
    if not path_str:
        return {}
    path = Path(path_str)
    if not path.exists() or not path.is_file():
        return {}
    return load_json(path)


def _normalized_graph_span_ids(graph_plan: dict[str, Any]) -> list[str]:
    raw_value = graph_plan.get("identified_graph_span_ids", [])
    if not isinstance(raw_value, list):
        return []
    return [str(item).strip() for item in raw_value if str(item).strip()]


def _normalized_graph_mapping_target_ids(graph_mapping_targets: dict[str, Any]) -> list[str]:
    rows = graph_mapping_targets.get("rows", [])
    if not isinstance(rows, list):
        return []
    return [str(row.get("span_id", "")).strip() for row in rows if isinstance(row, dict) and str(row.get("span_id", "")).strip()]


def build_graph_seed_context_for_workspace(workspace_dir: Path) -> dict[str, Any]:
    state = load_state(workspace_dir)
    skill_dir = Path(state["skill_dir"])
    classified = _safe_load_json(str(state["artifacts"].get("classified_spans_path", "")))
    timeline_index = _safe_load_json(str(state["artifacts"].get("timeline_index_path", "")))
    stack_evidence = _safe_load_json(str(state["artifacts"].get("stack_evidence_path", "")))
    graph_phase_stack_evidence = _safe_load_json(str(state["artifacts"].get("graph_phase_stack_evidence_path", "")))
    graph_plan = _safe_load_json(str(state["artifacts"].get("graph_execution_plan_path", "")))
    graph_mapping_targets = _safe_load_json(str(state["artifacts"].get("graph_mapping_targets_path", "")))
    graph_forward_context = _safe_load_json(str(state["artifacts"].get("graph_forward_context_path", "")))
    runtime_constraints = _safe_load_json(str(state["artifacts"].get("runtime_constraints_path", "")))
    divergence_report = _safe_load_json(str(state["artifacts"].get("repo_divergence_report_path", "")))

    knowledge_files = []
    for relative in KNOWLEDGE_FILES:
        path = skill_dir / relative
        knowledge_files.append(
            {
                "relative_path": relative.replace("\\", "/"),
                "absolute_path": str(path),
                "exists": path.exists(),
                "is_blank": not path.read_text(encoding="utf-8").strip() if path.exists() else True,
            }
        )

    streams = classified.get("streams", [])
    semantic_span_count = int(classified.get("semantic_span_count", 0))
    inventory_graph_span_ids = _normalized_graph_span_ids(graph_plan)
    if not inventory_graph_span_ids:
        merged_ids: list[str] = []
        for hint in graph_plan.get("mapping_hints", []):
            merged_ids.extend(str(item) for item in hint.get("candidate_span_ids", []))
        inventory_graph_span_ids = merged_ids
    formal_graph_target_ids = _normalized_graph_mapping_target_ids(graph_mapping_targets)
    model_candidates = graph_forward_context.get("model_file_candidates", [])
    primary_model = graph_forward_context.get("primary_model_file", {})

    candidate_forward_anchors = graph_forward_context.get("candidate_forward_anchors", [])
    support_file_hints = graph_forward_context.get("support_file_hints", [])
    graph_groups = graph_plan.get("graph_groups", graph_plan.get("phase_plan", []))

    output = {
        "schema_version": "graph_seed_context_v3",
        "workspace_dir": str(workspace_dir),
        "knowledge_applicability": divergence_report.get("knowledge_applicability", "knowledge_partially_applicable"),
        "recommended_analysis_mode": divergence_report.get("recommended_analysis_mode", "knowledge_partially_applicable"),
        "knowledge_files": knowledge_files,
        "runtime_constraints_path": str(state["artifacts"].get("runtime_constraints_path", "")),
        "repo_divergence_report_path": str(state["artifacts"].get("repo_divergence_report_path", "")),
        "graph_phase_stack_evidence_path": str(state["artifacts"].get("graph_phase_stack_evidence_path", "")),
        "phase_candidates": runtime_constraints.get("phase_candidates", []),
        "resolved_model_family": runtime_constraints.get("resolved_model_family", ""),
        "candidate_model_files": model_candidates,
        "primary_model_file": primary_model,
        "draft_model_file": graph_forward_context.get("draft_model_file", {}),
        "candidate_forward_anchors": candidate_forward_anchors,
        "candidate_search_roots": graph_forward_context.get("candidate_search_roots", []),
        "support_file_hints": support_file_hints[:80],
        "repo_file_existence_facts": graph_forward_context.get("repo_file_existence_facts", {}),
        "path_reconstruction_readiness": graph_forward_context.get(
            "path_reconstruction_readiness",
            runtime_constraints.get("step5_preconditions", {}),
        ),
        "graph_span_summary": {
            "stream_count": len(streams),
            "semantic_span_count": semantic_span_count,
            "inventory_graph_span_count": len(inventory_graph_span_ids),
            "formal_graph_target_count": len(formal_graph_target_ids),
        },
        "inventory_graph_span_ids": inventory_graph_span_ids,
        "formal_graph_target_ids": formal_graph_target_ids,
        "graph_groups": graph_groups,
        "graph_phase_stack_summary": graph_phase_stack_evidence.get("summary", {}),
        "graph_phase_evidence_rows": graph_phase_stack_evidence.get("rows", [])[:50],
        "hotspot_hints": [
            "python/sglang/srt/models",
            "python/sglang/srt/layers",
            "python/sglang/srt/speculative",
            "python/sglang/srt/hardware_backend/npu",
        ],
        "profiling_evidence_summary": {
            "task_count": len(timeline_index.get("tasks", [])),
            "op_count": len(timeline_index.get("ops", [])),
            "stack_row_count": len(stack_evidence.get("rows", [])),
            "graph_phase_stack_row_count": len(graph_phase_stack_evidence.get("rows", [])),
        },
        "notes": [
            "本文件只提供 graph_path_analyst 的候选上下文和搜索边界，不包含已确认的真实执行路径。",
            "support_file_hints 与 candidate_search_roots 只是搜索提示，不能直接当作 communication/cache/operator 最终落点。",
            "只有当 path_reconstruction_readiness.status=ready 时，Step5 才应继续做真实路径下钻。",
            "若知识文档与当前仓库冲突，必须以当前仓库代码和 profiling 证据为准。",
        ],
    }

    output_path = workspace_dir / "input" / "graph_seed_context.json"
    dump_json(output_path, output)
    state["artifacts"]["graph_seed_context_path"] = str(output_path)
    state["flags"]["graph_seed_context_built"] = True
    save_state(workspace_dir, state)
    return output


def main() -> int:
    args = build_parser().parse_args()
    build_graph_seed_context_for_workspace(Path(args.workspace_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
