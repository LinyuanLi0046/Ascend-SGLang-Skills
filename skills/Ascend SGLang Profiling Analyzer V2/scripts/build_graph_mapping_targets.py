from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from workflow_common import dump_json, iter_classified_streams, load_json, load_state, save_state


FORMAL_GRAPH_TARGET_SEMANTIC_CLASSES = {"compute", "communication"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="冻结 Step5 formal graph target set。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _window_overlaps_span(window: dict[str, Any], span: dict[str, Any]) -> bool:
    window_start_ns = int(window.get("start_ns", 0) or 0)
    window_end_ns = int(window.get("end_ns", 0) or 0)
    span_start_ns = int(span.get("start_ns", 0) or 0)
    span_end_ns = int(span.get("end_ns", 0) or 0)
    if window_end_ns <= window_start_ns or span_end_ns <= span_start_ns:
        return False
    return span_end_ns >= window_start_ns and span_start_ns <= window_end_ns


def _phase_anchor_span_ids(window: dict[str, Any]) -> list[str]:
    anchor_span_ids = _normalize_string_list(window.get("anchor_span_ids", []))
    anchor_span_id = str(window.get("anchor_span_id", "")).strip()
    if anchor_span_id:
        anchor_span_ids.append(anchor_span_id)
    return sorted(set(anchor_span_ids))


def _group_by_phase_window(graph_plan: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for group in graph_plan.get("graph_groups", []):
        if not isinstance(group, dict):
            continue
        graph_group_id = str(group.get("graph_group_id", "")).strip()
        if not graph_group_id:
            continue
        for phase_window_id in _normalize_string_list(group.get("phase_window_ids", [])):
            mapping.setdefault(phase_window_id, graph_group_id)
    return mapping


def _build_target_row(
    index: int,
    span: dict[str, Any],
    window: dict[str, Any],
    graph_group_id: str,
) -> dict[str, Any]:
    start_ns = int(span.get("start_ns", 0) or 0)
    end_ns = int(span.get("end_ns", 0) or 0)
    semantic_class = str(span.get("semantic_class", "unknown")).strip() or "unknown"
    return {
        "graph_target_row_id": f"graph_target_{index:05d}",
        "span_id": str(span.get("span_id", "")).strip(),
        "phase": str(window.get("phase", "")).strip() or "unknown",
        "phase_window_id": str(window.get("phase_window_id", "")).strip(),
        "graph_group_id": graph_group_id,
        "stream_id": str(span.get("stream_id", "")).strip(),
        "semantic_class": semantic_class,
        "start_ns": start_ns,
        "end_ns": end_ns,
        "dur_ns": max(0, end_ns - start_ns),
        "phase_anchor_span_ids": _phase_anchor_span_ids(window),
        "target_scope_reason": "step3_device_semantic_and_in_graph_phase_window",
        "task_ids": _normalize_string_list(span.get("task_ids", [])),
        "op_row_ids": _normalize_string_list(span.get("op_row_ids", [])),
        "trace_span_ids": _normalize_string_list(span.get("trace_span_ids", [])),
        "related_kernel_names": _normalize_string_list(span.get("related_kernel_names", [])),
        "related_op_names": _normalize_string_list(span.get("related_op_names", [])),
        "parallel_group": str(span.get("parallel_group", "")).strip(),
        "boundary_stream_id": str(window.get("boundary_stream_id", "")).strip(),
        "boundary_task_type": str(window.get("boundary_task_type", "")).strip(),
        "boundary_relation": str(window.get("boundary_relation", "")).strip(),
    }


def build_graph_mapping_targets_for_workspace(workspace_dir: Path) -> dict[str, Any]:
    state = load_state(workspace_dir)
    artifacts = state.get("artifacts", {})
    graph_plan_path = Path(str(artifacts.get("graph_execution_plan_path", "")).strip())
    classified_spans_path = Path(str(artifacts.get("classified_spans_path", "")).strip())
    ensure(graph_plan_path.exists(), f"缺少 graph_execution_plan.json: {graph_plan_path}")
    ensure(classified_spans_path.exists(), f"缺少 classified_spans.json: {classified_spans_path}")

    graph_plan = load_json(graph_plan_path)
    phase_windows = [item for item in graph_plan.get("phase_windows", []) if isinstance(item, dict)]
    ensure(phase_windows, "graph_execution_plan.json 缺少 phase_windows，无法冻结 formal graph targets。")
    group_by_window = _group_by_phase_window(graph_plan)
    phase_marker_ids = {
        span_id
        for window in phase_windows
        for span_id in _phase_anchor_span_ids(window)
        if span_id
    }

    rows_by_span_id: dict[str, dict[str, Any]] = {}
    row_index = 1
    for stream in iter_classified_streams(classified_spans_path):
        for span in stream.get("spans", []):
            if not isinstance(span, dict):
                continue
            if span.get("exclude_from_code_mapping"):
                continue
            if str(span.get("semantic_class", "")).strip() not in FORMAL_GRAPH_TARGET_SEMANTIC_CLASSES:
                continue
            if not str(span.get("stream_id", "")).strip():
                continue
            span_id = str(span.get("span_id", "")).strip()
            if not span_id or span_id in phase_marker_ids:
                continue
            for window in phase_windows:
                if not _window_overlaps_span(window, span):
                    continue
                phase_window_id = str(window.get("phase_window_id", "")).strip()
                graph_group_id = group_by_window.get(phase_window_id, "")
                candidate_row = _build_target_row(row_index, span, window, graph_group_id)
                existing = rows_by_span_id.get(span_id)
                if existing is None:
                    rows_by_span_id[span_id] = candidate_row
                    row_index += 1
                    break
                current_start = int(existing.get("start_ns", 0) or 0)
                candidate_start = int(candidate_row.get("start_ns", 0) or 0)
                if (candidate_start, phase_window_id) < (current_start, str(existing.get("phase_window_id", "")).strip()):
                    rows_by_span_id[span_id] = candidate_row
                break

    target_rows = sorted(
        rows_by_span_id.values(),
        key=lambda item: (
            int(item.get("start_ns", 0) or 0),
            int(item.get("end_ns", 0) or 0),
            str(item.get("span_id", "")),
        ),
    )
    for index, row in enumerate(target_rows, start=1):
        row["graph_target_row_id"] = f"graph_target_{index:05d}"

    counts_by_phase: dict[str, int] = {}
    counts_by_semantic_class: dict[str, int] = {}
    for row in target_rows:
        phase = str(row.get("phase", "")).strip() or "unknown"
        semantic_class = str(row.get("semantic_class", "")).strip() or "unknown"
        counts_by_phase[phase] = counts_by_phase.get(phase, 0) + 1
        counts_by_semantic_class[semantic_class] = counts_by_semantic_class.get(semantic_class, 0) + 1

    payload = {
        "schema_version": "graph_mapping_targets_v1",
        "status": "built" if target_rows else "blocked",
        "source": {
            "graph_execution_plan_path": str(graph_plan_path),
            "classified_spans_path": str(classified_spans_path),
        },
        "summary": {
            "approved_target_count": len(target_rows),
            "counts_by_phase": counts_by_phase,
            "counts_by_semantic_class": counts_by_semantic_class,
        },
        "rows": target_rows,
    }
    output_path = workspace_dir / "artifacts" / "graph" / "graph_mapping_targets.json"
    dump_json(output_path, payload)

    state.setdefault("artifacts", {})["graph_mapping_targets_path"] = str(output_path)
    state.setdefault("artifacts", {})["graph_execution_plan_path"] = str(graph_plan_path)
    state.setdefault("flags", {})["graph_mapping_targets_built"] = True
    save_state(workspace_dir, state)
    return payload


def main() -> int:
    args = build_parser().parse_args()
    build_graph_mapping_targets_for_workspace(Path(args.workspace_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
