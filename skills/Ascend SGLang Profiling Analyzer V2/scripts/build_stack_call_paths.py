from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from workflow_common import dump_json, load_json, load_state, save_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建 stack_call_paths.json。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def _safe_load_json(path_str: str) -> dict[str, Any]:
    if not path_str:
        return {"rows": [], "frames": []}
    path = Path(path_str)
    if not path.exists() or not path.is_file():
        return {"rows": [], "frames": []}
    return load_json(path)


def _resolve_stack_evidence_input_path(state: dict[str, Any]) -> Path | None:
    artifacts = state.get("artifacts", {})
    for key in ("stack_evidence_lite_path", "stack_evidence_path"):
        raw_path = str(artifacts.get(key, "")).strip()
        if not raw_path:
            continue
        path = Path(raw_path)
        if path.exists() and path.is_file():
            return path
    return None


def _normalize_repo_relative_path(path: str) -> str:
    return str(path or "").replace("\\", "/").lstrip("./")


def _repo_path_variants(path: str) -> set[str]:
    normalized = _normalize_repo_relative_path(path)
    if not normalized:
        return set()
    variants = {normalized}
    if normalized.startswith("python/"):
        variants.add(normalized[len("python/") :])
    else:
        variants.add(f"python/{normalized}")
    return variants
IMPLEMENTATION_PATH_HINT_KEYWORDS = [
    "layers/",
    "layer.py",
    "attention",
    "mlp",
    "moe",
    "hardware_backend",
    "linear",
    "allocator",
    "communicator",
    "backend",
]
ORCHESTRATION_PATH_PENALTY_KEYWORDS = [
    "speculative/",
    "scheduler",
    "schedule_batch",
    "worker",
    "prefill_delayer",
    "event_loop",
]
COMMUNICATION_HINT_KEYWORDS = [
    "communicator",
    "allreduce",
    "all_reduce",
    "allgather",
    "all_gather",
    "reduce_scatter",
    "broadcast",
    "hccl",
    "distributed",
]
COMPUTE_HINT_KEYWORDS = [
    "hardware_backend",
    "attention",
    "linear",
    "mlp",
    "moe",
    "backend",
    "matmul",
    "fused",
    "allocator",
]
COMMUNICATION_OP_KEYWORDS = [
    "allreduce",
    "all_reduce",
    "allgather",
    "all_gather",
    "reduce",
    "broadcast",
    "scatter",
    "record",
    "hccl",
]
COMPUTE_OP_KEYWORDS = [
    "matmul",
    "attention",
    "moe",
    "fused",
    "quant",
    "linear",
    "gemm",
    "cache",
    "extend",
]


def _normalized_frame_indexes(candidate: dict[str, Any]) -> list[int]:
    frame_indexes = candidate.get("frame_indexes", [])
    if not isinstance(frame_indexes, list):
        return []
    normalized: set[int] = set()
    for index in frame_indexes:
        try:
            value = int(index)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            normalized.add(value)
    return sorted(normalized)


