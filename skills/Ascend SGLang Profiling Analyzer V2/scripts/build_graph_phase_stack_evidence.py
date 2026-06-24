from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from workflow_common import dump_json, load_json, load_state, save_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="冻结 shared graph phase stack evidence。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def _safe_load_json(path_str: str) -> dict[str, Any]:
    path = Path(str(path_str).strip())
    if not path.exists() or not path.is_file():
        return {}
    return load_json(path)


def _normalize_phase_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen_span_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        span_id = str(row.get("span_id", "")).strip()
        if not span_id or span_id in seen_span_ids:
            continue
        seen_span_ids.add(span_id)
        normalized.append(dict(row))
    normalized.sort(
        key=lambda item: (
            int(item.get("start_ns", 0) or 0),
            int(item.get("end_ns", 0) or 0),
            str(item.get("candidate_phase", "")),
            str(item.get("span_id", "")),
        )
    )
    return normalized


def _resolve_stack_evidence_source(state: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    artifacts = state.setdefault("artifacts", {})
    candidate_keys = ("stack_evidence_lite_path", "stack_evidence_path")
    last_path: Path | None = None
    for artifact_key in candidate_keys:
        candidate_str = str(artifacts.get(artifact_key, "")).strip()
        if not candidate_str:
            continue
        candidate_path = Path(candidate_str)
        last_path = candidate_path
        if not candidate_path.exists() or not candidate_path.is_file():
            continue
        payload = _safe_load_json(str(candidate_path))
        if not isinstance(payload, dict):
            continue
        if isinstance(payload.get("graph_phase_marker_rows"), list) or isinstance(payload.get("graph_replay_rows"), list):
            return candidate_path, payload
    if last_path is not None:
        raise RuntimeError(f"缺少可用 stack_evidence 输入: {last_path}")
    raise RuntimeError("缺少 stack_evidence_lite.json / stack_evidence.json，无法构建 graph_phase_stack_evidence。")


def build_graph_phase_stack_evidence_for_workspace(workspace_dir: Path) -> dict[str, Any]:
    state = load_state(workspace_dir)
    artifacts = state.setdefault("artifacts", {})
    stack_evidence_path, stack_evidence = _resolve_stack_evidence_source(state)
    phase_rows = _normalize_phase_rows(
        stack_evidence.get("graph_phase_marker_rows", stack_evidence.get("graph_replay_rows", []))
    )

    summary = {
        "phase_row_count": len(phase_rows),
        "counts_by_phase": {},
        "counts_by_source": {},
    }
    for row in phase_rows:
        phase = str(row.get("candidate_phase", "")).strip() or "unknown"
        source = str(row.get("phase_source", "")).strip() or "unknown"
        summary["counts_by_phase"][phase] = int(summary["counts_by_phase"].get(phase, 0)) + 1
        summary["counts_by_source"][source] = int(summary["counts_by_source"].get(source, 0)) + 1

    payload = {
        "schema_version": "graph_phase_stack_evidence_v1",
        "status": "built" if phase_rows else "blocked",
        "source": {
            "stack_evidence_path": str(stack_evidence_path),
        },
        "summary": summary,
        "rows": phase_rows,
    }

    output_path = workspace_dir / "artifacts" / "graph" / "graph_phase_stack_evidence.json"
    dump_json(output_path, payload)
    artifacts["graph_phase_stack_evidence_path"] = str(output_path)
    state.setdefault("flags", {})["graph_phase_stack_evidence_built"] = True
    save_state(workspace_dir, state)
    return payload


def main() -> int:
    args = build_parser().parse_args()
    build_graph_phase_stack_evidence_for_workspace(Path(args.workspace_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
