from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Any

from workflow_common import dump_json, load_json, load_state, save_state
from span_scope_rules import classify_scope


EXCLUDE_KEYWORDS = [
    "CAPTURE_WAIT",
    "EVENT_WAIT",
    "EVENT_RESET",
    "NOP",
    "WAIT",
    "SYNC",
]

COMM_KEYWORDS = [
    "allreduce",
    "all_reduce",
    "reduce_scatter",
    "allgather",
    "broadcast",
    "communication",
    "hcom",
]

RUNTIME_KEYWORDS = [
    "graph",
    "replay",
    "scheduler",
    "verify_done",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成初版 classified_spans.json。")
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument("--output-path", default="")
    parser.add_argument("--write-state", default="true")
    return parser


def normalized(value: str) -> str:
    return value.strip().lower()


def overlaps(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    return min(end_a, end_b) - max(start_a, start_b) > 0


def build_related_maps(
    timeline_index: dict[str, Any]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    tasks_by_stream: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ops_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in timeline_index.get("tasks", []):
        tasks_by_stream[str(task.get("stream_id", "unknown"))].append(task)
    for op in timeline_index.get("ops", []):
        task_key = str(op.get("task_compound_id", "") or op.get("task_id", ""))
        if task_key:
            ops_by_task[task_key].append(op)
    return tasks_by_stream, ops_by_task


def load_trace_events(path: Path) -> list[dict[str, Any]]:
    payload = load_json(path)
    if isinstance(payload, dict):
        events = payload.get("traceEvents") or payload.get("events") or []
    else:
        events = payload
    return events if isinstance(events, list) else []


def classify_task_type(task_type: str, span_name: str) -> str:
    task_type_l = normalized(task_type)
    span_name_l = normalized(span_name)
    if span_name.strip() == "MODEL_EXECUTE":
        return "model_execute_marker"
    if any(keyword in span_name for keyword in EXCLUDE_KEYWORDS):
        return "wait_sync"
    if "communication" in task_type_l or any(keyword in span_name_l for keyword in COMM_KEYWORDS):
        return "communication"
    if "aic" in task_type_l or "vector" in task_type_l or "core" in task_type_l:
        return "compute"
    if any(keyword in span_name_l for keyword in RUNTIME_KEYWORDS):
        return "runtime_control"
    return "unknown"


def infer_stream_role(stream_id: str, spans: list[dict[str, Any]]) -> str:
    counts = defaultdict(int)
    for span in spans:
        if span.get("scope_class") != "hardware_semantic_candidate":
            continue
        counts[span["semantic_class"]] += 1
    if counts["communication"] and not counts["compute"]:
        return "communication"
    if counts["compute"] and counts["communication"]:
        return "mixed"
    if counts["compute"]:
        return "compute"
    if counts["model_execute_marker"]:
        return "runtime_control"
    if counts["runtime_control"]:
        return "runtime_control"
    return "unknown"


def apply_semantic_scope_guard(scope_info: dict[str, Any], semantic_class: str, span_name: str) -> dict[str, Any]:
    if scope_info.get("matched_scope_rule_source") == "force_include":
        return scope_info
    if semantic_class == "runtime_control":
        return {
            **scope_info,
            "scope_class": "hardware_excluded",
            "exclude_from_code_mapping": True,
            "exclude_reason": f"runtime control span should not enter semantic mapping: {span_name}",
            "matched_scope_rule_id": "runtime_control_semantic_guard",
            "matched_scope_rule_source": "semantic_guard",
        }
    return scope_info


def build_parallel_groups(spans: list[dict[str, Any]]) -> dict[str, str]:
    ordered = sorted(spans, key=lambda item: (item["start_ns"], item["end_ns"], item["stream_id"]))
    active: list[tuple[str, int]] = []
    result: dict[str, str] = {}
    group_index = 0
    for span in ordered:
        active = [(group_id, end_ns) for group_id, end_ns in active if end_ns > span["start_ns"]]
        if active:
            group_id = active[0][0]
        else:
            group_index += 1
            group_id = f"pg_{group_index:05d}"
        active.append((group_id, span["end_ns"]))
        result[span["span_id"]] = group_id
    return result


def parse_bool_flag(value: str) -> bool:
    normalized_value = str(value).strip().lower()
    if normalized_value in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized_value in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"无法解析布尔参数: {value!r}")


def build_classified_spans_payload(
    timeline_index: dict[str, Any],
    trace_events: list[dict[str, Any]],
) -> dict[str, Any]:
    tasks_by_stream, ops_by_task = build_related_maps(timeline_index)

    stream_spans: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_spans: list[dict[str, Any]] = []
    scope_summary = {
        "non_hardware_span_count": 0,
        "hardware_excluded_count": 0,
        "hardware_semantic_candidate_count": 0,
    }
    for trace_span in timeline_index.get("trace_spans", []):
        stream_id = str((trace_span.get("candidate_stream_ids") or ["unknown"])[0])
        related_tasks = [
            task for task in tasks_by_stream.get(stream_id, [])
            if overlaps(trace_span["start_ns"], trace_span["end_ns"], task["start_ns"], task["end_ns"])
        ]
        related_task_ids = [str(task.get("task_id", "")) for task in related_tasks if task.get("task_id", "")]
        related_task_compound_ids = [
            str(task.get("task_compound_id", "") or task.get("task_id", ""))
            for task in related_tasks
            if task.get("task_compound_id", "") or task.get("task_id", "")
        ]
        related_task_types = [str(task.get("task_type", "")) for task in related_tasks if task.get("task_type", "")]
        related_ops = []
        for task in related_tasks:
            task_key = str(task.get("task_compound_id", "") or task.get("task_id", ""))
            related_ops.extend(ops_by_task.get(task_key, []))
        related_op_ids = [str(op.get("op_row_id", "")) for op in related_ops if op.get("op_row_id", "")]
        related_op_names = [str(op.get("op_name", "")) for op in related_ops if op.get("op_name", "")]

        trace_event_index = int(trace_span.get("trace_event_index", -1))
        raw_event = trace_events[trace_event_index] if 0 <= trace_event_index < len(trace_events) else {}
        event_args = raw_event.get("args", {}) if isinstance(raw_event, dict) else {}
        if not isinstance(event_args, dict):
            event_args = {}
        span_name = str(trace_span.get("name", ""))
        scope_info = classify_scope(span_name, event_args, stream_id)
        semantic_class = classify_task_type(" ".join(related_task_types), span_name)
        scope_info = apply_semantic_scope_guard(scope_info, semantic_class, span_name)
        exclude = bool(scope_info["exclude_from_code_mapping"]) or semantic_class == "wait_sync"
        exclude_reason = str(scope_info.get("exclude_reason", ""))
        if not exclude_reason and semantic_class == "wait_sync":
            exclude_reason = "matched wait/sync keywords"
        semantic_confidence = "high" if scope_info["scope_class"] == "hardware_semantic_candidate" else "low"
        scope_summary[f"{scope_info['scope_class']}_count"] += 1
        record = {
            "span_id": trace_span["span_id"],
            "stream_id": stream_id,
            "start_ns": trace_span["start_ns"],
            "end_ns": trace_span["end_ns"],
            "dur_ns": trace_span["dur_ns"],
            "span_name": trace_span.get("name", ""),
            "semantic_class": semantic_class,
            "has_stream_id": bool(scope_info["has_stream_id"]),
            "scope_class": scope_info["scope_class"],
            "matched_scope_rule_id": scope_info["matched_scope_rule_id"],
            "matched_scope_rule_source": scope_info["matched_scope_rule_source"],
            "semantic_confidence": semantic_confidence,
            "stream_role": "unknown",
            "exclude_from_code_mapping": exclude,
            "exclude_reason": exclude_reason,
            "task_ids": related_task_ids,
            "task_compound_ids": related_task_compound_ids,
            "op_row_ids": related_op_ids,
            "related_task_types": related_task_types,
            "related_op_names": related_op_names,
            "external_mapping_required": False,
            "trace_event_ref": {
                "trace_event_index": trace_span.get("trace_event_index", -1),
                "pid": trace_span.get("pid", 0),
                "tid": trace_span.get("tid", ""),
                "name": trace_span.get("name", ""),
            },
        }
        stream_spans[stream_id].append(record)
        all_spans.append(record)

    parallel_groups = build_parallel_groups(all_spans)
    streams_payload = []
    semantic_span_count = 0
    excluded_span_count = 0
    for stream_id in sorted(stream_spans.keys()):
        spans = sorted(stream_spans[stream_id], key=lambda item: (item["start_ns"], item["end_ns"], item["span_id"]))
        role = infer_stream_role(stream_id, spans)
        for span in spans:
            span["external_mapping_required"] = bool(not span["exclude_from_code_mapping"])
            span["stream_role"] = role
            span["parallel_group"] = parallel_groups.get(span["span_id"], "")
            if span["exclude_from_code_mapping"]:
                excluded_span_count += 1
            else:
                semantic_span_count += 1
        streams_payload.append(
            {
                "stream_id": stream_id,
                "stream_role": role,
                "spans": spans,
            }
        )

    output = {
        "schema_version": "classified_spans_v1",
        "streams": streams_payload,
        "span_count": len(all_spans),
        "semantic_span_count": semantic_span_count,
        "excluded_span_count": excluded_span_count,
        "scope_summary": scope_summary,
    }
    return output


def classify_spans_for_workspace(
    workspace_dir: Path,
    *,
    output_path: Path | None = None,
    write_state: bool = True,
) -> dict[str, Any]:
    state = load_state(workspace_dir)
    timeline_index = load_json(Path(state["artifacts"]["timeline_index_path"]))
    trace_events = load_trace_events(Path(state["artifacts"]["trace_slice_path"]))
    output = build_classified_spans_payload(timeline_index, trace_events)
    resolved_output_path = output_path or (workspace_dir / "artifacts" / "classification" / "classified_spans.json")
    dump_json(resolved_output_path, output)
    if write_state:
        state["artifacts"]["classified_spans_path"] = str(resolved_output_path)
        state["flags"]["classification_done"] = True
        state["flags"]["hardware_scope_classified"] = True
        save_state(workspace_dir, state)
    return output


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    output_path = Path(args.output_path) if str(args.output_path).strip() else None
    write_state = parse_bool_flag(args.write_state)
    classify_spans_for_workspace(workspace_dir, output_path=output_path, write_state=write_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
