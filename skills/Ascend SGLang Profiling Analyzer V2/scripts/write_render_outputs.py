from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from workflow_common import dump_json, load_json, load_state, write_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 Step 6 正式结果文件。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def load_events(path: Path) -> list[dict[str, Any]]:
    payload = load_json(path)
    if isinstance(payload, dict):
        events = payload.get("traceEvents") or payload.get("events") or []
    else:
        events = payload
    return events if isinstance(events, list) else []


def build_graph_support_summary(mapping_rows: list[dict[str, Any]]) -> dict[str, Any]:
    support_status_breakdown: dict[str, int] = {}
    location_kind_breakdown: dict[str, int] = {}
    retained_candidate_code_location_count = 0
    for row in mapping_rows:
        support_status = str(row.get("graph_alignment_support_status", "")).strip()
        location_kind = str(row.get("graph_alignment_location_kind", "")).strip()
        candidate_code_location = str(row.get("graph_alignment_candidate_code_location", "")).strip()
        if support_status:
            support_status_breakdown[support_status] = support_status_breakdown.get(support_status, 0) + 1
        if location_kind:
            location_kind_breakdown[location_kind] = location_kind_breakdown.get(location_kind, 0) + 1
        if candidate_code_location and support_status and support_status != "final_operator_call":
            retained_candidate_code_location_count += 1
    return {
        "support_status_breakdown": support_status_breakdown,
        "location_kind_breakdown": location_kind_breakdown,
        "retained_candidate_code_location_count": retained_candidate_code_location_count,
    }


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    artifacts = state["artifacts"]

    mapping = load_json(Path(artifacts["span_code_mapping_path"]))
    events = load_events(Path(artifacts["annotated_trace_path"]))
    timeline = load_json(Path(artifacts["stream_span_timeline_path"]))

    mapping_rows = mapping.get("rows", [])
    mapped_event_count = sum(
        1
        for row in mapping_rows
        if not row.get("exclude_from_code_mapping") and str(row.get("code_location", "")).strip()
    )
    args_code_location_count = sum(
        1
        for event in events
        if isinstance(event.get("args"), dict) and str(event["args"].get("code_location", "")).strip()
    )
    top_level_code_location_count = sum(1 for event in events if str(event.get("code_location", "")).strip())

    warnings: list[str] = []
    coverage = mapping.get("coverage", {})
    graph_support_summary = build_graph_support_summary(mapping_rows)
    unresolved_semantic_span_count = coverage.get("unresolved_semantic_span_count")
    if unresolved_semantic_span_count is None:
        unresolved_semantic_span_count = coverage.get("unmapped_semantic_span_count", 0)
    if int(unresolved_semantic_span_count) > 0:
        warnings.append("仍存在未映射的语义 span。")

    result = {
        "status": "passed",
        "step": 6,
        "annotated_trace_stats": {
            "mapped_event_count": mapped_event_count,
            "args_code_location_count": args_code_location_count,
            "top_level_code_location_count": top_level_code_location_count,
        },
        "timeline_stats": {
            "stream_count": len(timeline.get("streams", [])),
            "global_order_count": len(timeline.get("global_order", [])),
        },
        "graph_support_summary": graph_support_summary,
        "warnings": warnings,
    }

    result_path = workspace_dir / "output" / "render_result.json"
    dump_json(result_path, result)
    report_path = workspace_dir / "output" / "render_report.md"
    report_lines = [
        "# Step 6 Render Report",
        "",
        "- Status: passed",
        f"- Mapped event count: {mapped_event_count}",
        f"- args.code_location count: {args_code_location_count}",
        f"- Top-level code_location count: {top_level_code_location_count}",
        f"- Timeline stream count: {len(timeline.get('streams', []))}",
        f"- Global order count: {len(timeline.get('global_order', []))}",
        f"- Graph support summary: {graph_support_summary}",
    ]
    if warnings:
        report_lines.append(f"- Warnings: {warnings}")
    write_text(report_path, "\n".join(report_lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
