from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from workflow_common import dump_json, load_json, load_state, save_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="在 graph phase window 内拆分 operator 级 graph spans。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _phase_windows_by_id(graph_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for window in graph_plan.get("phase_windows", []):
        if not isinstance(window, dict):
            continue
        phase_window_id = str(window.get("phase_window_id", "")).strip()
        if phase_window_id:
            result[phase_window_id] = window
    return result


def _build_operator_row(
    index: int,
    target_row: dict[str, Any],
    window: dict[str, Any],
) -> dict[str, Any]:
    start_ns = int(target_row.get("start_ns", 0) or 0)
    end_ns = int(target_row.get("end_ns", 0) or 0)
    semantic_class = str(target_row.get("semantic_class", "unknown")).strip() or "unknown"
    stream_id = str(target_row.get("stream_id", "")).strip()
    return {
        "graph_operator_span_id": f"graph_operator_span_{index:05d}",
        "graph_target_row_id": str(target_row.get("graph_target_row_id", "")).strip(),
        "span_id": str(target_row.get("span_id", "")).strip(),
        "phase": str(target_row.get("phase", "")).strip() or str(window.get("phase", "")).strip(),
        "graph_group_id": str(target_row.get("graph_group_id", "")).strip(),
        "phase_window_id": str(target_row.get("phase_window_id", "")).strip(),
        "parent_marker_span_id": str(window.get("anchor_span_id", "")).strip(),
        "start_ns": start_ns,
        "end_ns": end_ns,
        "dur_ns": max(0, end_ns - start_ns),
        "stream_id": stream_id,
        "stream_id_source": "graph_mapping_target_stream_id",
        "semantic_class": semantic_class,
        "trace_span_ids": _normalize_string_list(target_row.get("trace_span_ids", [])),
        "task_ids": _normalize_string_list(target_row.get("task_ids", [])),
        "op_row_ids": _normalize_string_list(target_row.get("op_row_ids", [])),
        "kernel_names": _normalize_string_list(target_row.get("related_kernel_names", [])),
        "operator_family_hint": semantic_class,
        "candidate_operator_names": _normalize_string_list(target_row.get("related_op_names", [])),
        "window_relation": "formal_graph_target_member",
        "evidence_sources": [
            "graph_mapping_targets.json",
            "graph_execution_plan.json",
        ],
        "requires_code_alignment": True,
        "formal_graph_target": True,
        "target_scope_reason": str(target_row.get("target_scope_reason", "")).strip(),
        "boundary_stream_id": str(window.get("boundary_stream_id", "")).strip(),
        "anchor_stream_id": str(window.get("anchor_stream_id", "")).strip(),
    }


def build_graph_operator_spans_for_workspace(workspace_dir: Path) -> dict[str, Any]:
    state = load_state(workspace_dir)
    artifacts = state.get("artifacts", {})
    graph_plan_path = Path(str(artifacts.get("graph_execution_plan_path", "")).strip())
    graph_mapping_targets_path = Path(str(artifacts.get("graph_mapping_targets_path", "")).strip())
    ensure(graph_plan_path.exists(), f"缺少 graph_execution_plan.json: {graph_plan_path}")
    ensure(graph_mapping_targets_path.exists(), f"缺少 graph_mapping_targets.json: {graph_mapping_targets_path}")

    graph_plan = load_json(graph_plan_path)
    graph_mapping_targets = load_json(graph_mapping_targets_path)
    phase_windows = _phase_windows_by_id(graph_plan)
    ensure(phase_windows, "graph_execution_plan.json 缺少 phase_windows，无法拆分 graph operator spans。")

    operator_rows: list[dict[str, Any]] = []
    counts_by_phase: dict[str, int] = {}
    counts_by_semantic_class: dict[str, int] = {}
    rows = graph_mapping_targets.get("rows", [])
    ensure(isinstance(rows, list), "graph_mapping_targets.json.rows 必须是列表。")

    row_index = 1
    for target_row in rows:
        if not isinstance(target_row, dict):
            continue
        span_id = str(target_row.get("span_id", "")).strip()
        phase_window_id = str(target_row.get("phase_window_id", "")).strip()
        if not span_id or not phase_window_id:
            continue
        window = phase_windows.get(phase_window_id)
        ensure(window is not None, f"graph_mapping_targets span_id={span_id} 指向了不存在的 phase_window_id={phase_window_id}")
        row = _build_operator_row(row_index, target_row, window)
        operator_rows.append(row)
        phase = str(row.get("phase", "")).strip() or "unknown"
        semantic_class = str(row.get("semantic_class", "")).strip() or "unknown"
        counts_by_phase[phase] = counts_by_phase.get(phase, 0) + 1
        counts_by_semantic_class[semantic_class] = counts_by_semantic_class.get(semantic_class, 0) + 1
        row_index += 1

    operator_rows.sort(
        key=lambda item: (
            int(item.get("start_ns", 0) or 0),
            int(item.get("end_ns", 0) or 0),
            str(item.get("span_id", "")),
            str(item.get("graph_operator_span_id", "")),
        )
    )

    payload = {
        "schema_version": "graph_operator_spans_v1",
        "status": "built" if operator_rows else "blocked",
        "source": {
            "graph_execution_plan_path": str(graph_plan_path),
            "graph_mapping_targets_path": str(graph_mapping_targets_path),
        },
        "summary": {
            "formal_graph_target_count": len(operator_rows),
            "graph_operator_span_count": len(operator_rows),
            "counts_by_phase": counts_by_phase,
            "counts_by_semantic_class": counts_by_semantic_class,
        },
        "rows": operator_rows,
    }
    output_path = workspace_dir / "artifacts" / "graph" / "graph_operator_spans.json"
    dump_json(output_path, payload)

    state.setdefault("artifacts", {})["graph_operator_spans_path"] = str(output_path)
    state.setdefault("flags", {})["graph_operator_spans_built"] = True
    save_state(workspace_dir, state)
    return payload


def main() -> int:
    args = build_parser().parse_args()
    build_graph_operator_spans_for_workspace(Path(args.workspace_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
