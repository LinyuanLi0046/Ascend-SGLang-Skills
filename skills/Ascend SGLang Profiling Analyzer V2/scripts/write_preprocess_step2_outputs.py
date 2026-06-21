from __future__ import annotations

import argparse
from pathlib import Path

from workflow_common import dump_json, load_json, load_state, resolve_artifact_path, save_state, write_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 Step 2 正式结果文件。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    timeline_path = resolve_artifact_path(workspace_dir, state, "timeline_index_path", "artifacts/index/timeline_index.json")
    timeline = load_json(timeline_path)

    summary = {
        "stream_count": len(timeline.get("streams", [])),
        "task_count": len(timeline.get("tasks", [])),
        "op_count": len(timeline.get("ops", [])),
        "trace_span_count": len(timeline.get("trace_spans", [])),
    }
    trace_index_policy = {
        "trace_span_build_policy": str(timeline.get("trace_span_build_policy", "")).strip() or "unknown",
        "trace_span_source_ph": str(timeline.get("trace_span_source_ph", "")).strip() or "unknown",
        "trace_non_span_ph_policy": str(timeline.get("trace_non_span_ph_policy", "")).strip() or "unknown",
        "trace_time_unit_policy": str(timeline.get("trace_time_unit_policy", "")).strip() or "unknown",
        "task_identity_policy": str(timeline.get("task_identity_policy", "")).strip() or "unknown",
    }
    alignment_summary = {
        "high_confidence_ops": sum(1 for op in timeline.get("ops", []) if op.get("alignment_confidence") == "high"),
        "medium_confidence_ops": sum(1 for op in timeline.get("ops", []) if op.get("alignment_confidence") == "medium"),
        "low_confidence_ops": sum(1 for op in timeline.get("ops", []) if op.get("alignment_confidence") == "low"),
        "unknown_stream_count": sum(
            1 for stream in timeline.get("streams", []) if str(stream.get("stream_id", "")).strip() in {"unknown", ""}
        ),
    }
    warnings: list[str] = []
    if alignment_summary["unknown_stream_count"] > 0:
        warnings.append("timeline_index.json 中仍存在 unknown stream。")
    if alignment_summary["low_confidence_ops"] > 0:
        warnings.append("存在低置信度 operator 时间对齐，后续 Step 4/6 需结合 stack 与 tracer 复核。")
    if trace_index_policy["trace_span_build_policy"] != "x_only":
        warnings.append("timeline_index.json 未显式声明 x_only trace span 策略。")
    if trace_index_policy["trace_time_unit_policy"] == "unknown":
        warnings.append("timeline_index.json 未显式声明 trace 时间单位解析策略。")
    if trace_index_policy["task_identity_policy"] == "unknown":
        warnings.append("timeline_index.json 未显式声明 task 身份键策略。")

    result = {
        "status": "passed",
        "step": 2,
        "timeline_index_summary": summary,
        "trace_index_policy": trace_index_policy,
        "alignment_summary": alignment_summary,
        "artifacts": {
            "timeline_index_path": str(timeline_path),
        },
        "warnings": warnings,
    }

    result_path = workspace_dir / "output" / "preprocess_step2_result.json"
    dump_json(result_path, result)
    report_path = workspace_dir / "output" / "preprocess_step2_report.md"
    report_lines = [
        "# Step 2 Timeline Index Report",
        "",
        "- Status: passed",
        f"- Stream count: {summary['stream_count']}",
        f"- Task count: {summary['task_count']}",
        f"- Op count: {summary['op_count']}",
        f"- Trace span count: {summary['trace_span_count']}",
        f"- Trace index policy: {trace_index_policy}",
        f"- Alignment summary: {alignment_summary}",
    ]
    if warnings:
        report_lines.append(f"- Warnings: {warnings}")
    write_text(report_path, "\n".join(report_lines) + "\n")
    state["artifacts"]["timeline_index_path"] = str(timeline_path)
    state["flags"]["timeline_index_built"] = True
    save_state(workspace_dir, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
