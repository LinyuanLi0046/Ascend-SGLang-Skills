from __future__ import annotations

import argparse
import csv
import time
from collections import Counter
from pathlib import Path
from typing import Any

from build_python_tracer_index import build_python_tracer_index_for_workspace
from workflow_common import dump_json, load_json, load_state, resolve_artifact_path, save_state, write_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 Step 1 正式结果文件。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def count_trace_events(path: Path) -> int:
    payload = load_json(path)
    if isinstance(payload, dict):
        events = payload.get("traceEvents") or payload.get("events") or []
    else:
        events = payload
    return len(events) if isinstance(events, list) else 0


def load_trace_events(path: Path) -> list[dict[str, Any]]:
    payload = load_json(path)
    if isinstance(payload, dict):
        events = payload.get("traceEvents") or payload.get("events") or []
    else:
        events = payload
    return events if isinstance(events, list) else []


def _normalized_key(value: str) -> str:
    return "".join(char.lower() for char in value if char.isalnum())


def has_stream_id(event_args: dict[str, Any]) -> bool:
    for key, value in event_args.items():
        if _normalized_key(str(key)) in {"streamid", "physicstreamid"} and str(value).strip():
            return True
    return False


def log(message: str) -> None:
    print(f"[write_preprocess_step1_outputs] {message}", flush=True)


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    step_start = time.perf_counter()
    state = load_state(workspace_dir)
    artifacts = state["artifacts"]

    trace_path = resolve_artifact_path(workspace_dir, state, "trace_slice_path", "artifacts/slices/trace_slice.json")
    kernel_path = resolve_artifact_path(
        workspace_dir, state, "kernel_slice_path", "artifacts/slices/kernel_details_slice.csv"
    )
    operator_path = resolve_artifact_path(
        workspace_dir, state, "operator_slice_path", "artifacts/slices/operator_details_slice.csv"
    )
    task_time_path = resolve_artifact_path(
        workspace_dir, state, "task_time_slice_path", "artifacts/slices/task_time_slice.csv"
    )
    op_summary_path = resolve_artifact_path(
        workspace_dir, state, "op_summary_slice_path", "artifacts/slices/op_summary_slice.csv"
    )

    log("开始加载 Step 1 切片 CSV")
    csv_stage_start = time.perf_counter()
    kernel_rows = load_csv_rows(kernel_path)
    operator_rows = load_csv_rows(operator_path)
    task_time_rows = load_csv_rows(task_time_path)
    op_summary_rows = load_csv_rows(op_summary_path)
    log(
        f"CSV 加载完成，耗时={time.perf_counter() - csv_stage_start:.2f}s "
        f"(kernel={len(kernel_rows)}, operator={len(operator_rows)}, task_time={len(task_time_rows)}, op_summary={len(op_summary_rows)})"
    )
    log("开始构建 python tracer index")
    tracer_stage_start = time.perf_counter()
    python_tracer_index = build_python_tracer_index_for_workspace(workspace_dir)
    log(
        f"python tracer index 构建完成，耗时={time.perf_counter() - tracer_stage_start:.2f}s "
        f"(status={python_tracer_index.get('status')}, total_frames={python_tracer_index.get('stats', {}).get('total_frame_count', 0)})"
    )
    state = load_state(workspace_dir)
    log("开始加载 trace_slice.json 统计信息")
    trace_stage_start = time.perf_counter()
    trace_events = load_trace_events(trace_path)
    log(f"trace_slice.json 加载完成，耗时={time.perf_counter() - trace_stage_start:.2f}s (events={len(trace_events)})")

    slice_counts = {
        "trace_events": len(trace_events),
        "kernel_rows": len(kernel_rows),
        "operator_rows": len(operator_rows),
        "task_time_rows": len(task_time_rows),
        "op_summary_rows": len(op_summary_rows),
    }
    trace_ph_counts = dict(
        sorted(Counter(str(event.get("ph", "")).strip() or "missing" for event in trace_events).items())
    )
    trace_x_events = [
        event
        for event in trace_events
        if event.get("ph") == "X" and "ts" in event and "dur" in event
    ]
    trace_x_summary = {
        "x_event_count": len(trace_x_events),
        "non_x_event_count": max(0, len(trace_events) - len(trace_x_events)),
        "x_with_stream_id_count": sum(
            1
            for event in trace_x_events
            if isinstance(event.get("args", {}), dict) and has_stream_id(event.get("args", {}))
        ),
    }
    time_source_stats = dict(
        sorted(
            Counter((row.get("time_source") or "").strip() or "missing" for row in operator_rows).items()
        )
    )
    warnings: list[str] = []
    if time_source_stats.get("raw_operator_details_without_absolute_time", 0) > 0:
        warnings.append("operator_details.csv 不含绝对开始时间；这些行仅作为调用栈证据保留。")
    if slice_counts["operator_rows"] == 0:
        warnings.append("operator_details_slice.csv 为空。")
    if trace_x_summary["x_event_count"] == 0:
        warnings.append("trace_slice.json 中没有可进入 Step 2 主索引的 X 事件。")
    warnings.extend(python_tracer_index.get("warnings", []))

    python_tracer_summary = {
        "hash_file_present": python_tracer_index["sources"]["hash_file_present"],
        "func_file_present": python_tracer_index["sources"]["func_file_present"],
        "index_built": True,
        "parse_status": python_tracer_index["status"],
        "repo_frame_count": python_tracer_index["stats"]["repo_frame_count"],
        "total_frame_count": python_tracer_index["stats"]["total_frame_count"],
    }

    result = {
        "status": "passed",
        "step": 1,
        "slice_counts": slice_counts,
        "trace_ph_counts": trace_ph_counts,
        "trace_x_summary": trace_x_summary,
        "trace_index_policy": {
            "step2_trace_span_source_ph": "X",
            "non_x_events_preserved_in_trace_slice": True,
        },
        "operator_time_source_stats": time_source_stats,
        "python_tracer_summary": python_tracer_summary,
        "artifacts": {
            "trace_slice_path": str(trace_path),
            "kernel_slice_path": str(kernel_path),
            "operator_slice_path": str(operator_path),
            "task_time_slice_path": str(task_time_path),
            "op_summary_slice_path": str(op_summary_path),
            "python_tracer_index_path": str(state["artifacts"].get("python_tracer_index_path", "")),
        },
        "warnings": warnings,
    }

    result_path = workspace_dir / "output" / "preprocess_step1_result.json"
    log("开始写 preprocess_step1_result.json")
    dump_json(result_path, result)
    report_path = workspace_dir / "output" / "preprocess_step1_report.md"
    report_lines = [
        "# Step 1 Preprocess Report",
        "",
        "- Status: passed",
        f"- Trace events: {slice_counts['trace_events']}",
        f"- Trace ph counts: {trace_ph_counts}",
        f"- Trace X summary: {trace_x_summary}",
        "- Trace index policy: Step 1 keeps non-X trace events for compatibility, while Step 2 only indexes X spans.",
        f"- Kernel rows: {slice_counts['kernel_rows']}",
        f"- Operator rows: {slice_counts['operator_rows']}",
        f"- Task time rows: {slice_counts['task_time_rows']}",
        f"- Op summary rows: {slice_counts['op_summary_rows']}",
        f"- Operator time sources: {time_source_stats}",
        f"- Python tracer summary: {python_tracer_summary}",
    ]
    if warnings:
        report_lines.append(f"- Warnings: {warnings}")
    log("开始写 preprocess_step1_report.md")
    write_text(report_path, "\n".join(report_lines) + "\n")

    state["flags"]["slicing_done"] = True
    state["artifacts"]["trace_slice_path"] = str(trace_path)
    state["artifacts"]["kernel_slice_path"] = str(kernel_path)
    state["artifacts"]["operator_slice_path"] = str(operator_path)
    state["artifacts"]["task_time_slice_path"] = str(task_time_path)
    state["artifacts"]["op_summary_slice_path"] = str(op_summary_path)
    log("开始回写 Step 1 状态位与 artifacts")
    save_state(workspace_dir, state)
    log(f"Step 1 正式结果写出完成，总耗时={time.perf_counter() - step_start:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
