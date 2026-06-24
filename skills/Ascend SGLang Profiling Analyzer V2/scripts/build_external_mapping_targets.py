from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from workflow_common import dump_json, iter_classified_streams, load_json, load_state, save_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="冻结 Step4 external formal target set。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def require_state_artifact_path(state: dict[str, Any], artifact_key: str, label: str) -> Path:
    raw_path = str(state.get("artifacts", {}).get(artifact_key, "")).strip()
    ensure(raw_path, f"缺少 state.artifacts.{artifact_key}，说明上游 {label} 尚未成功生成。")
    path = Path(raw_path)
    ensure(path.exists() and path.is_file(), f"{label} 不存在或不是文件: {path}")
    return path


def _normalized_graph_target_ids(graph_mapping_targets: dict[str, Any]) -> set[str]:
    rows = graph_mapping_targets.get("rows", [])
    if not isinstance(rows, list):
        return set()
    return {
        str(row.get("span_id", "")).strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("span_id", "")).strip()
    }


def _build_external_target_row(index: int, span: dict[str, Any]) -> dict[str, Any]:
    semantic_class = str(span.get("semantic_class", "unknown")).strip() or "unknown"
    scope_class = str(span.get("scope_class", "")).strip()
    return {
        "external_target_row_id": f"external_target_{index:05d}",
        "span_id": str(span.get("span_id", "")).strip(),
        "stream_id": str(span.get("stream_id", "")).strip(),
        "span_name": str(span.get("span_name", "")).strip(),
        "semantic_class": semantic_class,
        "parallel_group": str(span.get("parallel_group", "")).strip(),
        "approved_for_external_mapping": True,
        "target_scope_reason": "step3_device_semantic_minus_formal_graph_targets",
        "step3_scope_snapshot": {
            "scope_class": scope_class,
            "has_stream_id": bool(span.get("has_stream_id", False)),
            "exclude_from_code_mapping": bool(span.get("exclude_from_code_mapping", False)),
            "exclude_reason": str(span.get("exclude_reason", "")).strip(),
            "external_mapping_required": bool(span.get("external_mapping_required", False)),
            "stream_role": str(span.get("stream_role", "")).strip(),
        },
        "task_ids": [str(item).strip() for item in span.get("task_ids", []) if str(item).strip()],
        "op_row_ids": [str(item).strip() for item in span.get("op_row_ids", []) if str(item).strip()],
    }


def build_external_mapping_targets_for_workspace(workspace_dir: Path) -> dict[str, Any]:
    state = load_state(workspace_dir)
    artifacts = state.setdefault("artifacts", {})
    classified_spans_path = require_state_artifact_path(state, "classified_spans_path", "classified_spans.json")
    graph_mapping_targets_path = require_state_artifact_path(state, "graph_mapping_targets_path", "graph_mapping_targets.json")
    graph_execution_plan_path = require_state_artifact_path(state, "graph_execution_plan_path", "graph_execution_plan.json")

    graph_mapping_targets = load_json(graph_mapping_targets_path)
    graph_target_ids = _normalized_graph_target_ids(graph_mapping_targets)

    rows: list[dict[str, Any]] = []
    counts_by_semantic_class: dict[str, int] = {}
    counts_by_scope_class: dict[str, int] = {}
    row_index = 1
    for stream in iter_classified_streams(classified_spans_path):
        for span in stream.get("spans", []):
            if not isinstance(span, dict):
                continue
            span_id = str(span.get("span_id", "")).strip()
            if not span_id or span_id in graph_target_ids:
                continue
            if str(span.get("scope_class", "")).strip() != "hardware_semantic_candidate":
                continue
            if bool(span.get("exclude_from_code_mapping", False)):
                continue
            if not bool(span.get("external_mapping_required", False)):
                continue
            if not bool(span.get("has_stream_id", False)):
                continue
            row = _build_external_target_row(row_index, span)
            rows.append(row)
            row_index += 1
            semantic_class = str(row.get("semantic_class", "unknown")).strip() or "unknown"
            scope_class = str(row.get("step3_scope_snapshot", {}).get("scope_class", "")).strip() or "unknown"
            counts_by_semantic_class[semantic_class] = counts_by_semantic_class.get(semantic_class, 0) + 1
            counts_by_scope_class[scope_class] = counts_by_scope_class.get(scope_class, 0) + 1

    rows.sort(
        key=lambda item: (
            str(item.get("parallel_group", "")),
            str(item.get("stream_id", "")),
            str(item.get("span_id", "")),
        )
    )
    for index, row in enumerate(rows, start=1):
        row["external_target_row_id"] = f"external_target_{index:05d}"

    payload = {
        "schema_version": "external_mapping_targets_v2",
        "status": "built" if rows else "blocked",
        "source": {
            "classified_spans_path": str(classified_spans_path),
            "graph_mapping_targets_path": str(graph_mapping_targets_path),
            "graph_execution_plan_path": str(graph_execution_plan_path),
        },
        "summary": {
            "approved_target_count": len(rows),
            "counts_by_semantic_class": counts_by_semantic_class,
            "counts_by_scope_class": counts_by_scope_class,
        },
        "rows": rows,
    }

    output_path = workspace_dir / "artifacts" / "mapping" / "external_mapping_targets.json"
    dump_json(output_path, payload)
    artifacts["external_mapping_targets_path"] = str(output_path)
    state.setdefault("flags", {})["external_mapping_targets_built"] = True
    save_state(workspace_dir, state)
    return payload


def main() -> int:
    args = build_parser().parse_args()
    build_external_mapping_targets_for_workspace(Path(args.workspace_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
