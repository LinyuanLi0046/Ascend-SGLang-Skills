from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any

from workflow_common import dump_json, iter_trace_events, load_json, load_state, resolve_artifact_path, save_state


TRACE_SPAN_BUILD_POLICY = "x_only"
COMMUNICATION_KEYWORDS = [
    "hcom",
    "hccl",
    "allreduce",
    "all_reduce",
    "broadcast",
    "reduce_scatter",
    "reducescatter",
    "allgather",
    "all_gather",
    "send",
    "recv",
    "collective",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建 timeline_index.json。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def parse_int(value: str | int | None, default: int = 0) -> int:
    if value is None or value == "":
        return default
    return int(str(value))


def parse_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "." in text:
        return int(float(text))
    return int(text)


def parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def first_non_empty(row: dict[str, str], keys: list[str], default: str = "") -> str:
    for key in keys:
        value = row.get(key, "").strip()
        if value:
            return value
    return default


def parse_ns_value(value: Any) -> int | None:
    parsed = parse_decimal(value)
    if parsed is None:
        return None
    return int(parsed.to_integral_value(rounding=ROUND_DOWN))


def parse_us_to_ns(value: Any) -> int | None:
    parsed = parse_decimal(value)
    if parsed is None:
        return None
    return int((parsed * Decimal(1000)).to_integral_value(rounding=ROUND_DOWN))


def parse_trace_value_to_ns(value: Any, trace_unit: str) -> int | None:
    if trace_unit == "ns":
        return parse_ns_value(value)
    if trace_unit == "us":
        return parse_us_to_ns(value)
    raise ValueError(f"不支持的 trace_unit: {trace_unit}")


def detect_trace_unit(trace_slice_path: Path) -> str:
    payload = load_json(trace_slice_path)
    if isinstance(payload, dict):
        slice_info = payload.get("_slice_info", {})
        if isinstance(slice_info, dict):
            unit = str(slice_info.get("trace_unit", "")).strip().lower()
            if unit in {"us", "ns"}:
                return unit
        for key in ("displayTimeUnit", "trace_unit"):
            unit = str(payload.get(key, "")).strip().lower()
            if unit in {"us", "ns"}:
                return unit
    return "us"


def normalize_stream_id(stream_id: Any) -> str:
    text = str(stream_id or "").strip()
    return text or "unknown"


def build_task_compound_id(stream_id: str, task_id: str) -> str:
    return f"{normalize_stream_id(stream_id)}::{task_id}"


def load_trace_events(trace_slice_path: Path) -> list[dict[str, Any]]:
    hardware_events: list[dict[str, Any]] = []
    trace_unit = detect_trace_unit(trace_slice_path)
    for index, event in enumerate(iter_trace_events(trace_slice_path)):
        if event.get("ph") != "X":
            continue
        if "dur" not in event or "ts" not in event:
            continue
        start_ns = parse_trace_value_to_ns(event["ts"], trace_unit)
        dur_ns = parse_trace_value_to_ns(event["dur"], trace_unit)
        if start_ns is None or dur_ns is None:
            continue
        hardware_events.append(
            {
                "span_id": f"s_{len(hardware_events) + 1:06d}",
                "trace_event_index": index,
                "pid": event.get("pid", 0),
                "tid": str(event.get("tid", "")),
                "name": str(event.get("name", "")),
                "start_ns": start_ns,
                "dur_ns": dur_ns,
            }
        )
    for event in hardware_events:
        event["end_ns"] = event["start_ns"] + event["dur_ns"]
        event["candidate_stream_ids"] = [event["tid"]] if event["tid"] else []
    return hardware_events


def first_parsed_ns(row: dict[str, str], ns_keys: list[str], us_keys: list[str]) -> int | None:
    for key in ns_keys:
        value = row.get(key)
        parsed = parse_ns_value(value)
        if parsed is not None:
            return parsed
    for key in us_keys:
        value = row.get(key)
        if value is None or str(value).strip() == "":
            continue
        parsed = parse_us_to_ns(value)
        if parsed is not None:
            return parsed
    return None


def extract_row_time_bounds(row: dict[str, str]) -> tuple[int | None, int | None]:
    start_ns = first_parsed_ns(
        row,
        ["start_ns", "derived_start_ns"],
        ["effective_start_us", "task_start(us)", "Task Start Time(us)", "Start Time(us)"],
    )
    end_ns = first_parsed_ns(
        row,
        ["end_ns", "derived_end_ns"],
        ["effective_end_us", "task_stop(us)"],
    )
    dur_ns = first_parsed_ns(
        row,
        ["dur_ns", "derived_dur_ns"],
        ["effective_duration_us", "task_time(us)", "Task Duration(us)", "Duration(us)"],
    )
    if start_ns is None and end_ns is not None and dur_ns is not None:
        start_ns = max(0, end_ns - dur_ns)
    if end_ns is None and start_ns is not None and dur_ns is not None:
        end_ns = start_ns + dur_ns
    return start_ns, end_ns


def build_streams(
    trace_spans: list[dict[str, Any]],
    kernel_rows: list[dict[str, str]],
    task_rows: list[dict[str, str]],
    op_rows: list[dict[str, str]],
) -> list[dict[str, Any]]:
    streams: dict[str, dict[str, Any]] = {}
    rows_by_stream: dict[str, list[dict[str, str]]] = defaultdict(list)

    def touch_stream(stream_id: str, start_ns: int, end_ns: int) -> None:
        slot = streams.setdefault(
            stream_id,
            {
                "stream_id": stream_id,
                "device_id": "0",
                "first_seen_ns": start_ns,
                "last_seen_ns": end_ns,
                "candidate_stream_role": "unknown",
                "stream_role_evidence": [],
                "task_count": 0,
                "trace_span_count": 0,
            },
        )
        slot["first_seen_ns"] = min(slot["first_seen_ns"], start_ns)
        slot["last_seen_ns"] = max(slot["last_seen_ns"], end_ns)

    for span in trace_spans:
        stream_id = normalize_stream_id(span["candidate_stream_ids"][0] if span["candidate_stream_ids"] else "unknown")
        touch_stream(stream_id, span["start_ns"], span["end_ns"])
        streams[stream_id]["trace_span_count"] += 1

    for row in kernel_rows + task_rows + op_rows:
        stream_id = normalize_stream_id(first_non_empty(row, ["Stream ID", "stream_id"], "unknown"))
        rows_by_stream[stream_id].append(row)
        start_ns, end_ns = extract_row_time_bounds(row)
        if start_ns is None and end_ns is None:
            continue
        if start_ns is None:
            start_ns = end_ns
        if end_ns is None:
            end_ns = start_ns
        touch_stream(stream_id, start_ns, end_ns)

    by_stream_task_count = defaultdict(set)
    for stream_id, stream_rows in rows_by_stream.items():
        for row in stream_rows:
            task_id = first_non_empty(row, ["Task ID", "Task Id", "task_id"])
            if task_id:
                by_stream_task_count[stream_id].add(task_id)
    for stream_id, task_ids in by_stream_task_count.items():
        streams[stream_id]["task_count"] = len(task_ids)
    for stream_id, slot in streams.items():
        stream_rows = rows_by_stream.get(stream_id, [])
        joined_text = " ".join(
            first_non_empty(row, ["Op Name", "Name", "Task Type"]).lower()
            for row in stream_rows
        )
        if any(keyword in joined_text for keyword in COMMUNICATION_KEYWORDS):
            slot["candidate_stream_role"] = "communication"
            slot["stream_role_evidence"] = ["matched communication op keywords"]
        elif slot["trace_span_count"] > 0 or slot["task_count"] > 0:
            slot["candidate_stream_role"] = "compute"
            slot["stream_role_evidence"] = ["observed hardware spans/tasks in selected window"]
    return [streams[key] for key in sorted(streams.keys())]


def build_tasks(
    kernel_rows: list[dict[str, str]],
    task_time_rows: list[dict[str, str]],
    op_summary_rows: list[dict[str, str]],
) -> list[dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}

    def ensure_task(task_id: str, stream_id: str, start_ns: int | None, end_ns: int | None) -> dict[str, Any]:
        normalized_stream_id = normalize_stream_id(stream_id)
        compound_id = build_task_compound_id(normalized_stream_id, task_id)
        slot = tasks.setdefault(
            compound_id,
            {
                "task_compound_id": compound_id,
                "task_id": task_id,
                "stream_id": normalized_stream_id,
                "kernel_row_ids": [],
                "task_time_row_ids": [],
                "op_summary_row_ids": [],
                "start_ns": start_ns,
                "end_ns": end_ns,
                "task_type": "",
                "kernel_name": "",
                "alignment_confidence": "low",
                "alignment_basis": [],
                "neighbor_prev_id": "",
                "neighbor_next_id": "",
            },
        )
        if start_ns is not None:
            if slot["start_ns"] is None:
                slot["start_ns"] = start_ns
            else:
                slot["start_ns"] = min(slot["start_ns"], start_ns)
        if end_ns is not None:
            if slot["end_ns"] is None:
                slot["end_ns"] = end_ns
            else:
                slot["end_ns"] = max(slot["end_ns"], end_ns)
        if not slot["stream_id"] and normalized_stream_id:
            slot["stream_id"] = normalized_stream_id
        return slot

    for index, row in enumerate(kernel_rows, start=1):
        task_id = first_non_empty(row, ["Task ID", "Task Id"], f"kern_{index:06d}")
        start_ns, end_ns = extract_row_time_bounds(row)
        slot = ensure_task(task_id, first_non_empty(row, ["Stream ID", "stream_id"], "unknown"), start_ns, end_ns)
        slot["kernel_row_ids"].append(row.get("slice_row_id") or f"kernel_{index:06d}")
        slot["kernel_name"] = first_non_empty(row, ["Name", "Kernel Name"], slot["kernel_name"])
        slot["alignment_basis"] = sorted(set(slot["alignment_basis"]) | {"kernel"})

    for index, row in enumerate(task_time_rows, start=1):
        task_id = first_non_empty(row, ["Task ID", "Task Id"], f"tasktime_{index:06d}")
        start_ns, end_ns = extract_row_time_bounds(row)
        slot = ensure_task(task_id, first_non_empty(row, ["Stream ID", "stream_id"], "unknown"), start_ns, end_ns)
        slot["task_time_row_ids"].append(row.get("slice_row_id") or f"task_time_{index:06d}")
        slot["task_type"] = first_non_empty(row, ["Task Type", "task_type"], slot["task_type"])
        slot["alignment_basis"] = sorted(set(slot["alignment_basis"]) | {"task_time"})

    for index, row in enumerate(op_summary_rows, start=1):
        task_id = first_non_empty(row, ["Task ID", "Task Id"], f"opsummary_{index:06d}")
        start_ns, end_ns = extract_row_time_bounds(row)
        slot = ensure_task(task_id, first_non_empty(row, ["Stream ID", "stream_id"], "unknown"), start_ns, end_ns)
        slot["op_summary_row_ids"].append(row.get("slice_row_id") or f"op_summary_{index:06d}")
        slot["task_type"] = first_non_empty(row, ["Task Type"], slot["task_type"])
        slot["alignment_basis"] = sorted(set(slot["alignment_basis"]) | {"op_summary"})
    normalized_tasks: list[dict[str, Any]] = []
    for key in sorted(tasks.keys()):
        slot = dict(tasks[key])
        start_ns = slot.get("start_ns")
        end_ns = slot.get("end_ns")
        if start_ns is None and end_ns is not None:
            start_ns = end_ns
        if end_ns is None and start_ns is not None:
            end_ns = start_ns
        slot["start_ns"] = int(start_ns or 0)
        slot["end_ns"] = int(end_ns or slot["start_ns"])
        basis = set(slot.get("alignment_basis", []))
        if {"kernel", "task_time", "op_summary"}.issubset(basis):
            slot["alignment_confidence"] = "high"
        elif len(basis) >= 2:
            slot["alignment_confidence"] = "medium"
        else:
            slot["alignment_confidence"] = "low"
        normalized_tasks.append(slot)
    annotate_neighbors(normalized_tasks, "stream_id", "task_compound_id")
    return normalized_tasks


def build_ops(op_summary_rows: list[dict[str, str]], operator_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = []
    operator_rows_by_match_id = {
        first_non_empty(row, ["matched_op_summary_row_id"]): row
        for row in operator_rows
        if first_non_empty(row, ["matched_op_summary_row_id"])
    }
    task_has_call_stack = {
        build_task_compound_id(
            normalize_stream_id(first_non_empty(row, ["Stream ID", "stream_id"], "unknown")),
            first_non_empty(row, ["Task ID", "Task Id"]),
        ): True
        for row in operator_rows
        if first_non_empty(row, ["Task ID", "Task Id"]) and bool(row.get("Call Stack", "").strip())
    }
    for row in op_summary_rows:
        op_row_id = f"op_{row.get('slice_row_id', '000000')}"
        start_ns, end_ns = extract_row_time_bounds(row)
        start_ns = int(start_ns or 0)
        end_ns = int(end_ns if end_ns is not None else start_ns)
        task_id = first_non_empty(row, ["Task ID", "Task Id"])
        stream_id = normalize_stream_id(first_non_empty(row, ["Stream ID", "stream_id"], "unknown"))
        task_compound_id = build_task_compound_id(stream_id, task_id) if task_id else ""
        call_stack_present = bool(task_compound_id and task_has_call_stack.get(task_compound_id))
        operator_row = operator_rows_by_match_id.get(row.get("slice_row_id", ""))
        ops.append(
            {
                "op_row_id": op_row_id,
                "op_name": first_non_empty(row, ["Op Name", "Name"]),
                "stream_id": stream_id,
                "task_id": task_id,
                "task_compound_id": task_compound_id,
                "start_ns": start_ns,
                "end_ns": end_ns,
                "call_stack_present": call_stack_present,
                "time_source": first_non_empty(row, ["time_source"], "op_summary"),
                "time_match_confidence": first_non_empty(
                    operator_row or row, ["time_match_confidence"], "high"
                ),
                "alignment_confidence": first_non_empty(
                    operator_row or row, ["time_match_confidence"], "high"
                ),
                "alignment_basis": first_non_empty(operator_row or row, ["time_match_basis"], "op_summary"),
                "neighbor_prev_id": "",
                "neighbor_next_id": "",
            }
        )
    annotate_neighbors(ops, "stream_id", "op_row_id")
    return ops


def annotate_neighbors(rows: list[dict[str, Any]], stream_key: str, id_key: str) -> None:
    rows_by_stream: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_stream[str(row.get(stream_key, "unknown"))].append(row)
    for stream_rows in rows_by_stream.values():
        stream_rows.sort(key=lambda item: (int(item.get("start_ns", 0)), int(item.get("end_ns", 0)), str(item.get(id_key, ""))))
        for index, row in enumerate(stream_rows):
            row["neighbor_prev_id"] = str(stream_rows[index - 1].get(id_key, "")) if index > 0 else ""
            row["neighbor_next_id"] = str(stream_rows[index + 1].get(id_key, "")) if index + 1 < len(stream_rows) else ""


def build_links(tasks: list[dict[str, Any]], trace_spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    spans_by_stream: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for span in trace_spans:
        for stream_id in span.get("candidate_stream_ids", []):
            spans_by_stream[str(stream_id)].append(span)
    for task in tasks:
        for span in spans_by_stream.get(str(task["stream_id"]), []):
            overlap = min(task["end_ns"], span["end_ns"]) - max(task["start_ns"], span["start_ns"])
            if overlap <= 0:
                continue
            shorter = max(1, min(task["end_ns"] - task["start_ns"], span["dur_ns"]))
            score = round(overlap / shorter, 4)
            links.append(
                {
                    "src_type": "task",
                    "src_id": task["task_compound_id"],
                    "src_task_id": task["task_id"],
                    "dst_type": "span",
                    "dst_id": span["span_id"],
                    "link_type": "time_overlap",
                    "score": score,
                }
            )
    return links


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    trace_slice_path = resolve_artifact_path(workspace_dir, state, "trace_slice_path", "artifacts/slices/trace_slice.json")
    kernel_slice_path = resolve_artifact_path(
        workspace_dir, state, "kernel_slice_path", "artifacts/slices/kernel_details_slice.csv"
    )
    operator_slice_path = resolve_artifact_path(
        workspace_dir, state, "operator_slice_path", "artifacts/slices/operator_details_slice.csv"
    )
    task_time_slice_path = resolve_artifact_path(
        workspace_dir, state, "task_time_slice_path", "artifacts/slices/task_time_slice.csv"
    )
    op_summary_slice_path = resolve_artifact_path(
        workspace_dir, state, "op_summary_slice_path", "artifacts/slices/op_summary_slice.csv"
    )

    trace_spans = load_trace_events(trace_slice_path)
    kernel_rows = load_csv_rows(kernel_slice_path)
    operator_rows = load_csv_rows(operator_slice_path)
    task_time_rows = load_csv_rows(task_time_slice_path)
    op_summary_rows = load_csv_rows(op_summary_slice_path)

    tasks = build_tasks(kernel_rows, task_time_rows, op_summary_rows)
    ops = build_ops(op_summary_rows, operator_rows)
    streams = build_streams(trace_spans, kernel_rows, task_time_rows, op_summary_rows + operator_rows)
    links = build_links(tasks, trace_spans)

    output = {
        "schema_version": "timeline_index_v1",
        "trace_span_build_policy": TRACE_SPAN_BUILD_POLICY,
        "trace_span_source_ph": "X",
        "trace_non_span_ph_policy": "preserve_in_trace_slice_only",
        "trace_time_unit_policy": "trace_slice_metadata_first_then_us_default",
        "task_identity_policy": "task_compound_id=stream_id::task_id; raw task_id preserved",
        "window_start_ns": int(state["inputs"]["window_start_ns"]),
        "window_end_ns": int(state["inputs"]["window_end_ns"]),
        "streams": streams,
        "tasks": tasks,
        "ops": ops,
        "trace_spans": trace_spans,
        "links": links,
    }
    output_path = workspace_dir / "artifacts" / "index" / "timeline_index.json"
    dump_json(output_path, output)
    state["artifacts"]["trace_slice_path"] = str(trace_slice_path)
    state["artifacts"]["kernel_slice_path"] = str(kernel_slice_path)
    state["artifacts"]["operator_slice_path"] = str(operator_slice_path)
    state["artifacts"]["task_time_slice_path"] = str(task_time_slice_path)
    state["artifacts"]["op_summary_slice_path"] = str(op_summary_slice_path)
    state["artifacts"]["timeline_index_path"] = str(output_path)
    state["flags"]["timeline_index_built"] = True
    save_state(workspace_dir, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
