from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
from typing import Any

from span_scope_rules import match_exclude_rule
from workflow_common import dump_json, load_json, load_state, save_state, validate_code_location, write_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 Step 7 正式验证结果。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def load_events(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        events = payload.get("traceEvents") or payload.get("events") or []
    elif isinstance(payload, list):
        events = payload
    else:
        events = []
    return events if isinstance(events, list) else []


def graph_precision_required(graph_plan: dict[str, Any], graph_mapping_targets: dict[str, Any]) -> bool:
    mode = str(graph_plan.get("mode", "")).strip()
    graph_target_rows = graph_mapping_targets.get("rows", [])
    graph_target_count = len(graph_target_rows) if isinstance(graph_target_rows, list) else 0
    return mode in {"spec_v2", "decode_graph"} and graph_target_count > 0


def graph_precision_expected(graph_plan: dict[str, Any]) -> bool:
    mode = str(graph_plan.get("mode", "")).strip()
    return mode in {"spec_v2", "decode_graph"}


def extract_graph_alignment_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("items", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def require_artifact_path(artifacts: dict[str, Any], key: str) -> Path:
    raw_path = str(artifacts.get(key, "")).strip()
    if not raw_path:
        raise RuntimeError(f"Step7 缺少必须工件路径: {key}")
    path = Path(raw_path)
    if not path.exists():
        raise RuntimeError(f"Step7 必须工件不存在: {key} -> {path}")
    return path


def looks_like_graph_entry_location(code_location: str) -> bool:
    normalized = str(code_location or "").replace("\\", "/").lower()
    return any(
        token in normalized
        for token in [
            "model_runner.py:2619",
            "model_runner.py:2620",
            "cuda_graph_runner.py",
            "replay",
            "forward(",
        ]
    )


def collect_scope_issues(classified: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    issues: list[str] = []
    issue_category_counter: Counter[str] = Counter()
    issue_name_counter: Counter[str] = Counter()
    issue_class_counter: Counter[str] = Counter()
    for stream in classified.get("streams", []):
        for span in stream.get("spans", []):
            span_id = str(span.get("span_id", ""))
            span_name = str(span.get("span_name", ""))
            semantic_class = str(span.get("semantic_class", "")).strip() or "<missing>"
            issue_categories: list[str] = []
            if span.get("scope_class") == "hardware_semantic_candidate" and not span.get("has_stream_id", False):
                issues.append(f"{span_id}:semantic_without_stream_id")
                issue_categories.append("semantic_without_stream_id")
            if not span.get("exclude_from_code_mapping") and match_exclude_rule(span_name, {}):
                issues.append(f"{span_id}:unexpected_semantic_scope")
                issue_categories.append("unexpected_semantic_scope")
            if not span.get("exclude_from_code_mapping") and semantic_class == "runtime_control":
                issues.append(f"{span_id}:runtime_control_in_semantic")
                issue_categories.append("runtime_control_in_semantic")
            if issue_categories:
                issue_name_counter[span_name] += 1
                issue_class_counter[semantic_class] += 1
                for category in issue_categories:
                    issue_category_counter[category] += 1
    summary = {
        "by_category": dict(issue_category_counter),
        "top_span_names": issue_name_counter.most_common(10),
        "top_semantic_classes": issue_class_counter.most_common(10),
    }
    return issues, summary


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    artifacts = state["artifacts"]

    classified = load_json(require_artifact_path(artifacts, "classified_spans_path"))
    mapping = load_json(require_artifact_path(artifacts, "span_code_mapping_path"))
    timeline = load_json(require_artifact_path(artifacts, "stream_span_timeline_path"))
    annotated_trace = load_json(require_artifact_path(artifacts, "annotated_trace_path"))
    graph_plan = load_json(require_artifact_path(artifacts, "graph_execution_plan_path"))
    graph_forward_context = load_json(require_artifact_path(artifacts, "graph_forward_context_path"))
    graph_mapping_targets = load_json(require_artifact_path(artifacts, "graph_mapping_targets_path"))
    graph_span_alignment = load_json(require_artifact_path(artifacts, "graph_span_alignment_path"))
    graph_operator_spans = load_json(require_artifact_path(artifacts, "graph_operator_spans_path"))

    mapping_by_span = {row["span_id"]: row for row in mapping.get("rows", [])}
    events = load_events(annotated_trace)
    trace_event_ref_by_span: dict[str, dict[str, Any]] = {}
    semantic_spans: list[str] = []
    excluded_spans: list[str] = []
    semantic_location_frequency: dict[str, int] = {}
    high_confidence_mapping_count = 0
    graph_entry_line_fallback_count = 0
    for stream in classified.get("streams", []):
        for span in stream.get("spans", []):
            trace_event_ref_by_span[span["span_id"]] = span.get("trace_event_ref", {})
            if span.get("exclude_from_code_mapping"):
                excluded_spans.append(span["span_id"])
            else:
                semantic_spans.append(span["span_id"])

    graph_precision_issues: list[str] = []
    graph_mapping_target_rows = graph_mapping_targets.get("rows", []) if isinstance(graph_mapping_targets.get("rows", []), list) else []
    formal_graph_target_ids = {
        str(row.get("span_id", "")).strip()
        for row in graph_mapping_target_rows
        if isinstance(row, dict) and str(row.get("span_id", "")).strip()
    }
    if graph_precision_expected(graph_plan) and not formal_graph_target_ids:
        graph_precision_issues.append("graph_mapping_targets.json 未提供任何正式 formal graph target。")
    if graph_precision_required(graph_plan, graph_mapping_targets) and graph_plan.get("mapping_granularity") != "per_span_forward_code":
        graph_precision_issues.append("graph_execution_plan.mapping_granularity 不是 per_span_forward_code。")
    if graph_precision_required(graph_plan, graph_mapping_targets) and graph_forward_context.get("mapping_granularity") != "per_span_forward_code":
        graph_precision_issues.append("graph_forward_context.mapping_granularity 不是 per_span_forward_code。")
    alignment_items = extract_graph_alignment_items(graph_span_alignment)
    operator_span_ids = {
        str(item.get("graph_operator_span_id", "")).strip()
        for item in graph_operator_spans.get("rows", [])
        if isinstance(item, dict) and str(item.get("graph_operator_span_id", "")).strip()
    }
    location_kind_breakdown: dict[str, int] = {}
    operator_evidence_breakdown: dict[str, int] = {}
    if graph_precision_required(graph_plan, graph_mapping_targets) and not alignment_items:
        graph_precision_issues.append("graph_span_alignment 缺少正式 span items/rows。")
    if graph_precision_required(graph_plan, graph_mapping_targets) and not operator_span_ids:
        graph_precision_issues.append("graph_operator_spans.json 缺少正式 operator spans。")
    for index, item in enumerate(alignment_items):
        location_kind = str(item.get("location_kind", "")).strip()
        operator_evidence_kind = str(item.get("operator_evidence_kind", "")).strip()
        requires_further_drilldown = item.get("requires_further_drilldown")
        graph_operator_span_id = str(item.get("graph_operator_span_id", "")).strip()
        location_kind_breakdown[location_kind or "<missing>"] = location_kind_breakdown.get(location_kind or "<missing>", 0) + 1
        operator_evidence_breakdown[operator_evidence_kind or "<missing>"] = (
            operator_evidence_breakdown.get(operator_evidence_kind or "<missing>", 0) + 1
        )
        if not graph_operator_span_id:
            graph_precision_issues.append(f"graph_span_alignment[{index}] 缺少 graph_operator_span_id")
        elif graph_operator_span_id not in operator_span_ids:
            graph_precision_issues.append(
                f"graph_span_alignment[{index}] graph_operator_span_id 无法回溯: {graph_operator_span_id}"
            )
        if location_kind != "operator_call":
            graph_precision_issues.append(f"graph_span_alignment[{index}] location_kind={location_kind or '<missing>'}")
        if requires_further_drilldown is not False:
            graph_precision_issues.append(
                f"graph_span_alignment[{index}] requires_further_drilldown={requires_further_drilldown!r}"
            )
        if not operator_evidence_kind:
            graph_precision_issues.append(f"graph_span_alignment[{index}] 缺少 operator_evidence_kind")

    mapping_complete = True
    annotated_trace_ok = True
    no_top_level_code_location = True
    excluded_span_clean = True
    for span_id in semantic_spans:
        row = mapping_by_span.get(span_id)
        if not row or not validate_code_location(str(row.get("code_location", ""))):
            mapping_complete = False
            continue
        code_location = str(row.get("code_location", "")).strip()
        semantic_location_frequency[code_location] = semantic_location_frequency.get(code_location, 0) + 1
        if str(row.get("confidence", "")).strip() in {"high", "medium"}:
            high_confidence_mapping_count += 1
        if looks_like_graph_entry_location(code_location):
            graph_entry_line_fallback_count += 1
        ref = trace_event_ref_by_span.get(span_id, {})
        event_index = ref.get("trace_event_index")
        if not isinstance(event_index, int) or not (0 <= event_index < len(events)):
            annotated_trace_ok = False
            continue
        event = events[event_index]
        if str(event.get("code_location", "")).strip():
            no_top_level_code_location = False
        args_dict = event.get("args", {})
        if not isinstance(args_dict, dict) or str(args_dict.get("code_location", "")).strip() != str(row.get("code_location", "")).strip():
            annotated_trace_ok = False
    for span_id in excluded_spans:
        row = mapping_by_span.get(span_id)
        if row and str(row.get("code_location", "")).strip():
            excluded_span_clean = False

    timeline_order_stable = True
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
            timeline_order_stable = False
            break

    checks = {
        "mapping_complete": mapping_complete,
        "annotated_trace_args_code_location_ok": annotated_trace_ok,
        "no_top_level_code_location": no_top_level_code_location,
        "excluded_span_clean": excluded_span_clean,
        "timeline_order_stable": timeline_order_stable,
        "graph_precision_satisfied": not graph_precision_issues,
    }
    coverage = mapping.get("coverage", {})
    unresolved_semantic_span_count = coverage.get("unresolved_semantic_span_count")
    if unresolved_semantic_span_count is None:
        unresolved_semantic_span_count = coverage.get("unmapped_semantic_span_count", 0)
    expected_coverage_fields = {
        "total_span_count",
        "semantic_span_count",
        "excluded_span_count",
        "mapped_span_count",
        "unresolved_semantic_span_count",
        "low_confidence_span_count",
    }
    missing_coverage_fields = sorted(expected_coverage_fields - set(coverage.keys()))
    scope_summary = classified.get("scope_summary", {})
    scope_issues, scope_issue_summary = collect_scope_issues(classified)
    checks["scope_gate_sane"] = not scope_issues
    checks["coverage_schema_consistent"] = not missing_coverage_fields
    status = "passed" if all(checks.values()) else "failed"
    warnings: list[str] = []
    top_repeated_code_location = ""
    top_repeated_code_location_count = 0
    if semantic_location_frequency:
        top_repeated_code_location, top_repeated_code_location_count = max(
            semantic_location_frequency.items(),
            key=lambda item: item[1],
        )
    if int(unresolved_semantic_span_count) > 0:
        warnings.append("coverage 显示仍有未映射语义 span。")
    if missing_coverage_fields:
        warnings.append(f"coverage 缺少字段: {missing_coverage_fields}")
    if scope_issues:
        warnings.append(f"scope 规则仍存在异常 span: {scope_issues[:10]}")
    if graph_precision_issues:
        warnings.extend(graph_precision_issues)

    result = {
        "status": status,
        "coverage": {
            **coverage,
            "unresolved_semantic_span_count": int(unresolved_semantic_span_count),
        },
        "mapping_quality": {
            "semantic_span_total": len(semantic_spans),
            "mapped_semantic_span_count": sum(1 for span_id in semantic_spans if validate_code_location(str(mapping_by_span.get(span_id, {}).get("code_location", "")))),
            "high_confidence_mapping_count": high_confidence_mapping_count,
            "graph_entry_line_fallback_count": graph_entry_line_fallback_count,
            "top_repeated_code_location": top_repeated_code_location,
            "top_repeated_code_location_count": top_repeated_code_location_count,
        },
        "graph_operator_span_summary": {
            "formal_graph_target_count": len(formal_graph_target_ids),
            "graph_operator_span_count": len(operator_span_ids),
            "aligned_formal_graph_target_count": len(
                {
                    str(item.get("span_id", "")).strip()
                    for item in alignment_items
                    if str(item.get("span_id", "")).strip() in formal_graph_target_ids
                    and str(item.get("location_kind", "")).strip() == "operator_call"
                    and item.get("requires_further_drilldown") is False
                }
            ),
            "aligned_graph_operator_span_count": sum(
                1
                for item in alignment_items
                if str(item.get("graph_operator_span_id", "")).strip() in operator_span_ids
                and str(item.get("location_kind", "")).strip() == "operator_call"
                and item.get("requires_further_drilldown") is False
            ),
        },
        "scope_summary": scope_summary,
        "scope_issues": scope_issues,
        "scope_issue_summary": scope_issue_summary,
        "graph_precision_issues": graph_precision_issues,
        "graph_location_kind_breakdown": location_kind_breakdown,
        "graph_operator_evidence_breakdown": operator_evidence_breakdown,
        "checks": checks,
        "warnings": warnings,
    }

    result_path = workspace_dir / "output" / "validation_result.json"
    dump_json(result_path, result)
    report_path = workspace_dir / "output" / "validation_report.md"
    report_lines = [
        "# Step 7 Validation Report",
        "",
        f"- Status: {status}",
        f"- Coverage: {result['coverage']}",
        f"- Mapping Quality: {result['mapping_quality']}",
        f"- Checks: {checks}",
    ]
    if graph_precision_issues:
        report_lines.append(f"- Graph precision issues: {graph_precision_issues}")
    if warnings:
        report_lines.append(f"- Warnings: {warnings}")
    write_text(report_path, "\n".join(report_lines) + "\n")

    state["artifacts"]["validation_result_path"] = str(result_path)
    state["flags"]["validation_passed"] = status == "passed"
    save_state(workspace_dir, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
