from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from build_graph_seed_context import build_graph_seed_context_for_workspace
from build_runtime_constraints import load_model_configs
from workflow_common import dump_json, load_json, load_state, save_state


SUPPORT_FILE_CANDIDATES = [
    "python/sglang/srt/model_executor/model_runner.py",
    "python/sglang/srt/speculative/eagle_worker_v2.py",
    "python/sglang/srt/layers/communicator.py",
    "python/sglang/srt/hardware_backend/npu/memory_pool_npu.py",
    "python/sglang/srt/hardware_backend/npu/attention/ascend_backend.py",
    "python/sglang/srt/layers/quantization/modelslim/modelslim.py",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建 graph replay 对应的模型 forward 链路上下文。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def list_architectures(model_context: dict[str, Any]) -> list[str]:
    if model_context.get("architecture_candidates"):
        return [str(item).strip() for item in model_context.get("architecture_candidates", []) if str(item).strip()]
    parsed_configs = model_context.get("parsed_configs", {})
    config = parsed_configs.get("config.json", {})
    architectures = config.get("architectures", []) if isinstance(config, dict) else []
    return [str(item).strip() for item in architectures if str(item).strip()]


def scan_model_files(repo_root: Path, architectures: list[str]) -> list[dict[str, Any]]:
    model_dir = repo_root / "python" / "sglang" / "srt" / "models"
    results: list[dict[str, Any]] = []
    if not model_dir.exists():
        return results
    for file_path in sorted(model_dir.glob("*.py")):
        content = file_path.read_text(encoding="utf-8")
        repo_relative_path = file_path.relative_to(repo_root).as_posix()
        for architecture in architectures:
            exact_entry = f"EntryClass = [{architecture}]"
            spaced_entry = f"EntryClass = [ {architecture} ]"
            class_decl = f"class {architecture}("
            matched_by = ""
            if exact_entry in content or spaced_entry in content:
                matched_by = "entry_class"
            elif class_decl in content:
                matched_by = "class_definition"
            if matched_by:
                results.append(
                    {
                        "architecture": architecture,
                        "repo_relative_path": repo_relative_path,
                        "matched_by": matched_by,
                    }
                )
    return results


def select_primary_model_file(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if not candidates:
        return {}
    sorted_candidates = sorted(
        candidates,
        key=lambda item: (
            0 if item.get("matched_by") == "entry_class" else 1,
            item.get("repo_relative_path", ""),
        ),
    )
    return sorted_candidates[0]


def find_class_forward_line(content_lines: list[str], class_name_suffix: str) -> int:
    class_names = [
        line.strip().split("class ", 1)[1].split("(", 1)[0].split(":", 1)[0].strip()
        for line in content_lines
        if line.strip().startswith("class ")
    ]
    target_classes = [name for name in class_names if name.endswith(class_name_suffix)]
    if not target_classes:
        return 0
    for target_class in target_classes:
        for index, line in enumerate(content_lines, start=1):
            if line.strip().startswith(f"class {target_class}"):
                class_indent = len(line) - len(line.lstrip(" "))
                for inner_index in range(index + 1, len(content_lines) + 1):
                    inner_line = content_lines[inner_index - 1]
                    stripped = inner_line.strip()
                    if not stripped:
                        continue
                    indent = len(inner_line) - len(inner_line.lstrip(" "))
                    if indent <= class_indent and stripped.startswith("class "):
                        break
                    if indent > class_indent and stripped.startswith("def forward("):
                        return inner_index
    return 0


def find_first_forward_line(content_lines: list[str]) -> int:
    for index, line in enumerate(content_lines, start=1):
        if line.strip().startswith("def forward("):
            return index
    return 0


def collect_model_file_summary(repo_root: Path, repo_relative_path: str) -> dict[str, Any]:
    file_path = repo_root / repo_relative_path
    if not file_path.exists():
        return {
            "repo_relative_path": repo_relative_path,
            "exists": False,
            "first_forward_line": 0,
            "model_entry_forward_line": 0,
            "inner_model_forward_line": 0,
            "decoder_layer_forward_line": 0,
        }
    content_lines = file_path.read_text(encoding="utf-8").splitlines()
    return {
        "repo_relative_path": repo_relative_path,
        "exists": True,
        "first_forward_line": find_first_forward_line(content_lines),
        "model_entry_forward_line": find_class_forward_line(content_lines, "ForCausalLM"),
        "inner_model_forward_line": find_class_forward_line(content_lines, "Model"),
        "decoder_layer_forward_line": find_class_forward_line(content_lines, "DecoderLayer"),
    }


def safe_json_path(raw_value: str) -> Path | None:
    value = str(raw_value).strip()
    if not value:
        return None
    path = Path(value)
    if not path.exists() or not path.is_file():
        return None
    return path


def _repo_divergence_file_sets(repo_divergence_report: dict[str, Any]) -> tuple[set[str], set[str]]:
    existing_files = {
        str(item).strip()
        for item in repo_divergence_report.get("existing_files", [])
        if str(item).strip()
    }
    missing_files = {
        str(item).strip()
        for item in repo_divergence_report.get("missing_files", [])
        if str(item).strip()
    }
    for row in repo_divergence_report.get("checked_files", []):
        if not isinstance(row, dict):
            continue
        relative_path = str(row.get("relative_path", "")).strip()
        if not relative_path:
            continue
        if row.get("exists"):
            existing_files.add(relative_path)
        else:
            missing_files.add(relative_path)
    return existing_files, missing_files


def build_candidate_forward_anchors(model_summary: dict[str, Any], model_role: str) -> list[dict[str, Any]]:
    repo_relative_path = str(model_summary.get("repo_relative_path", ""))
    if not repo_relative_path:
        return []
    anchors = []
    for kind, line, symbol in [
        ("first_forward", int(model_summary.get("first_forward_line", 0) or 0), "forward"),
        ("model_forward", int(model_summary.get("model_entry_forward_line", 0) or 0), "entry.forward"),
        ("inner_model_forward", int(model_summary.get("inner_model_forward_line", 0) or 0), "model.forward"),
        ("decoder_layer_forward", int(model_summary.get("decoder_layer_forward_line", 0) or 0), "decoder_layer.forward"),
    ]:
        if line > 0:
            anchors.append(
                {
                    "kind": kind,
                    "model_role": model_role,
                    "repo_relative_path": repo_relative_path,
                    "symbol": symbol,
                    "line": line,
                }
            )
    return anchors


def _existing_support_files(
    repo_root: Path,
    runtime_constraints: dict[str, Any],
    repo_divergence_report: dict[str, Any],
) -> list[str]:
    repo_existing_files, repo_missing_files = _repo_divergence_file_sets(repo_divergence_report)
    files = [relative for relative in SUPPORT_FILE_CANDIDATES if (repo_root / relative).exists()]
    graph_runner_dir = repo_root / "python" / "sglang" / "srt" / "hardware_backend" / "npu" / "graph_runner"
    if graph_runner_dir.exists():
        for path in sorted(graph_runner_dir.glob("*.py")):
            files.append(path.relative_to(repo_root).as_posix())
    if "modelslim" not in runtime_constraints.get("quant_mode_candidates", []):
        files = [item for item in files if not item.endswith("modelslim.py")]
    normalized_files = []
    for item in files:
        normalized = str(item).replace("\\", "/")
        if normalized in repo_missing_files and normalized not in repo_existing_files:
            continue
        normalized_files.append(normalized)
    return sorted(dict.fromkeys(normalized_files))


def _candidate_search_roots(
    primary_model: dict[str, Any],
    draft_model: dict[str, Any],
    runtime_constraints: dict[str, Any],
) -> list[str]:
    roots = [
        "python/sglang/srt/models",
        "python/sglang/srt/layers",
        "python/sglang/srt/model_executor",
        "python/sglang/srt/speculative",
    ]
    if "ascend_npu" in runtime_constraints.get("backend_candidates", []):
        roots.extend(
            [
                "python/sglang/srt/hardware_backend/npu",
                "python/sglang/srt/hardware_backend/npu/attention",
                "python/sglang/srt/hardware_backend/npu/graph_runner",
            ]
        )
    if "modelslim" in runtime_constraints.get("quant_mode_candidates", []):
        roots.append("python/sglang/srt/layers/quantization/modelslim")
    for model_file in [primary_model.get("repo_relative_path", ""), draft_model.get("repo_relative_path", "")]:
        if model_file:
            roots.append(str(Path(model_file).parent).replace("\\", "/"))
    return sorted(dict.fromkeys(item for item in roots if item))


def _normalized_graph_mapping_target_ids(graph_mapping_targets: dict[str, Any]) -> list[str]:
    rows = graph_mapping_targets.get("rows", [])
    if not isinstance(rows, list):
        return []
    normalized: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        span_id = str(row.get("span_id", "")).strip()
        if span_id:
            normalized.append(span_id)
    return sorted(set(normalized))


def _graph_mapping_target_summary(graph_mapping_targets: dict[str, Any]) -> dict[str, Any]:
    summary = graph_mapping_targets.get("summary", {})
    if not isinstance(summary, dict):
        return {
            "approved_target_count": len(_normalized_graph_mapping_target_ids(graph_mapping_targets)),
            "counts_by_phase": {},
            "counts_by_semantic_class": {},
        }
    return {
        "approved_target_count": int(summary.get("approved_target_count", 0) or 0),
        "counts_by_phase": dict(summary.get("counts_by_phase", {})) if isinstance(summary.get("counts_by_phase"), dict) else {},
        "counts_by_semantic_class": dict(summary.get("counts_by_semantic_class", {}))
        if isinstance(summary.get("counts_by_semantic_class"), dict)
        else {},
    }


def _normalized_graph_span_ids(graph_plan: dict[str, Any]) -> list[str]:
    raw_value = graph_plan.get("identified_graph_span_ids", [])
    if not isinstance(raw_value, list):
        return []
    return [str(item).strip() for item in raw_value if str(item).strip()]


def _readiness_blockers(
    runtime_constraints: dict[str, Any],
    graph_plan: dict[str, Any],
    graph_mapping_targets: dict[str, Any],
    primary_model_candidates: list[dict[str, Any]],
    draft_model_candidates: list[dict[str, Any]],
    candidate_forward_anchors: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    blockers = [str(item) for item in runtime_constraints.get("step5_preconditions", {}).get("blockers", [])]
    warnings = [str(item) for item in runtime_constraints.get("step5_preconditions", {}).get("warnings", [])]
    graph_mode = str(graph_plan.get("graph_mode", "unknown")).strip()
    graph_mapping_target_ids = _normalized_graph_mapping_target_ids(graph_mapping_targets)
    if graph_mode not in {"speculative", "decode_only"}:
        blockers.append("graph_execution_plan 未能把当前样例归类为 speculative/decode_only graph。")
    if not graph_plan.get("graph_groups"):
        blockers.append("graph_execution_plan 未识别到可分析的 graph groups。")
    if not graph_mapping_target_ids:
        blockers.append("graph_mapping_targets 未识别到正式 graph mapping targets，Step5 不应尝试路径重建。")
    if not primary_model_candidates:
        blockers.append("当前 repo 未定位到主模型实现文件，无法从模型 forward 继续下钻。")
    if runtime_constraints.get("step5_preconditions", {}).get("requires_draft_model") and not draft_model_candidates:
        blockers.append("speculative 模式要求 draft 模型，但当前 repo/输入未定位到 draft 模型实现文件。")
    if not candidate_forward_anchors:
        blockers.append("graph_forward_context 未抽取到任何 forward 锚点，无法开始 Step5 下钻。")
    if str(graph_plan.get("status", "")).strip() == "blocked":
        warnings.extend(str(item) for item in graph_plan.get("warnings", []))
    return blockers, warnings


def build_graph_forward_context_for_workspace(workspace_dir: Path) -> dict[str, Any]:
    state = load_state(workspace_dir)
    repo_root = Path(state["inputs"]["code_repo_path"])
    runtime_constraints_path = safe_json_path(str(state["artifacts"].get("runtime_constraints_path", "")))
    graph_plan_path = safe_json_path(str(state["artifacts"].get("graph_execution_plan_path", "")))
    graph_mapping_targets_path = safe_json_path(str(state["artifacts"].get("graph_mapping_targets_path", "")))
    repo_divergence_report_path = safe_json_path(str(state["artifacts"].get("repo_divergence_report_path", "")))
    runtime_constraints = load_json(runtime_constraints_path) if runtime_constraints_path else {}
    graph_plan = load_json(graph_plan_path) if graph_plan_path else {}
    graph_mapping_targets = load_json(graph_mapping_targets_path) if graph_mapping_targets_path else {}
    repo_divergence_report = load_json(repo_divergence_report_path) if repo_divergence_report_path else {}

    primary_model_context = dict(runtime_constraints.get("primary_model_context", {}))
    if not primary_model_context:
        primary_model_context = load_model_configs(Path(state["inputs"]["model_root_path"]))
    draft_model_context = dict(runtime_constraints.get("draft_model_context", {}))

    architectures = list_architectures(primary_model_context)
    draft_architectures = list_architectures(draft_model_context)
    model_candidates = scan_model_files(repo_root, architectures)
    draft_model_candidates = scan_model_files(repo_root, draft_architectures)
    primary_model = select_primary_model_file(model_candidates)
    draft_model = select_primary_model_file(draft_model_candidates)
    model_summary = collect_model_file_summary(repo_root, primary_model.get("repo_relative_path", ""))
    draft_model_summary = collect_model_file_summary(repo_root, draft_model.get("repo_relative_path", ""))

    candidate_forward_anchors = build_candidate_forward_anchors(model_summary, "primary")
    candidate_forward_anchors.extend(build_candidate_forward_anchors(draft_model_summary, "draft"))
    support_files = _existing_support_files(repo_root, runtime_constraints, repo_divergence_report)
    support_files.extend([primary_model.get("repo_relative_path", ""), draft_model.get("repo_relative_path", "")])
    support_file_hints = sorted(dict.fromkeys(item for item in support_files if item))
    repo_existing_files, repo_missing_files = _repo_divergence_file_sets(repo_divergence_report)
    repo_file_existence_facts = {
        "fact_source": {
            "repo_divergence_report_path": str(repo_divergence_report_path) if repo_divergence_report_path else "",
            "repo_exists_scan": True,
        },
        "existing_files": sorted(dict.fromkeys(item for item in ([*repo_existing_files, *support_file_hints]))),
        "missing_files": sorted(repo_missing_files),
    }

    blockers, warnings = _readiness_blockers(
        runtime_constraints,
        graph_plan,
        graph_mapping_targets,
        model_candidates,
        draft_model_candidates,
        candidate_forward_anchors,
    )
    graph_mapping_target_summary = _graph_mapping_target_summary(graph_mapping_targets)
    if architectures and not primary_model:
        warnings.append("已解析到主模型 architecture，但当前 repo 未匹配到对应模型文件。")
    if draft_architectures and not draft_model:
        warnings.append("已解析到 draft 模型 architecture，但当前 repo 未匹配到对应 draft 模型文件。")

    output = {
        "schema_version": "graph_forward_context_v3",
        "status": "blocked" if blockers else "partial",
        "goal": "prepare candidate model/forward context for graph_path_analyst; path reconstruction must be done by the subagent against the current repo.",
        "architectures": architectures,
        "draft_architectures": draft_architectures,
        "model_file_candidates": model_candidates,
        "draft_model_file_candidates": draft_model_candidates,
        "primary_model_file": primary_model,
        "draft_model_file": draft_model,
        "primary_model_context": primary_model_context,
        "draft_model_context": draft_model_context,
        "runtime_constraints_path": str(runtime_constraints_path) if runtime_constraints_path else "",
        "repo_divergence_report_path": str(repo_divergence_report_path) if repo_divergence_report_path else "",
        "graph_execution_plan_path": str(graph_plan_path) if graph_plan_path else "",
        "graph_mapping_targets_path": str(graph_mapping_targets_path) if graph_mapping_targets_path else "",
        "graph_groups": graph_plan.get("graph_groups", []),
        "graph_mapping_targets_summary": graph_mapping_target_summary,
        "model_file_summary": model_summary,
        "draft_model_file_summary": draft_model_summary,
        "candidate_search_roots": _candidate_search_roots(primary_model, draft_model, runtime_constraints),
        "candidate_forward_anchors": candidate_forward_anchors,
        "candidate_decoder_layer_anchors": [row for row in candidate_forward_anchors if row["kind"] == "decoder_layer_forward"],
        "support_file_hints": support_file_hints,
        "repo_file_existence_facts": repo_file_existence_facts,
        "path_reconstruction_ready": not blockers,
        "path_reconstruction_readiness": {
            "status": "ready" if not blockers else "blocked",
            "blockers": blockers,
            "warnings": warnings,
            "graph_group_count": len(graph_plan.get("graph_groups", [])),
            "formal_graph_target_count": len(_normalized_graph_mapping_target_ids(graph_mapping_targets)),
            "counts_by_phase": graph_mapping_target_summary.get("counts_by_phase", {}),
        },
        "mapping_granularity": "candidate_context_only",
        "mapping_limitations": [
            "当前文件只提供 Step5 下钻是否可开始的正式前提判断，以及候选模型文件、forward 锚点、support/search roots。",
            "当前文件不负责生成 phase window 内 operator spans；这些 operator spans 必须由 build_graph_operator_spans.py 单独生成。",
            "本脚本不再通过纯关键词扫描预生成 communication/cache/operator 行级锚点；这些位置必须由 graph_path_analyst 结合当前 repo、知识手册与 profiling 证据继续下钻确认。",
            "graph 内真实代码路径重建与 span alignment 必须由 graph_path_analyst 基于当前 repo 和 profiling 证据完成。",
        ],
        "warnings": warnings,
    }

    output_path = workspace_dir / "artifacts" / "graph" / "graph_forward_context.json"
    dump_json(output_path, output)
    state["artifacts"]["graph_forward_context_path"] = str(output_path)
    state["flags"]["graph_forward_context_built"] = True
    save_state(workspace_dir, state)
    build_graph_seed_context_for_workspace(workspace_dir)
    return output


def main() -> int:
    args = build_parser().parse_args()
    build_graph_forward_context_for_workspace(Path(args.workspace_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
