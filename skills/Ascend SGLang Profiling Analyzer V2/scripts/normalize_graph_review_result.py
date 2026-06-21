from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="对 Step 5 graph_review_result.json 做轻量 lint 与结构归一化。")
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument(
        "--output-path",
        default="",
        help="可选；默认使用 workspace/output/graph_review_result.json",
    )
    return parser


def _append_missing_closers(raw_text: str) -> str:
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in raw_text:
        if in_string:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in {"}", "]"} and stack:
            expected = stack[-1]
            if ch == expected:
                stack.pop()
    if in_string:
        raw_text += '"'
    if stack:
        raw_text += "".join(reversed(stack))
    return raw_text


def _load_or_autofix_json(path: Path) -> tuple[dict[str, Any], bool]:
    text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("graph_review_result.json 顶层必须是对象。")
        return payload, False
    except json.JSONDecodeError as exc:
        repaired = _append_missing_closers(text.rstrip())
        if repaired == text:
            raise ValueError(f"graph_review_result.json JSON 语法非法，且无法自动补齐: {exc}") from exc
        try:
            payload = json.loads(repaired)
        except json.JSONDecodeError as repaired_exc:
            raise ValueError(
                f"graph_review_result.json JSON 语法非法，自动补齐闭合符后仍无法解析: {repaired_exc}"
            ) from repaired_exc
        if not isinstance(payload, dict):
            raise ValueError("graph_review_result.json 顶层必须是对象。")
        path.write_text(repaired + ("\n" if not repaired.endswith("\n") else ""), encoding="utf-8", newline="\n")
        return payload, True


def _dict_phase_rows(payload: dict[str, Any], row_key_name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, value in payload.items():
        if key in {"rows", "items", "row_count", "status", "summary", "notes"}:
            continue
        if isinstance(value, dict):
            rows.append({row_key_name: key, **value})
        elif isinstance(value, list):
            rows.append({row_key_name: key, "items": value})
    return rows


def _normalize_rows_payload(payload: Any, fallback_status: str, row_key_name: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    existing_status = ""
    if isinstance(payload, dict):
        existing_status = str(payload.get("status", "")).strip()
        if isinstance(payload.get("rows"), list):
            rows = [item for item in payload.get("rows", []) if isinstance(item, dict)]
        elif isinstance(payload.get("items"), list):
            rows = [item for item in payload.get("items", []) if isinstance(item, dict)]
        else:
            rows = _dict_phase_rows(payload, row_key_name)
    elif isinstance(payload, list):
        rows = [item for item in payload if isinstance(item, dict)]
    normalized = {
        "status": existing_status or fallback_status,
        "row_count": len(rows),
        "rows": rows,
    }
    if isinstance(payload, dict):
        for key in ("summary", "notes"):
            if key in payload and key not in normalized:
                normalized[key] = payload[key]
    return normalized


def normalize_graph_review_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    changed = False
    status = str(payload.get("status", "")).strip()
    promotion = payload.get("artifact_promotion", {})
    if not isinstance(promotion, dict):
        return payload, changed

    for key in ("graph_execution_plan_updates", "graph_forward_context_updates"):
        updates = promotion.get(key)
        if isinstance(updates, dict) and status and not str(updates.get("status", "")).strip():
            updates["status"] = status
            changed = True

    normalized_specs = {
        "graph_span_candidates_payload": "phase",
        "forward_segment_template_payload": "phase",
        "graph_span_alignment_payload": "span_id",
    }
    for key, row_key_name in normalized_specs.items():
        before = promotion.get(key, {})
        after = _normalize_rows_payload(before, status or "partial", row_key_name)
        if after != before:
            promotion[key] = after
            changed = True

    if isinstance(payload.get("span_alignment"), list):
        payload["span_alignment"] = _normalize_rows_payload(payload.get("span_alignment"), status or "partial", "span_id")
        changed = True
    return payload, changed


def normalize_graph_review_result_file(path: Path) -> bool:
    payload, changed = _load_or_autofix_json(path)
    payload, normalized_changed = normalize_graph_review_payload(payload)
    if changed or normalized_changed:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return changed or normalized_changed


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    output_path = Path(args.output_path) if args.output_path else workspace_dir / "output" / "graph_review_result.json"
    normalize_graph_review_result_file(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
