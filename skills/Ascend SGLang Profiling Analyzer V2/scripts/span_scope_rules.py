from __future__ import annotations

from fnmatch import fnmatch
from typing import Any


EXCLUDE_PATTERNS = [
    ("exclude_capture_record", "CAPTURE_RECORD"),
    ("exclude_capture_wait", "CAPTURE_WAIT*"),
    ("exclude_capture_all", "CAPTURE_*"),
    ("exclude_notify_record", "NOTIFY_RECORD*"),
    ("exclude_notify_wait", "NOTIFY_WAIT*"),
    ("exclude_event_record", "EVENT_RECORD*"),
    ("exclude_event_wait", "EVENT_WAIT*"),
    ("exclude_event_reset", "EVENT_RESET*"),
    ("exclude_free", "Free"),
    ("exclude_ascendcl", "AscendCL@*"),
    ("exclude_runtime_event", "Runtime@Event*"),
    ("exclude_enqueue_record", "Enqueue@record"),
    ("exclude_dequeue_record", "Dequeue@record"),
    ("exclude_profiler_step", "ProfilerStep#*"),
    ("exclude_notify_record_alt", "Notify_Record"),
    ("exclude_notify_wait_alt", "Notify_Wait"),
    ("exclude_record_event", "record_event"),
    ("exclude_verify_done", "*verify_done*"),
]

FORCE_INCLUDE_PATTERNS = [
    ("include_fill_new_verified_id", "fill_new_verified_id"),
    ("include_assign_req_to_token_pool", "assign_req_to_token_pool"),
    ("include_assign_draft_cache_locs", "assign_draft_cache_locs*"),
    ("include_cache_loc_assign", "cache_loc_assign"),
    ("include_cache_loc_update", "cache_loc_update"),
    ("include_build_tree_efficient", "build_tree_efficient"),
    ("include_compute_position_kernel", "compute_position_kernel"),
]


def _normalized_key(value: str) -> str:
    return "".join(char.lower() for char in value if char.isalnum())


def normalized_name(value: str) -> str:
    return value.strip().lower()


def _normalize_stream_id_value(value: Any) -> str:
    text = str(value).strip()
    if not text or text.lower() in {"unknown", "n/a", "na", "none", "null"}:
        return ""
    return text


def extract_stream_id(event_args: dict[str, Any], trace_span_stream_id: str = "") -> str:
    normalized_trace_stream_id = _normalize_stream_id_value(trace_span_stream_id)
    if normalized_trace_stream_id:
        return normalized_trace_stream_id
    for key, value in event_args.items():
        normalized_key = _normalized_key(str(key))
        if normalized_key in {"streamid", "physicstreamid"}:
            normalized_value = _normalize_stream_id_value(value)
            if normalized_value:
                return normalized_value
    return ""


def _match_pattern(value: str, pattern: str) -> bool:
    return fnmatch(value.lower(), pattern.lower())


def match_exclude_rule(span_name: str, event_args: dict[str, Any]) -> dict[str, str] | None:
    del event_args
    name = span_name.strip()
    for rule_id, pattern in EXCLUDE_PATTERNS:
        if _match_pattern(name, pattern):
            return {
                "rule_id": rule_id,
                "scope_class": "hardware_excluded",
                "exclude_reason": f"matched exclude rule `{pattern}`",
            }
    return None


def match_force_include_rule(span_name: str, event_args: dict[str, Any]) -> dict[str, str] | None:
    del event_args
    name = span_name.strip()
    for rule_id, pattern in FORCE_INCLUDE_PATTERNS:
        if _match_pattern(name, pattern):
            return {
                "rule_id": rule_id,
                "scope_class": "hardware_semantic_candidate",
                "exclude_reason": "",
            }
    return None


def classify_scope(span_name: str, event_args: dict[str, Any], trace_span_stream_id: str = "") -> dict[str, Any]:
    stream_id = extract_stream_id(event_args, trace_span_stream_id)
    if not stream_id:
        return {
            "has_stream_id": False,
            "stream_id": "",
            "scope_class": "non_hardware_span",
            "exclude_from_code_mapping": True,
            "exclude_reason": "missing streamId/stream_id",
            "matched_scope_rule_id": "missing_stream_id",
            "matched_scope_rule_source": "stream_id_gate",
        }

    force_include = match_force_include_rule(span_name, event_args)
    if force_include:
        return {
            "has_stream_id": True,
            "stream_id": stream_id,
            "scope_class": "hardware_semantic_candidate",
            "exclude_from_code_mapping": False,
            "exclude_reason": "",
            "matched_scope_rule_id": force_include["rule_id"],
            "matched_scope_rule_source": "force_include",
        }

    exclude = match_exclude_rule(span_name, event_args)
    if exclude:
        return {
            "has_stream_id": True,
            "stream_id": stream_id,
            "scope_class": "hardware_excluded",
            "exclude_from_code_mapping": True,
            "exclude_reason": exclude["exclude_reason"],
            "matched_scope_rule_id": exclude["rule_id"],
            "matched_scope_rule_source": "exclude",
        }

    return {
        "has_stream_id": True,
        "stream_id": stream_id,
        "scope_class": "hardware_semantic_candidate",
        "exclude_from_code_mapping": False,
        "exclude_reason": "",
        "matched_scope_rule_id": "default_hardware_candidate",
        "matched_scope_rule_source": "default",
    }