def _contains_any_keyword(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _semantic_category(semantic_class: str, stream_role: str) -> str:
    normalized_stream_role = str(stream_role or "").strip().lower()
    if normalized_stream_role in {"communication", "compute"}:
        return normalized_stream_role
    normalized_semantic_class = str(semantic_class or "").strip().lower()
    if normalized_semantic_class in {"communication", "compute"}:
        return normalized_semantic_class
    return "unknown"


def _candidate_has_implementation_hint(path_text: str, symbol_text: str, semantic_category: str) -> bool:
    combined_text = f"{path_text} {symbol_text}"
    if semantic_category == "communication":
        return _contains_any_keyword(combined_text, COMMUNICATION_HINT_KEYWORDS)
    if semantic_category == "compute":
        return _contains_any_keyword(combined_text, COMPUTE_HINT_KEYWORDS)
    return _contains_any_keyword(combined_text, IMPLEMENTATION_PATH_HINT_KEYWORDS)


def _candidate_is_orchestration_heavy(path_text: str, symbol_text: str) -> bool:
    return _contains_any_keyword(f"{path_text} {symbol_text}", ORCHESTRATION_PATH_PENALTY_KEYWORDS)


def _semantic_op_match_score(path_text: str, symbol_text: str, op_name: str, span_name: str, semantic_category: str) -> int:
    combined_candidate_text = f"{path_text} {symbol_text}"
    op_text = f"{op_name} {span_name}".strip().lower()
    if semantic_category == "communication":
        if _contains_any_keyword(op_text, COMMUNICATION_OP_KEYWORDS) and _contains_any_keyword(
            combined_candidate_text, COMMUNICATION_HINT_KEYWORDS
        ):
            return 16
        return 0
    if semantic_category == "compute":
        if _contains_any_keyword(op_text, COMPUTE_OP_KEYWORDS) and _contains_any_keyword(
            combined_candidate_text, COMPUTE_HINT_KEYWORDS
        ):
            return 12
        return 0
    return 0


def _function_candidate_score_breakdown(
    candidate: dict[str, Any],
    *,
    op_name: str = "",
    semantic_class: str = "",
    stream_role: str = "",
    span_name: str = "",
) -> dict[str, int]:
    path_text = _normalize_repo_relative_path(str(candidate.get("repo_relative_path", ""))).lower()
    symbol_text = str(candidate.get("symbol", "")).strip().lower()
    frame_indexes = _normalized_frame_indexes(candidate)
    line_candidates = candidate.get("line_candidates", [])
    best_frame_index = min(frame_indexes) if frame_indexes else None
    semantic_category = _semantic_category(semantic_class, stream_role)
    implementation_hint = _candidate_has_implementation_hint(path_text, symbol_text, semantic_category)
    orchestration_heavy = _candidate_is_orchestration_heavy(path_text, symbol_text)
    semantic_class_path_match = 0
    if semantic_category == "communication":
        semantic_class_path_match = 12 if implementation_hint else (-10 if orchestration_heavy else 0)
    elif semantic_category == "compute":
        semantic_class_path_match = 10 if implementation_hint else (-8 if orchestration_heavy else 0)
    return {
        "implementation_path_hint": 10 if implementation_hint else 0,
        "orchestration_path_penalty": -8 if orchestration_heavy else 0,
        "op_name_symbol_match": _semantic_op_match_score(path_text, symbol_text, op_name, span_name, semantic_category),
        "semantic_class_path_match": semantic_class_path_match,
        "non_wrapper_symbol": 10 if symbol_text and symbol_text not in {"forward", "replay"} else 0,
        # Keep frame depth as a secondary hint only; avoid letting outer wrappers dominate the ranking.
        "frame_depth_preference": max(0, 8 - min(best_frame_index, 8)) if best_frame_index is not None else 0,
        "line_candidate_count": min(len(line_candidates), 4) if isinstance(line_candidates, list) else 0,
        "replay_penalty": -30 if "replay" in symbol_text else 0,
    }


def _function_candidate_score(
    candidate: dict[str, Any],
    *,
    op_name: str = "",
    semantic_class: str = "",
    stream_role: str = "",
    span_name: str = "",
) -> int:
    return sum(
        _function_candidate_score_breakdown(
            candidate,
            op_name=op_name,
            semantic_class=semantic_class,
            stream_role=stream_role,
            span_name=span_name,
        ).values()
    )


def _valid_file_function(candidate: dict[str, Any]) -> bool:
    return (
        isinstance(candidate, dict)
        and bool(_normalize_repo_relative_path(candidate.get("repo_relative_path", "")))
        and bool(str(candidate.get("symbol", "")).strip())
        and int(candidate.get("entry_line", 0) or 0) > 0
    )


def _valid_code_line_candidate(candidate: dict[str, Any]) -> bool:
    return (
        isinstance(candidate, dict)
        and bool(str(candidate.get("code_location", "")).strip())
        and int(candidate.get("line", 0) or 0) > 0
    )


def _frame_to_line_candidate(frame: dict[str, Any], basis: list[str], confidence: str) -> dict[str, Any] | None:
    repo_relative_path = _normalize_repo_relative_path(frame.get("repo_relative_path", ""))
    line = int(frame.get("line", 0) or 0)
    if not repo_relative_path or line <= 0:
        return None
    symbol = str(frame.get("symbol", "")).strip()
    return {
        "code_location": f"{repo_relative_path}:{line}",
        "repo_relative_path": repo_relative_path,
        "line": line,
        "symbol": symbol,
        "basis": basis,
        "confidence": confidence,
    }


def _merge_file_function_candidates(
    *candidate_groups: list[dict[str, Any]],
    op_name: str = "",
    semantic_class: str = "",
    stream_role: str = "",
    span_name: str = "",
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for group in candidate_groups:
        for candidate in group:
            if not _valid_file_function(candidate):
                continue
            repo_relative_path = _normalize_repo_relative_path(candidate.get("repo_relative_path", ""))
            symbol = str(candidate.get("symbol", "")).strip()
            entry_line = int(candidate.get("entry_line", 0) or 0)
            key = (repo_relative_path, symbol)
            bucket = merged.setdefault(
                key,
                {
                    "file_function": f"{repo_relative_path}:{symbol}",
                    "repo_relative_path": repo_relative_path,
                    "symbol": symbol,
                    "entry_line": entry_line,
                    "line_candidates": [],
                    "frame_indexes": [],
                    "evidence_sources": [],
                },
            )
            bucket["entry_line"] = min(int(bucket.get("entry_line", entry_line) or entry_line), entry_line)
            bucket["line_candidates"].extend(candidate.get("line_candidates", []))
            bucket["frame_indexes"].extend(candidate.get("frame_indexes", []))
            bucket["evidence_sources"] = sorted(
                set(bucket.get("evidence_sources", [])) | set(candidate.get("evidence_sources", []))
            )
    ranked = []
    for candidate in merged.values():
        candidate["line_candidates"] = sorted(
            {int(line) for line in candidate.get("line_candidates", []) if int(line) > 0}
        )
        candidate["frame_indexes"] = _normalized_frame_indexes(candidate)
        best_frame_index = candidate["frame_indexes"][0] if candidate["frame_indexes"] else None
        candidate["best_frame_index"] = best_frame_index
        candidate["frame_depth_rank_reason"] = (
            "smaller_frame_index_preferred" if best_frame_index is not None else "no_frame_index_available"
        )
        candidate["score_breakdown"] = _function_candidate_score_breakdown(
            candidate,
            op_name=op_name,
            semantic_class=semantic_class,
            stream_role=stream_role,
            span_name=span_name,
        )
        candidate["score"] = sum(candidate["score_breakdown"].values())
        candidate["implementation_hint_matched"] = _candidate_has_implementation_hint(
            _normalize_repo_relative_path(str(candidate.get("repo_relative_path", "")).lower()),
            str(candidate.get("symbol", "")).strip().lower(),
            _semantic_category(semantic_class, stream_role),
        )
        candidate["orchestration_path_penalty_applied"] = _candidate_is_orchestration_heavy(
            _normalize_repo_relative_path(str(candidate.get("repo_relative_path", "")).lower()),
            str(candidate.get("symbol", "")).strip().lower(),
        )
        candidate["confidence"] = "medium"
        ranked.append(candidate)
    ranked.sort(
        key=lambda item: (
            -int(item.get("score", 0) or 0),
            int(item.get("best_frame_index", 1_000_000) or 1_000_000),
            int(item.get("entry_line", 0) or 0),
            str(item.get("repo_relative_path", "")),
            str(item.get("symbol", "")),
        )
    )
    return ranked


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    deduped = []
    for candidate in candidates:
        code_location = str(candidate.get("code_location", "")).strip()
        basis = "|".join(str(part) for part in candidate.get("basis", []))
        key = (code_location, basis)
        if not code_location or key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _best_code_line_candidates(
    repo_frames: list[dict[str, Any]],
    tracer_frames: list[dict[str, Any]],
    existing_candidates: list[dict[str, Any]],
    primary_file_function: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    primary_path = _normalize_repo_relative_path(primary_file_function.get("repo_relative_path", ""))
    primary_symbol = str(primary_file_function.get("symbol", "")).strip()
    for candidate in existing_candidates:
        if not _valid_code_line_candidate(candidate):
            continue
        line_candidate = {
            "code_location": str(candidate.get("code_location", "")).strip(),
            "repo_relative_path": _normalize_repo_relative_path(candidate.get("repo_relative_path", "")),
            "line": int(candidate.get("line", 0) or 0),
            "symbol": str(candidate.get("symbol", "")).strip(),
            "basis": list(candidate.get("basis", [])),
            "confidence": str(candidate.get("confidence", "low")).strip() or "low",
            "kind": "call_stack_line_candidate",
        }
        if (
            primary_path
            and primary_symbol
            and line_candidate["repo_relative_path"] == primary_path
            and line_candidate["symbol"] == primary_symbol
            and "primary_file_function" not in line_candidate["basis"]
        ):
            line_candidate["basis"].append("primary_file_function")
        candidates.append(line_candidate)

    for frame in repo_frames:
        basis = ["operator_call_stack"]
        if (
            primary_path
            and primary_symbol
            and _normalize_repo_relative_path(frame.get("repo_relative_path", "")) == primary_path
            and str(frame.get("symbol", "")).strip() == primary_symbol
        ):
            basis.append("primary_file_function")
        candidate = _frame_to_line_candidate(
            frame,
            basis,
            "medium",
        )
        if candidate:
            candidate["kind"] = "call_stack_line_candidate"
            candidates.append(candidate)

    for frame in tracer_frames[:3]:
        frame_path = _normalize_repo_relative_path(frame.get("repo_relative_path", ""))
        frame_line = int(frame.get("line", 0) or 0)
        if not frame_path or frame_line <= 0:
            continue
        code_location = f"{frame_path}:{frame_line}"
        basis = ["python_tracer"]
        if any(
            bool(_repo_path_variants(frame_path) & _repo_path_variants(str(repo_frame.get("repo_relative_path", ""))))
            and frame_line == int(repo_frame.get("line", -1))
            for repo_frame in repo_frames
        ):
            basis.insert(0, "operator_call_stack")
            confidence = "high"
        else:
            confidence = "low"
        candidates.append(
            {
                "code_location": code_location,
                "repo_relative_path": frame_path,
                "line": frame_line,
                "basis": basis,
                "confidence": confidence,
                "symbol": str(frame.get("symbol", "")),
                "kind": "python_tracer_line_candidate",
            }
        )
    ranked = _dedupe_candidates(candidates)
    ranked.sort(
        key=lambda item: (
            0 if "primary_file_function" in item.get("basis", []) else 1,
            0 if "python_tracer" not in item.get("basis", []) else 1,
            -int(item.get("line", 0) or 0),
        )
    )
    return ranked


def _match_tracer_frames(
    tracer_index: dict[str, Any],
    repo_frames: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    matched_tracer_frames: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    symbols_index = tracer_index.get("by_symbol", {})
    paths_index = tracer_index.get("by_path_variant", {})
    for repo_frame in repo_frames:
        repo_symbol = str(repo_frame.get("symbol", "")).strip()
        if repo_symbol:
            for tracer_frame in symbols_index.get(repo_symbol, []):
                key = (
                    str(tracer_frame.get("repo_relative_path", "")),
                    int(tracer_frame.get("line", 0) or 0),
                    str(tracer_frame.get("symbol", "")),
                )
                if key not in seen:
                    seen.add(key)
                    matched_tracer_frames.append(tracer_frame)
        for variant in _repo_path_variants(str(repo_frame.get("repo_relative_path", ""))):
            for tracer_frame in paths_index.get(variant, []):
                key = (
                    str(tracer_frame.get("repo_relative_path", "")),
                    int(tracer_frame.get("line", 0) or 0),
                    str(tracer_frame.get("symbol", "")),
                )
                if key not in seen:
                    seen.add(key)
                    matched_tracer_frames.append(tracer_frame)
    return matched_tracer_frames


def _build_tracer_index(tracer_frames: list[dict[str, Any]]) -> dict[str, Any]:
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    by_path_variant: dict[str, list[dict[str, Any]]] = {}
    for frame in tracer_frames:
        symbol = str(frame.get("symbol", "")).strip()
        if symbol:
            by_symbol.setdefault(symbol, []).append(frame)
        for variant in _repo_path_variants(str(frame.get("repo_relative_path", ""))):
            by_path_variant.setdefault(variant, []).append(frame)
    return {
        "by_symbol": by_symbol,
        "by_path_variant": by_path_variant,
    }


def _merged_call_path(repo_frames: list[dict[str, Any]], matched_tracer_frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "repo_relative_path": _normalize_repo_relative_path(frame.get("repo_relative_path", "")),
            "line": frame.get("line", 0),
            "symbol": frame.get("symbol", ""),
            "source": "operator_call_stack",
        }
        for frame in repo_frames
    ] + [
        {
            "repo_relative_path": _normalize_repo_relative_path(frame.get("repo_relative_path", "")),
            "line": frame.get("line", 0),
            "symbol": frame.get("symbol", ""),
            "source": "python_tracer",
        }
        for frame in matched_tracer_frames[:10]
        if not any(
            bool(
                _repo_path_variants(str(frame.get("repo_relative_path", "")))
                & _repo_path_variants(str(repo_frame.get("repo_relative_path", "")))
            )
            and int(frame.get("line", -1)) == int(repo_frame.get("line", -2))
            for repo_frame in repo_frames
        )
    ]


def _graph_kind_from_rows(rows: list[dict[str, Any]], default_phase: str) -> tuple[str, str, str]:
    text = " ".join(
        [default_phase]
        + [str(row.get("raw_call_stack", "")) for row in rows]
        + [str(frame.get("symbol", "")) for row in rows for frame in row.get("repo_frames", [])]
    ).lower()
    if any(keyword in text for keyword in ["prepare_for_v2_verify", "verify_tree", "verify", "build_tree", "greedy"]):
        return "verify_graph_replay", "verify", "high"
    if any(
        keyword in text
        for keyword in [
            "_draft_extend_for_decode",
            "draft_extend",
            "draft_prefill",
            "prefill",
            "alloc_extend_kernel",
            "extend",
            "assign_draft_cache_locs",
            "draft_cache",
            "cache_loc",
        ]
    ):
        return "draft_prefill_graph_replay", "draft_prefill", "high"
    if "draft_decode" in text or "decode" in text:
        return "draft_decode_graph_replay", "draft_decode", "medium"
    if "replay" in text or "graph" in text:
        return "decode_graph_replay", "decode", "low"
    return "unknown", default_phase or "unknown", "low"


def _load_external_mapping_target_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    raw_path = str(state.get("artifacts", {}).get("external_mapping_targets_path", "")).strip()
    if not raw_path:
        raise RuntimeError("缺少 state.artifacts.external_mapping_targets_path。")
    path = Path(raw_path)
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"external_mapping_targets.json 不存在: {path}")
    payload = load_json(path)
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        raise RuntimeError("external_mapping_targets.json.rows 必须是列表。")
    target_rows = []
    seen_span_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        span_id = str(row.get("span_id", "")).strip()
        if not span_id or span_id in seen_span_ids:
            continue
        seen_span_ids.add(span_id)
        target_rows.append(dict(row))
    return target_rows


def build_stack_call_paths_for_workspace(workspace_dir: Path) -> dict[str, Any]:
    state = load_state(workspace_dir)
    stack_evidence_path = _resolve_stack_evidence_input_path(state)
    stack_evidence = _safe_load_json(str(stack_evidence_path) if stack_evidence_path else "")
    external_mapping_targets = _load_external_mapping_target_rows(state)
    python_tracer_index = _safe_load_json(str(state["artifacts"].get("python_tracer_index_path", "")))

    tracer_frames = python_tracer_index.get("frames", [])
    tracer_index = _build_tracer_index(tracer_frames)
    rows = []
    for row in stack_evidence.get("rows", []):
        repo_frames = row.get("repo_frames", [])
        matched_tracer_frames = _match_tracer_frames(tracer_index, repo_frames)
        row_op_name = str(row.get("op_name", "")).strip()
        stack_file_function_candidates = row.get("file_function_candidates", [])
        tracer_file_function_candidates = _merge_file_function_candidates(
            [
                {
                    "repo_relative_path": _normalize_repo_relative_path(frame.get("repo_relative_path", "")),
                    "symbol": str(frame.get("symbol", "")).strip(),
                    "entry_line": int(frame.get("line", 0) or 0),
                    "line_candidates": [int(frame.get("line", 0) or 0)],
                    "frame_indexes": [index],
                    "evidence_sources": ["python_tracer"],
                }
                for index, frame in enumerate(matched_tracer_frames)
            ],
            op_name=row_op_name,
        )
        merged_function_candidates = _merge_file_function_candidates(
            stack_file_function_candidates,
            tracer_file_function_candidates,
            op_name=row_op_name,
        )
        primary_file_function = merged_function_candidates[0] if merged_function_candidates else {}
        code_line_candidates = _best_code_line_candidates(
            repo_frames,
            matched_tracer_frames,
            row.get("code_line_candidates", []),
            primary_file_function,
        )
        rows.append(
            {
                "op_row_id": row.get("op_row_id", ""),
                "op_name": row.get("op_name", ""),
                "repo_call_path": [
                    {
                        "repo_relative_path": _normalize_repo_relative_path(frame.get("repo_relative_path", "")),
                        "line": frame.get("line", 0),
                        "symbol": frame.get("symbol", ""),
                        "frame_class": frame.get("frame_class", ""),
                    }
                    for frame in repo_frames
                ],
                "python_tracer_frames": matched_tracer_frames[:10],
                "merged_call_path": _merged_call_path(repo_frames, matched_tracer_frames),
                "file_function_candidates": merged_function_candidates[:10],
                "primary_file_function": primary_file_function,
                "code_line_candidates": code_line_candidates[:10],
                "selection_notes": [
                    "优先围绕 repo 调用栈与 tracer 共同收敛出的文件:函数候选定位 span 所处函数。",
                    "再结合 python tracer 的同 symbol/path 命中补充代码行候选，供 Step4 子 agent 结合语义和左右 span 正式选线。",
                    "候选排序已联合考虑 span 语义、实现层路径提示和协调层惩罚；高频协调入口不应仅因通用 score 被直接视为最终定位。",
                ],
            }
        )

    op_row_map = {str(row.get("op_row_id", "")): row for row in stack_evidence.get("op_rows", stack_evidence.get("rows", []))}
    external_span_row_map = {
        str(row.get("span_id", "")).strip(): row
        for row in stack_evidence.get("external_span_rows", [])
        if isinstance(row, dict) and str(row.get("span_id", "")).strip()
    }
    external_span_rows = []
    for target_row in external_mapping_targets:
        span_id = str(target_row.get("span_id", "")).strip()
        span_row = external_span_row_map.get(span_id, {})
        span_repo_frames = []
        for op_row_id in span_row.get("matched_stack_rows", []):
            span_repo_frames.extend(op_row_map.get(str(op_row_id), {}).get("repo_frames", []))
        matched_tracer_frames = _match_tracer_frames(tracer_index, span_repo_frames)
        semantic_class = str(
            span_row.get("semantic_class", target_row.get("semantic_class", "unknown"))
        ).strip() or "unknown"
        target_scope_snapshot = dict(target_row.get("step3_scope_snapshot", {}) or {})
        stream_role = str(
            span_row.get("stream_role", target_scope_snapshot.get("stream_role", semantic_class))
        ).strip() or semantic_class
        op_name_context = " ".join(
            sorted(
                {
                    str(op_row_map.get(str(op_row_id), {}).get("op_name", "")).strip()
                    for op_row_id in span_row.get("matched_stack_rows", [])
                    if str(op_row_map.get(str(op_row_id), {}).get("op_name", "")).strip()
                }
            )
        )
        tracer_file_function_candidates = _merge_file_function_candidates(
            [
                {
                    "repo_relative_path": _normalize_repo_relative_path(frame.get("repo_relative_path", "")),
                    "symbol": str(frame.get("symbol", "")).strip(),
                    "entry_line": int(frame.get("line", 0) or 0),
                    "line_candidates": [int(frame.get("line", 0) or 0)],
                    "frame_indexes": [index],
                    "evidence_sources": ["python_tracer"],
                }
                for index, frame in enumerate(matched_tracer_frames)
            ],
            op_name=op_name_context,
            semantic_class=semantic_class,
            stream_role=stream_role,
            span_name=str(span_row.get("span_name", "")).strip(),
        )
        file_function_candidates = _merge_file_function_candidates(
            span_row.get("file_function_candidates", []),
            tracer_file_function_candidates,
            op_name=op_name_context,
            semantic_class=semantic_class,
            stream_role=stream_role,
            span_name=str(span_row.get("span_name", "")).strip(),
        )
        primary_file_function = file_function_candidates[0] if file_function_candidates else {}
        code_line_candidates = _best_code_line_candidates(
            span_repo_frames,
            matched_tracer_frames,
            span_row.get("code_line_candidates", []),
            primary_file_function,
        )
        external_span_rows.append(
            {
                "external_target_row_id": str(target_row.get("external_target_row_id", "")).strip(),
                "span_id": span_id,
                "stream_id": str(span_row.get("stream_id", target_row.get("stream_id", ""))).strip(),
                "parallel_group": str(span_row.get("parallel_group", target_row.get("parallel_group", ""))).strip(),
                "span_name": str(span_row.get("span_name", target_row.get("span_name", ""))).strip(),
                "semantic_class": semantic_class,
                "stream_role": stream_role,
                "scope_class": str(target_scope_snapshot.get("scope_class", span_row.get("scope_class", ""))).strip(),
                "exclude_from_code_mapping": bool(
                    target_scope_snapshot.get("exclude_from_code_mapping", span_row.get("exclude_from_code_mapping", False))
                ),
                "exclude_reason": str(target_scope_snapshot.get("exclude_reason", span_row.get("exclude_reason", ""))).strip(),
                "external_mapping_required": bool(
                    target_scope_snapshot.get("external_mapping_required", span_row.get("external_mapping_required", True))
                ),
                "has_stream_id": bool(target_scope_snapshot.get("has_stream_id", span_row.get("has_stream_id", True))),
                "approved_for_external_mapping": bool(target_row.get("approved_for_external_mapping", True)),
                "target_scope_reason": str(target_row.get("target_scope_reason", "")).strip(),
                "step3_scope_snapshot": target_scope_snapshot,
                "stream_id_source": "shared_external_target_freeze",
                "task_ids": span_row.get("task_ids", target_row.get("task_ids", [])),
                "op_row_ids": span_row.get("op_row_ids", target_row.get("op_row_ids", [])),
                "matched_stack_rows": span_row.get("matched_stack_rows", []),
                "repo_call_path": [
                    {
                        "repo_relative_path": _normalize_repo_relative_path(frame.get("repo_relative_path", "")),
                        "line": frame.get("line", 0),
                        "symbol": frame.get("symbol", ""),
                        "frame_class": frame.get("frame_class", ""),
                    }
                    for frame in span_repo_frames
                ],
                "python_tracer_frames": matched_tracer_frames[:10],
                "merged_call_path": _merged_call_path(span_repo_frames, matched_tracer_frames),
                "file_function_candidates": file_function_candidates[:10],
                "primary_file_function": primary_file_function,
                "code_line_candidates": code_line_candidates[:10],
                "implementation_evidence_present": bool(span_row.get("implementation_evidence_present", False)),
                "communication_impl_evidence_present": bool(span_row.get("communication_impl_evidence_present", False)),
                "compute_impl_evidence_present": bool(span_row.get("compute_impl_evidence_present", False)),
                "mapping_risk_flags": span_row.get("mapping_risk_flags", [])[:5],
                "recommended_primary_location_kind": str(span_row.get("recommended_primary_location_kind", "")).strip(),
                "recommended_unresolved_reason": str(span_row.get("recommended_unresolved_reason", "")).strip(),
                "semantic_target_candidates": [
                    {
                        "code_location": candidate.get("code_location", ""),
                        "confidence": candidate.get("confidence", "low"),
                        "basis": candidate.get("basis", []),
                        "kind": candidate.get("kind", ""),
                    }
                    for candidate in code_line_candidates[:5]
                ],
                "is_device_tensor_related": bool(span_row.get("is_device_tensor_related", False)),
                "selection_notes": [
                    "Step4 只消费 external_mapping_targets.json 已冻结的 formal graph 外 target set，不得自行扩写目标范围。",
                    "以 graph 外 span 为单位合并 operator call stack 与 python tracer 证据，先收敛 repo 文件:函数，再结合语义和左右 span 从 code_line_candidates 中正式选线。",
                    "若 communication span 缺少实现层 repo frame，应优先降级到 function_entry_fallback 或 unresolved，而不是把调度层函数包装成高质量精确定位。",
                ],
            }
        )

    graph_phase_rows = []
    phase_source_rows = stack_evidence.get("graph_phase_marker_rows", stack_evidence.get("graph_replay_rows", []))
    for phase_row in phase_source_rows:
        matched_rows = [op_row_map.get(str(op_row_id), {}) for op_row_id in phase_row.get("matched_stack_rows", [])]
        matched_rows = [row for row in matched_rows if row]
        if matched_rows:
            matched_graph_kind, candidate_phase, phase_confidence = _graph_kind_from_rows(
                matched_rows,
                str(phase_row.get("candidate_phase", "unknown")),
            )
        else:
            matched_graph_kind = str(phase_row.get("matched_graph_kind", "")).strip() or "unknown"
            candidate_phase = str(phase_row.get("candidate_phase", "unknown")).strip() or "unknown"
            phase_confidence = str(phase_row.get("phase_confidence", "")).strip() or "medium"
        graph_phase_rows.append(
            {
                "span_id": phase_row.get("span_id", ""),
                "parallel_group": phase_row.get("parallel_group", ""),
                "stream_id": phase_row.get("stream_id", ""),
                "start_ns": int(phase_row.get("start_ns", 0) or 0),
                "end_ns": int(phase_row.get("end_ns", 0) or 0),
                "span_name": phase_row.get("span_name", ""),
                "file_function_candidates": phase_row.get("file_function_candidates", [])[:10],
                "primary_file_function": phase_row.get("primary_file_function", {}),
                "matched_graph_kind": matched_graph_kind,
                "candidate_phase": candidate_phase,
                "phase_confidence": phase_confidence,
                "phase_source": phase_row.get("phase_source", ""),
                "replay_anchor_file": phase_row.get("replay_anchor_file", ""),
                "marker_kind": phase_row.get("marker_kind", ""),
                "evidence_sources": phase_row.get("evidence_sources", []),
                "matched_frames": phase_row.get("matched_frames", []),
                "matched_stack_rows": phase_row.get("matched_stack_rows", []),
                "supporting_span_ids": phase_row.get("supporting_span_ids", []),
                "reason": phase_row.get("reason", ""),
            }
        )

    output = {
        "schema_version": "stack_call_paths_v2",
        "stack_evidence_path": str(stack_evidence_path) if stack_evidence_path else state["artifacts"]["stack_evidence_path"],
        "external_mapping_targets_path": str(state["artifacts"].get("external_mapping_targets_path", "")),
        "python_tracer_index_path": state["artifacts"]["python_tracer_index_path"],
        "rows": rows,
        "external_span_rows": external_span_rows,
        "graph_phase_rows": graph_phase_rows,
        "summary": {
            "op_row_count": len(rows),
            "external_span_row_count": len(external_span_rows),
            "graph_phase_row_count": len(graph_phase_rows),
        },
    }
    output_path = workspace_dir / "artifacts" / "mapping" / "stack_call_paths.json"
    dump_json(output_path, output)
    state["artifacts"]["stack_call_paths_path"] = str(output_path)
    state["flags"]["stack_call_paths_built"] = True
    save_state(workspace_dir, state)
    return output


def main() -> int:
    args = build_parser().parse_args()
    build_stack_call_paths_for_workspace(Path(args.workspace_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
