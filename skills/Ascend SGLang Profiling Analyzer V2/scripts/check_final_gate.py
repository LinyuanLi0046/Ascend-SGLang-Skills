from __future__ import annotations

import argparse
import re
from pathlib import Path

from workflow_common import (
    compute_sha256,
    load_provenance_manifest,
    load_json,
    load_state,
    now_iso,
    read_text,
    save_state,
    validate_code_location,
    write_error_context,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查 V2 最终结构化交付物门禁。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


SELF_CALL_RE = re.compile(r"\bself\.\w+\s*\(")
CONSTRUCTOR_LINE_RE = re.compile(r"^\s*self\.\w+\s*=\s*[A-Za-z_][\w\.]*\(")


def graph_precision_required(graph_plan: dict, graph_mapping_targets: dict) -> bool:
    mode = str(graph_plan.get("mode", "")).strip()
    rows = graph_mapping_targets.get("rows", [])
    graph_target_count = len(rows) if isinstance(rows, list) else 0
    return mode in {"spec_v2", "decode_graph"} and graph_target_count > 0


def graph_precision_expected(graph_plan: dict) -> bool:
    mode = str(graph_plan.get("mode", "")).strip()
    return mode in {"spec_v2", "decode_graph"}


def extract_graph_alignment_items(payload):
    if isinstance(payload, dict):
        for key in ("items", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def read_repo_source_line(repo_root: Path, code_location: str) -> str:
    path_text, _, line_text = str(code_location).rpartition(":")
    if not path_text or not line_text.isdigit():
        return ""
    line_number = int(line_text)
    if line_number <= 0:
        return ""
    file_path = repo_root / path_text
    if not file_path.exists():
        return ""
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if 1 <= line_number <= len(lines):
        return lines[line_number - 1].strip()
    return ""


def is_graph_entry_location(code_location: str, repo_root: Path) -> bool:
    source_line = read_repo_source_line(repo_root, code_location).lower()
    return any(
        token in source_line
        for token in [
            ".replay(",
            "self.model.forward(",
            " model.forward(",
            "runner.forward(",
        ]
    )


def graph_source_line_violation(code_location: str, repo_root: Path) -> str:
    source_line = read_repo_source_line(repo_root, code_location)
    stripped = source_line.strip()
    if not stripped:
        return ""
    if ".replay(" in stripped:
        return "graph_replay_entry"
    if CONSTRUCTOR_LINE_RE.match(stripped):
        return "constructor_line"
    if SELF_CALL_RE.search(stripped):
        return "module_call_anchor"
    return ""


def verify_provenance(workspace_dir: Path, state: dict) -> list[str]:
    manifest = load_provenance_manifest(workspace_dir)
    errors: list[str] = []
    artifacts_manifest = manifest.get("artifacts", {})
    required_labels = [
        "artifact:graph_execution_plan_path",
        "artifact:graph_forward_context_path",
        "artifact:graph_mapping_targets_path",
        "artifact:graph_operator_spans_path",
        "artifact:graph_span_alignment_path",
        "artifact:span_code_mapping_path",
        "artifact:annotated_trace_path",
        "artifact:stream_span_timeline_path",
        "artifact:validation_result_path",
    ]
    for label in required_labels:
        entry = artifacts_manifest.get(label)
        if not isinstance(entry, dict):
            errors.append(f"workspace_provenance.json 缺少 {label}")
            continue
        path = Path(str(entry.get("path", "")).strip())
        if not path.exists():
            errors.append(f"{label} 对应文件不存在: {path}")
            continue
        expected_hash = str(entry.get("sha256", "")).strip()
        actual_hash = compute_sha256(path)
        if expected_hash != actual_hash:
            errors.append(f"{label} 在 finalize 之后被修改。")
    return errors


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    artifacts = state["artifacts"]
    flags = state["flags"]
    errors: list[str] = []

    if state.get("status") != "awaiting_final_gate":
        errors.append("final gate 只能在 state.status=awaiting_final_gate 时运行。")
    if int(state.get("current_step", 0) or 0) != 7:
        errors.append("final gate 只能在 current_step=7 时运行。")
    if int(state.get("last_completed_step", 0) or 0) != 7:
        errors.append("final gate 需要先完成 Step 7。")
    if str(state.get("next_action", "")).strip() != "run_final_gate":
        errors.append("final gate 前置 next_action 必须为 run_final_gate。")

    required_paths = {
        "annotated_trace_path": artifacts.get("annotated_trace_path", ""),
        "stream_span_timeline_path": artifacts.get("stream_span_timeline_path", ""),
        "span_code_mapping_path": artifacts.get("span_code_mapping_path", ""),
        "validation_result_path": artifacts.get("validation_result_path", ""),
        "graph_execution_plan_path": artifacts.get("graph_execution_plan_path", ""),
        "graph_forward_context_path": artifacts.get("graph_forward_context_path", ""),
        "graph_mapping_targets_path": artifacts.get("graph_mapping_targets_path", ""),
        "graph_operator_spans_path": artifacts.get("graph_operator_spans_path", ""),
        "graph_span_candidates_path": artifacts.get("graph_span_candidates_path", ""),
        "forward_segment_template_path": artifacts.get("forward_segment_template_path", ""),
        "graph_span_alignment_path": artifacts.get("graph_span_alignment_path", ""),
    }
    for label, path_str in required_paths.items():
        if not path_str or not Path(path_str).exists():
            errors.append(f"缺少必须工件: {label}")

    if errors:
        finalize_failure(workspace_dir, state, errors)
        return 1

    classified = load_json(Path(artifacts["classified_spans_path"]))
    mapping = load_json(Path(artifacts["span_code_mapping_path"]))
    timeline = load_json(Path(artifacts["stream_span_timeline_path"]))
    validation = load_json(Path(artifacts["validation_result_path"]))
    graph_plan = load_json(Path(artifacts["graph_execution_plan_path"]))
    graph_forward_context = load_json(Path(artifacts["graph_forward_context_path"]))
    graph_mapping_targets = load_json(Path(artifacts["graph_mapping_targets_path"]))
    graph_operator_spans = load_json(Path(artifacts["graph_operator_spans_path"]))
    graph_span_alignment = load_json(Path(artifacts["graph_span_alignment_path"]))
    annotated_trace = load_json(Path(artifacts["annotated_trace_path"]))
    validation_report_path = workspace_dir / "output" / "validation_report.md"
    if validation_report_path.exists():
        validation_report_text = read_text(validation_report_path).lower()
        if "status: failed" in validation_report_text and validation.get("status") == "passed":
            errors.append("validation_report.md 与 validation_result.json 状态冲突：报告 failed，但 JSON 为 passed。")

    errors.extend(verify_provenance(workspace_dir, state))

    if validation.get("status") != "passed":
        errors.append("validation_result.json.status 必须为 passed。")
    if validation.get("checks", {}).get("graph_precision_satisfied") is not True:
        errors.append("validation_result.json 必须显式确认 graph replay 已达到逐 span forward 代码行精度。")
    graph_mapping_target_rows = graph_mapping_targets.get("rows", [])
    formal_graph_target_ids = {
        str(item.get("span_id", "")).strip()
        for item in graph_mapping_target_rows
        if isinstance(item, dict) and str(item.get("span_id", "")).strip()
    } if isinstance(graph_mapping_target_rows, list) else set()
    if graph_precision_expected(graph_plan) and not formal_graph_target_ids:
        errors.append("graph_mapping_targets.json 未提供任何正式 formal graph target。")
    if graph_precision_required(graph_plan, graph_mapping_targets) and graph_plan.get("mapping_granularity") != "per_span_forward_code":
        errors.append("graph_execution_plan 仍不是 per_span_forward_code。")
    if graph_precision_required(graph_plan, graph_mapping_targets) and graph_forward_context.get("mapping_granularity") != "per_span_forward_code":
        errors.append("graph_forward_context 仍未达到 per_span_forward_code。")
    graph_operator_span_ids = {
        str(item.get("graph_operator_span_id", "")).strip()
        for item in graph_operator_spans.get("rows", [])
        if isinstance(item, dict) and str(item.get("graph_operator_span_id", "")).strip()
    }
    if graph_precision_required(graph_plan, graph_mapping_targets) and not graph_operator_span_ids:
        errors.append("graph_operator_spans.json 缺少正式 operator spans。")
    alignment_items = extract_graph_alignment_items(graph_span_alignment)
    for index, item in enumerate(alignment_items):
        location_kind = str(item.get("location_kind", "")).strip()
        operator_evidence_kind = str(item.get("operator_evidence_kind", "")).strip()
        requires_further_drilldown = item.get("requires_further_drilldown")
        graph_operator_span_id = str(item.get("graph_operator_span_id", "")).strip()
        code_location = str(item.get("code_location", "")).strip()
        if not graph_operator_span_id:
            errors.append(f"graph_span_alignment[{index}] 缺少 graph_operator_span_id")
        elif graph_operator_span_id not in graph_operator_span_ids:
            errors.append(f"graph_span_alignment[{index}] graph_operator_span_id 无法回溯: {graph_operator_span_id}")
        if location_kind != "operator_call":
            errors.append(f"graph_span_alignment[{index}] location_kind 不是 operator_call: {location_kind or '<missing>'}")
        if not operator_evidence_kind:
            errors.append(f"graph_span_alignment[{index}] 缺少 operator_evidence_kind")
        if requires_further_drilldown is not False:
            errors.append(
                f"graph_span_alignment[{index}] requires_further_drilldown 必须为 false，当前为 {requires_further_drilldown!r}"
            )
        if code_location:
            violation = graph_source_line_violation(code_location, Path(state["inputs"]["code_repo_path"]))
            if violation:
                errors.append(f"graph_span_alignment[{index}] code_location 仍停在 {violation}: {code_location}")

    mapping_by_span = {row["span_id"]: row for row in mapping.get("rows", [])}
    coverage = mapping.get("coverage", {})
    unresolved_semantic_span_count = coverage.get("unresolved_semantic_span_count")
    if unresolved_semantic_span_count is None:
        unresolved_semantic_span_count = coverage.get("unmapped_semantic_span_count", 0)
    required_coverage_fields = {
        "total_span_count",
        "semantic_span_count",
        "excluded_span_count",
        "mapped_span_count",
        "unresolved_semantic_span_count",
        "low_confidence_span_count",
    }
    missing_coverage_fields = sorted(required_coverage_fields - set(coverage.keys()))
    if missing_coverage_fields:
        errors.append(f"coverage 缺少字段: {missing_coverage_fields}")
    events = annotated_trace.get("traceEvents") if isinstance(annotated_trace, dict) else None
    if not isinstance(events, list):
        events = annotated_trace.get("events") if isinstance(annotated_trace, dict) else None
    if not isinstance(events, list):
        events = annotated_trace if isinstance(annotated_trace, list) else []
    semantic_spans = []
    excluded_spans = []
    trace_event_ref_by_span = {}
    semantic_location_frequency: dict[str, int] = {}
    for stream in classified.get("streams", []):
        previous_start = None
        for span in stream.get("spans", []):
            trace_event_ref_by_span[span["span_id"]] = span.get("trace_event_ref", {})
            if span.get("exclude_from_code_mapping"):
                excluded_spans.append(span["span_id"])
            else:
                semantic_spans.append(span["span_id"])
            if previous_start is not None and span["start_ns"] < previous_start:
                errors.append(f"classified_spans 中 stream {stream['stream_id']} 时序倒序。")
            previous_start = span["start_ns"]
            if span.get("scope_class") == "hardware_semantic_candidate" and not span.get("has_stream_id", False):
                errors.append(f"semantic span 缺少 streamId/stream_id: {span['span_id']}")
            span_name = str(span.get("span_name", ""))
            if not span.get("exclude_from_code_mapping") and any(
                token in span_name for token in ["CAPTURE_", "NOTIFY_", "EVENT_", "AscendCL@", "Runtime@Event"]
            ):
                errors.append(f"明显非语义 span 被保留在 semantic 集合: {span['span_id']} -> {span_name}")

    for span_id in semantic_spans:
        row = mapping_by_span.get(span_id)
        if not row:
            errors.append(f"语义 span 缺少映射行: {span_id}")
            continue
        code_location = str(row.get("code_location", ""))
        if not code_location:
            errors.append(f"语义 span 缺少 code_location: {span_id}")
        elif not validate_code_location(code_location):
            errors.append(f"语义 span code_location 非法: {span_id} -> {code_location}")
        else:
            semantic_location_frequency[code_location] = semantic_location_frequency.get(code_location, 0) + 1
        ref = trace_event_ref_by_span.get(span_id, {})
        event_index = ref.get("trace_event_index")
        if isinstance(event_index, int) and 0 <= event_index < len(events):
            event = events[event_index]
            if "code_location" in event:
                errors.append(f"annotated trace 顶层不得出现 code_location: span={span_id}")
            args_dict = event.get("args", {})
            if not isinstance(args_dict, dict) or str(args_dict.get("code_location", "")).strip() != code_location:
                errors.append(f"annotated trace args.code_location 缺失或不一致: span={span_id}")

    for span_id in excluded_spans:
        row = mapping_by_span.get(span_id)
        if row and row.get("code_location"):
            errors.append(f"排除 span 不得写 code_location: {span_id}")

    if int(unresolved_semantic_span_count) > 0:
        errors.append("coverage 显示仍有 unresolved semantic spans。")

    if alignment_items:
        graph_location_frequency: dict[str, int] = {}
        repo_root = Path(state["inputs"]["code_repo_path"])
        for item in alignment_items:
            code_location = str(item.get("code_location", "")).strip()
            if code_location:
                graph_location_frequency[code_location] = graph_location_frequency.get(code_location, 0) + 1
        if graph_location_frequency:
            top_graph_location, top_graph_count = max(graph_location_frequency.items(), key=lambda item: item[1])
            top_graph_ratio = top_graph_count / max(1, len(alignment_items))
            if top_graph_ratio >= 0.5 and is_graph_entry_location(top_graph_location, repo_root):
                errors.append(
                    f"graph span alignment 异常收缩到入口行: {top_graph_location} 命中 {top_graph_count}/{len(alignment_items)} spans。"
                )

    global_order = timeline.get("global_order", [])
    for index in range(1, len(global_order)):
        prev = global_order[index - 1]
        curr = global_order[index]
        if (curr["start_ns"], curr["end_ns"], curr["stream_id"], curr["span_id"]) < (
            prev["start_ns"],
            prev["end_ns"],
            prev["stream_id"],
            prev["span_id"],
        ):
            errors.append("stream_span_timeline.json 的 global_order 不是稳定时间序。")
            break

    if not flags.get("annotated_trace_generated"):
        errors.append("state.flags.annotated_trace_generated 必须为 true。")
    if not flags.get("timeline_generated"):
        errors.append("state.flags.timeline_generated 必须为 true。")
    if not flags.get("validation_passed"):
        errors.append("state.flags.validation_passed 必须为 true。")

    if errors:
        finalize_failure(workspace_dir, state, errors)
        return 1

    state["status"] = "completed"
    state["next_action"] = "completed"
    state["flags"]["final_review_passed"] = True
    state.setdefault("step_history", []).append(
        {
            "step": "final_gate",
            "name": "FINAL_GATE",
            "status": "completed",
            "at": now_iso(),
        }
    )
    save_state(workspace_dir, state)
    print("最终门禁通过。")
    return 0


def finalize_failure(workspace_dir: Path, state: dict, errors: list[str]) -> None:
    write_error_context(
        workspace_dir,
        {
            "task_type": "debug_failure",
            "failed_step": "final_gate",
            "failed_component": "check_final_gate.py",
            "error_type": "final_gate_failed",
            "error_message": "; ".join(errors),
            "related_files": [
                state["artifacts"].get("annotated_trace_path", ""),
                state["artifacts"].get("stream_span_timeline_path", ""),
                state["artifacts"].get("span_code_mapping_path", ""),
                state["artifacts"].get("validation_result_path", ""),
                state["artifacts"].get("graph_execution_plan_path", ""),
                state["artifacts"].get("graph_span_alignment_path", ""),
            ],
            "previous_fixes": [],
            "timestamp": now_iso(),
        },
    )
    state["status"] = "blocked"
    state["next_action"] = "call_profiling_debugger"
    state["flags"]["debug_fix_pending"] = True
    save_state(workspace_dir, state)
    print("最终门禁失败:")
    for item in errors:
        print(f"- {item}")


if __name__ == "__main__":
    raise SystemExit(main())
