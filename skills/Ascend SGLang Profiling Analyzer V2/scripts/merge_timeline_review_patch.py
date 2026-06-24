from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Any

from check_scope_gate import build_scope_gate_result
from classify_spans import infer_stream_role
from workflow_common import dump_json, load_json


ALLOWED_SPAN_MUTATION_FIELDS = {
    "semantic_class",
    "exclude_from_code_mapping",
    "exclude_reason",
    "semantic_confidence",
    "parallel_group",
}
ALLOWED_STREAM_MUTATION_FIELDS = {"stream_role"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="合并 Step 3 timeline review patch，生成 reviewed artifacts。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _normalized_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        normalized = str(item).strip()
        if normalized:
            result.append(normalized)
    return result


def _flatten_streams(classified: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    spans: list[dict[str, Any]] = []
    stream_roles: dict[str, str] = {}
    for stream in classified.get("streams", []):
        if not isinstance(stream, dict):
            continue
        stream_id = str(stream.get("stream_id", "")).strip()
        if not stream_id:
            continue
        stream_roles[stream_id] = str(stream.get("stream_role", "")).strip()
        for span in stream.get("spans", []):
            if not isinstance(span, dict):
                continue
            copied = dict(span)
            copied["stream_id"] = stream_id
            spans.append(copied)
    spans.sort(key=lambda item: (int(item.get("start_ns", 0) or 0), int(item.get("end_ns", 0) or 0), str(item.get("span_id", ""))))
    return spans, stream_roles


def validate_timeline_review_patch_payload(patch: dict[str, Any], classified_base: dict[str, Any]) -> None:
    status = str(patch.get("status", "")).strip()
    ensure(status == "passed", f"timeline_review_patch.status 必须为 passed，当前为 {status!r}")
    blocking_issues = patch.get("blocking_issues", [])
    ensure(isinstance(blocking_issues, list), "timeline_review_patch.blocking_issues 必须是列表。")
    ensure(not blocking_issues, "timeline_review_patch.status=passed 时 blocking_issues 必须为空列表。")
    ensure(isinstance(patch.get("review_scope", {}), dict), "timeline_review_patch.review_scope 必须是对象。")
    ensure(isinstance(patch.get("mutation_summary", {}), dict), "timeline_review_patch.mutation_summary 必须是对象。")
    patch_allowed_fields = set(_normalized_string_list(patch.get("allowed_mutation_fields", [])))
    ensure(
        patch_allowed_fields.issubset(ALLOWED_SPAN_MUTATION_FIELDS | ALLOWED_STREAM_MUTATION_FIELDS),
        "timeline_review_patch.allowed_mutation_fields 包含未授权字段。",
    )
    stream_updates = patch.get("stream_updates", [])
    span_updates = patch.get("span_updates", [])
    ensure(isinstance(stream_updates, list), "timeline_review_patch.stream_updates 必须是列表。")
    ensure(isinstance(span_updates, list), "timeline_review_patch.span_updates 必须是列表。")

    base_spans, _ = _flatten_streams(classified_base)
    base_stream_ids = {str(span.get("stream_id", "")).strip() for span in base_spans}
    base_span_ids = {str(span.get("span_id", "")).strip() for span in base_spans}
    seen_stream_ids: set[str] = set()
    seen_span_ids: set[str] = set()

    for index, update in enumerate(stream_updates):
        ensure(isinstance(update, dict), f"stream_updates[{index}] 必须是对象。")
        stream_id = str(update.get("stream_id", "")).strip()
        ensure(stream_id, f"stream_updates[{index}].stream_id 不能为空。")
        ensure(stream_id in base_stream_ids, f"stream_updates[{index}].stream_id 不存在于 base classified: {stream_id}")
        ensure(stream_id not in seen_stream_ids, f"stream_updates[{index}] 重复 stream_id: {stream_id}")
        seen_stream_ids.add(stream_id)
        ensure(str(update.get("stream_role", "")).strip(), f"stream_updates[{index}].stream_role 不能为空。")
        ensure(str(update.get("reason", "")).strip(), f"stream_updates[{index}].reason 不能为空。")
        ensure(str(update.get("evidence_summary", "")).strip(), f"stream_updates[{index}].evidence_summary 不能为空。")

    for index, update in enumerate(span_updates):
        ensure(isinstance(update, dict), f"span_updates[{index}] 必须是对象。")
        span_id = str(update.get("span_id", "")).strip()
        ensure(span_id, f"span_updates[{index}].span_id 不能为空。")
        ensure(span_id in base_span_ids, f"span_updates[{index}].span_id 不存在于 base classified: {span_id}")
        ensure(span_id not in seen_span_ids, f"span_updates[{index}] 重复 span_id: {span_id}")
        seen_span_ids.add(span_id)
        field_updates = update.get("field_updates", {})
        ensure(isinstance(field_updates, dict) and field_updates, f"span_updates[{index}].field_updates 必须是非空对象。")
        unknown_fields = sorted(set(field_updates.keys()) - ALLOWED_SPAN_MUTATION_FIELDS)
        ensure(not unknown_fields, f"span_updates[{index}] 包含禁止修改字段: {unknown_fields}")
        ensure(str(update.get("reason", "")).strip(), f"span_updates[{index}].reason 不能为空。")
        ensure(str(update.get("evidence_summary", "")).strip(), f"span_updates[{index}].evidence_summary 不能为空。")


def _normalize_parallel_groups(spans: list[dict[str, Any]]) -> None:
    token_order: list[str] = []
    token_map: dict[str, str] = {}
    for span in spans:
        token = str(span.get("parallel_group", "")).strip()
        if not token:
            token = f"__span__:{span.get('span_id', '')}"
        if token not in token_map:
            token_order.append(token)
            token_map[token] = ""
        span["_parallel_group_token"] = token
    for index, token in enumerate(token_order, start=1):
        token_map[token] = f"pg_{index:05d}"
    for span in spans:
        token = str(span.pop("_parallel_group_token", "")).strip()
        span["parallel_group"] = token_map[token]


def _rebuild_classified_payload(
    classified_base: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    spans, base_stream_roles = _flatten_streams(classified_base)
    span_map = {str(span.get("span_id", "")).strip(): span for span in spans}
    stream_role_overrides = {
        str(item.get("stream_id", "")).strip(): str(item.get("stream_role", "")).strip()
        for item in patch.get("stream_updates", [])
        if isinstance(item, dict)
    }

    for item in patch.get("span_updates", []):
        if not isinstance(item, dict):
            continue
        span_id = str(item.get("span_id", "")).strip()
        if not span_id:
            continue
        field_updates = item.get("field_updates", {})
        if not isinstance(field_updates, dict):
            continue
        target = span_map[span_id]
        for key, value in field_updates.items():
            target[key] = value

    spans_by_stream: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for span in spans:
        spans_by_stream[str(span.get("stream_id", "")).strip()].append(span)

    scope_summary = {
        "non_hardware_span_count": 0,
        "hardware_excluded_count": 0,
        "hardware_semantic_candidate_count": 0,
    }
    semantic_span_count = 0
    excluded_span_count = 0
    streams_payload: list[dict[str, Any]] = []

    for stream_id in sorted(spans_by_stream.keys()):
        stream_spans = sorted(
            spans_by_stream[stream_id],
            key=lambda item: (int(item.get("start_ns", 0) or 0), int(item.get("end_ns", 0) or 0), str(item.get("span_id", ""))),
        )
        inferred_role = infer_stream_role(stream_id, stream_spans)
        final_role = stream_role_overrides.get(stream_id) or inferred_role or base_stream_roles.get(stream_id, "")
        for span in stream_spans:
            span["stream_role"] = final_role
            span["external_mapping_required"] = not bool(span.get("exclude_from_code_mapping"))
            scope_class = str(span.get("scope_class", "")).strip()
            scope_summary[f"{scope_class}_count"] = int(scope_summary.get(f"{scope_class}_count", 0) or 0) + 1
            if span["external_mapping_required"]:
                semantic_span_count += 1
            else:
                excluded_span_count += 1
        streams_payload.append(
            {
                "stream_id": stream_id,
                "stream_role": final_role,
                "spans": stream_spans,
            }
        )

    ordered_spans = [
        span
        for stream in sorted(streams_payload, key=lambda item: str(item.get("stream_id", "")))
        for span in stream.get("spans", [])
    ]
    _normalize_parallel_groups(ordered_spans)

    return {
        "schema_version": str(classified_base.get("schema_version", "")).strip() or "classified_spans_v1",
        "streams": streams_payload,
        "span_count": len(spans),
        "semantic_span_count": semantic_span_count,
        "excluded_span_count": excluded_span_count,
        "scope_summary": scope_summary,
        "review_patch_summary": {
            "applied_stream_update_count": len(stream_role_overrides),
            "applied_span_update_count": len([item for item in patch.get("span_updates", []) if isinstance(item, dict)]),
        },
    }


def merge_timeline_review_patch_for_workspace(workspace_dir: Path) -> dict[str, Any]:
    classified_base_path = workspace_dir / "artifacts" / "classification" / "classified_spans.base.json"
    patch_path = workspace_dir / "output" / "timeline_review_patch.json"
    reviewed_classified_path = workspace_dir / "artifacts" / "classification" / "classified_spans.reviewed.json"
    reviewed_scope_gate_path = workspace_dir / "output" / "scope_gate_result.reviewed.json"

    ensure(classified_base_path.exists(), f"缺少 Step 3 base classified 文件: {classified_base_path}")
    ensure(patch_path.exists(), f"缺少 timeline_review_patch.json: {patch_path}")

    classified_base = load_json(classified_base_path)
    patch = load_json(patch_path)
    validate_timeline_review_patch_payload(patch, classified_base)

    reviewed_classified = _rebuild_classified_payload(classified_base, patch)
    dump_json(reviewed_classified_path, reviewed_classified)

    reviewed_scope_gate = build_scope_gate_result(reviewed_classified)
    dump_json(reviewed_scope_gate_path, reviewed_scope_gate)
    ensure(
        str(reviewed_scope_gate.get("status", "")).strip() == "passed",
        "reviewed scope gate 未通过，禁止 promotion。",
    )
    return {
        "classified_spans_reviewed_path": str(reviewed_classified_path),
        "scope_gate_result_reviewed_path": str(reviewed_scope_gate_path),
        "reviewed_scope_gate_status": str(reviewed_scope_gate.get("status", "")).strip(),
    }


def main() -> int:
    args = build_parser().parse_args()
    merge_timeline_review_patch_for_workspace(Path(args.workspace_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
