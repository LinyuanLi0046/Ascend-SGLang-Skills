from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from typing import Any

from workflow_common import dump_json, iter_classified_streams, load_json, load_state, normalize_repo_relative_path, save_state


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


VALID_SPEC_MODES = {"spec_v2", "decode_graph", "disabled"}


FRAME_RE = re.compile(r"(?P<path>/[^()\n]+)\((?P<line>\d+)\):\s*(?P<symbol>[^;]+)")
EXTERNAL_MARKERS = [
    "site-packages",
    "torch_npu",
    "/usr/local/python",
    "/usr/lib/python",
]
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
DEVICE_TEXT_KEYWORDS = [
    "tensor",
    "matmul",
    "mul",
    "add",
    "sub",
    "div",
    "reshape",
    "permute",
    "view",
    "index",
    "cat",
    "gather",
    "scatter",
    "allreduce",
    "all_reduce",
    "broadcast",
    "reduce_scatter",
    "allgather",
    "torch.",
    "torch_npu",
    "torch.ops",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从 operator_details 构建 stack_evidence.json。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def _normalize_text(value: str) -> str:
    return str(value or "").replace("\\", "/").strip().lower()


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
    normalized_stream_role = _normalize_text(stream_role)
    if normalized_stream_role in {"communication", "compute"}:
        return normalized_stream_role
    normalized_semantic_class = _normalize_text(semantic_class)
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
    op_text = _normalize_text(f"{op_name} {span_name}")
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
    path_text = _normalize_text(str(candidate.get("repo_relative_path", "")))
    symbol_text = _normalize_text(str(candidate.get("symbol", "")))
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


def parse_call_stack(raw_call_stack: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for index, chunk in enumerate(raw_call_stack.split(";")):
        match = FRAME_RE.search(chunk.strip())
        if not match:
            continue
        frames.append(
            {
                "path": match.group("path"),
                "line": int(match.group("line")),
                "symbol": match.group("symbol").strip(),
                "frame_index": index,
            }
        )
    return frames


def is_repo_frame(frame_path: str, repo_name: str) -> bool:
    normalized = frame_path.replace("\\", "/")
    return f"/{repo_name}/" in normalized or normalized.startswith(f"{repo_name}/")


def frame_class(frame_path: str) -> str:
    normalized = frame_path.replace("\\", "/")
    if any(marker in normalized for marker in EXTERNAL_MARKERS):
        return "external_python"
    if normalized.endswith(".py"):
        return "repo_python"
    return "other"


def select_repo_frames(frames: list[dict[str, Any]], repo_root: Path) -> list[dict[str, Any]]:
    repo_name = repo_root.name
    selected: list[dict[str, Any]] = []
    for frame in frames:
        path_str = str(frame["path"])
        if not is_repo_frame(path_str, repo_name):
            continue
        repo_relative_path = normalize_repo_relative_path(path_str, repo_root)
        symbol = str(frame.get("symbol", "")).strip()
        selected.append(
            {
                **frame,
                "repo_relative_path": repo_relative_path,
                "frame_class": frame_class(path_str),
            }
        )
    return selected


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


def _build_file_function_candidates_from_frames(
    repo_frames: list[dict[str, Any]],
    evidence_source: str,
    *,
    op_name: str = "",
    semantic_class: str = "",
    stream_role: str = "",
    span_name: str = "",
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for frame in repo_frames:
        repo_relative_path = str(frame.get("repo_relative_path", "")).strip()
        symbol = str(frame.get("symbol", "")).strip()
        line = int(frame.get("line", 0) or 0)
        if not repo_relative_path or not symbol or line <= 0:
            continue
        key = (repo_relative_path, symbol)
        candidate = grouped.setdefault(
            key,
            {
                "repo_relative_path": repo_relative_path,
                "symbol": symbol,
                "entry_line": line,
                "line_candidates": [],
                "frame_indexes": [],
                "evidence_sources": [evidence_source],
            },
        )
        candidate["entry_line"] = min(int(candidate.get("entry_line", line) or line), line)
        candidate["line_candidates"].append(line)
        candidate["frame_indexes"].append(int(frame.get("frame_index", 0) or 0))
        evidence_sources = set(candidate.get("evidence_sources", []))
        evidence_sources.add(evidence_source)
        candidate["evidence_sources"] = sorted(evidence_sources)

    ranked = []
    for candidate in grouped.values():
        candidate["line_candidates"] = sorted({int(line) for line in candidate.get("line_candidates", []) if int(line) > 0})
        candidate["frame_indexes"] = _normalized_frame_indexes(candidate)
        best_frame_index = candidate["frame_indexes"][0] if candidate["frame_indexes"] else None
        candidate["confidence"] = "medium"
        candidate["file_function"] = f"{candidate['repo_relative_path']}:{candidate['symbol']}"
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
            _normalize_text(candidate.get("repo_relative_path", "")),
            _normalize_text(candidate.get("symbol", "")),
            _semantic_category(semantic_class, stream_role),
        )
        candidate["orchestration_path_penalty_applied"] = _candidate_is_orchestration_heavy(
            _normalize_text(candidate.get("repo_relative_path", "")),
            _normalize_text(candidate.get("symbol", "")),
        )
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


def _dedupe_code_line_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for item in candidates:
        code_location = str(item.get("code_location", "")).strip()
        basis = "|".join(str(part) for part in item.get("basis", []))
        key = (code_location, basis)
        if not code_location or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _frame_to_line_candidate(frame: dict[str, Any], basis: list[str], confidence: str) -> dict[str, Any] | None:
    repo_relative_path = str(frame.get("repo_relative_path", "")).strip()
    line = int(frame.get("line", 0) or 0)
    if not repo_relative_path or line <= 0:
        return None
    return {
        "code_location": f"{repo_relative_path}:{line}",
        "repo_relative_path": repo_relative_path,
        "line": line,
        "symbol": str(frame.get("symbol", "")).strip(),
        "basis": basis,
        "confidence": confidence,
    }


def _build_code_line_candidates(
    repo_frames: list[dict[str, Any]],
    primary_file_function: dict[str, Any],
    evidence_source: str,
) -> list[dict[str, Any]]:
    primary_path = str(primary_file_function.get("repo_relative_path", "")).strip()
    primary_symbol = str(primary_file_function.get("symbol", "")).strip()
    candidates: list[dict[str, Any]] = []
    for frame in repo_frames:
        basis = [evidence_source]
        if primary_path and primary_symbol:
            if (
                str(frame.get("repo_relative_path", "")).strip() == primary_path
                and str(frame.get("symbol", "")).strip() == primary_symbol
            ):
                basis.append("primary_file_function")
        candidate = _frame_to_line_candidate(frame, basis, "medium")
        if candidate:
            candidates.append(candidate)
    candidates.sort(
        key=lambda item: (
            0 if "primary_file_function" in item.get("basis", []) else 1,
            -int(item.get("line", 0) or 0),
        )
    )
    return _dedupe_code_line_candidates(candidates)


def _select_primary_file_function(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return candidates[0] if candidates else {}


def detect_replay_anchor(frames: list[dict[str, Any]]) -> bool:
    return any(str(frame.get("symbol", "")).strip().lower() == "replay" for frame in frames)


def detect_spec_v2_anchor(frames: list[dict[str, Any]]) -> bool:
    text = " ".join(str(frame.get("path", "")) + " " + str(frame.get("symbol", "")) for frame in frames).lower()
    return any(keyword in text for keyword in ["eagle", "spec", "draft", "verify"])


def load_runtime_constraints(workspace_dir: Path) -> dict[str, Any]:
    path = workspace_dir / "input" / "runtime_constraints.json"
    ensure(
        path.exists() and path.is_file(),
        f"缺少 runtime_constraints.json: {path}。请先通过 prepare_agent_dispatch/bootstrap 生成正式前置工件。",
    )
    payload = load_json(path)
    spec_mode = str(payload.get("spec_mode", "")).strip()
    ensure(
        spec_mode in VALID_SPEC_MODES,
        f"runtime_constraints.json 缺少有效 spec_mode，当前为 {spec_mode or '<missing>'}。",
    )
    return payload


def read_operator_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def build_payload_rows(rows: list[dict[str, str]], repo_root: Path) -> list[dict[str, Any]]:
    payload_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        raw_stack = row.get("Call Stack", "").strip()
        if not raw_stack:
            continue
        frames = parse_call_stack(raw_stack)
        repo_frames = select_repo_frames(frames, repo_root)
        file_function_candidates = _build_file_function_candidates_from_frames(repo_frames, "operator_call_stack")
        primary_file_function = _select_primary_file_function(file_function_candidates)
        code_line_candidates = _build_code_line_candidates(repo_frames, primary_file_function, "operator_call_stack")
        matched_op_summary_row_id = row.get("matched_op_summary_row_id", "").strip()
        op_row_id = f"op_{matched_op_summary_row_id}" if matched_op_summary_row_id else f"op_{row.get('slice_row_id', f'{index:06d}')}"
        payload_rows.append(
            {
                "op_row_id": op_row_id,
                "op_name": row.get("Name", "").strip(),
                "raw_call_stack": raw_stack,
                "parsed_frames": frames,
                "repo_frames": repo_frames,
                "file_function_candidates": file_function_candidates[:20],
                "primary_file_function": primary_file_function,
                "code_line_candidates": code_line_candidates[:20],
                "has_replay_anchor": detect_replay_anchor(frames),
                "has_spec_v2_anchor": detect_spec_v2_anchor(frames),
                "matched_op_summary_row_id": matched_op_summary_row_id,
            }
        )
    return payload_rows


def _device_tensor_related(span_name: str, op_names: list[str]) -> bool:
    text = " ".join([span_name, *op_names]).lower()
    return any(keyword in text for keyword in DEVICE_TEXT_KEYWORDS)


def _detect_replay_anchor_from_rows(matched_rows: list[dict[str, Any]], spec_mode: str) -> dict[str, str]:
    replay_frames: list[dict[str, str]] = []
    for row in matched_rows:
        for frame in row.get("repo_frames", []):
            symbol = str(frame.get("symbol", "")).strip().lower()
            if symbol != "replay":
                continue
            replay_frames.append(
                {
                    "repo_relative_path": str(frame.get("repo_relative_path", "")).replace("\\", "/").strip().lower(),
                    "symbol": symbol,
                }
            )

    for frame in replay_frames:
        repo_relative_path = frame["repo_relative_path"]
        if "eagle_draft_extend_cuda_graph_runner.py" in repo_relative_path:
            return {
                "matched_graph_kind": "draft_prefill_graph_replay",
                "candidate_phase": "draft_prefill",
                "phase_confidence": "high",
                "phase_source": "replay_anchor_file",
                "replay_anchor_file": "sglang/srt/speculative/eagle_draft_extend_cuda_graph_runner.py",
                "replay_anchor_symbol": "replay",
            }
        if "eagle_draft_cuda_graph_runner.py" in repo_relative_path:
            return {
                "matched_graph_kind": "draft_decode_graph_replay",
                "candidate_phase": "draft_decode",
                "phase_confidence": "high",
                "phase_source": "replay_anchor_file",
                "replay_anchor_file": "sglang/srt/speculative/eagle_draft_cuda_graph_runner.py",
                "replay_anchor_symbol": "replay",
            }
        if "npu_graph_runner.py" in repo_relative_path:
            if spec_mode == "spec_v2":
                return {
                    "matched_graph_kind": "verify_graph_replay",
                    "candidate_phase": "verify",
                    "phase_confidence": "high",
                    "phase_source": "replay_anchor_file",
                    "replay_anchor_file": "sglang/srt/hardware_backend/npu/graph_runner/npu_graph_runner.py",
                    "replay_anchor_symbol": "replay",
                }
            return {
                "matched_graph_kind": "decode_graph_replay",
                "candidate_phase": "decode",
                "phase_confidence": "high",
                "phase_source": "replay_anchor_file",
                "replay_anchor_file": "sglang/srt/hardware_backend/npu/graph_runner/npu_graph_runner.py",
                "replay_anchor_symbol": "replay",
            }

    if replay_frames:
        return {
            "detection_error": "detected replay text but did not match a supported replay runner file",
            "matched_graph_kind": "",
            "candidate_phase": "",
            "phase_confidence": "",
            "phase_source": "unsupported_replay_anchor",
            "replay_anchor_file": "",
            "replay_anchor_symbol": "replay",
        }
    return {}


def _op_rows_by_id(op_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("op_row_id", "")): row for row in op_rows if str(row.get("op_row_id", "")).strip()}


def _base_phase_marker_row(
    span: dict[str, Any],
    *,
    candidate_phase: str,
    matched_graph_kind: str,
    phase_source: str,
    phase_confidence: str,
    replay_anchor_file: str = "",
    reason: str = "",
    supporting_span_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "span_id": str(span.get("span_id", "")).strip(),
        "parallel_group": str(span.get("parallel_group", "")).strip(),
        "stream_id": str(span.get("stream_id", "")).strip(),
        "start_ns": int(span.get("start_ns", 0) or 0),
        "end_ns": int(span.get("end_ns", 0) or 0),
        "span_name": str(span.get("span_name", "")).strip(),
        "task_ids": list(span.get("task_ids", []) or []),
        "op_row_ids": list(span.get("op_row_ids", []) or []),
        "matched_stack_rows": list(span.get("op_row_ids", []) or []),
        "file_function_candidates": [],
        "primary_file_function": {},
        "matched_graph_kind": matched_graph_kind,
        "candidate_phase": candidate_phase,
        "phase_confidence": phase_confidence,
        "phase_source": phase_source,
        "replay_anchor_file": replay_anchor_file,
        "replay_anchor_symbol": "replay" if replay_anchor_file else "",
        "marker_kind": "MODEL_EXECUTE",
        "evidence_sources": ["timeline_model_execute_marker"],
        "matched_frames": [],
        "supporting_span_ids": list(supporting_span_ids or []),
        "reason": reason,
    }


def _segment_index_for_ts(markers: list[dict[str, Any]], timestamp_ns: int) -> int:
    for index, marker in enumerate(markers):
        next_start_ns = int(markers[index + 1]["start_ns"]) if index + 1 < len(markers) else 2**63 - 1
        if int(marker["start_ns"]) <= timestamp_ns < next_start_ns:
            return index
    return -1


def _build_phase_marker_rows(
    model_execute_spans: list[dict[str, Any]],
    replay_support_rows: list[dict[str, Any]],
    spec_mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered_markers = sorted(
        model_execute_spans,
        key=lambda item: (int(item.get("start_ns", 0) or 0), int(item.get("end_ns", 0) or 0), str(item.get("span_id", ""))),
    )
    detection_errors: list[dict[str, Any]] = []
    if not ordered_markers:
        detection_errors.append(
            {
                "span_id": "",
                "span_name": "MODEL_EXECUTE",
                "parallel_group": "",
                "stream_id": "",
                "matched_stack_rows": [],
                "message": "Step4 未找到任何 MODEL_EXECUTE marker，无法建立 graph phase 起点。",
            }
        )
        return [], detection_errors

    if spec_mode == "spec_v2":
        if len(ordered_markers) not in {2, 3}:
            detection_errors.append(
                {
                    "span_id": "",
                    "span_name": "MODEL_EXECUTE",
                    "parallel_group": "",
                    "stream_id": "",
                    "matched_stack_rows": [],
                    "message": f"spec_v2 场景要求时间窗内存在 2 或 3 个 MODEL_EXECUTE，当前为 {len(ordered_markers)}。",
                }
            )
            return [], detection_errors
        verify_rows = [
            row
            for row in replay_support_rows
            if str(row.get("candidate_phase", "")).strip() == "verify"
            and str(row.get("replay_anchor_file", "")).strip().endswith("npu_graph_runner.py")
        ]
        if not verify_rows:
            detection_errors.append(
                {
                    "span_id": "",
                    "span_name": "MODEL_EXECUTE",
                    "parallel_group": "",
                    "stream_id": "",
                    "matched_stack_rows": [],
                    "message": "spec_v2 场景未找到 npu_graph_runner.py::replay 对应的 verify 支撑证据，无法确认 verify MODEL_EXECUTE。",
                }
            )
            return [], detection_errors
        earliest_verify_start_ns = min(int(row.get("start_ns", 0) or 0) for row in verify_rows)
        verify_index = _segment_index_for_ts(ordered_markers, earliest_verify_start_ns)
        if verify_index < 0:
            detection_errors.append(
                {
                    "span_id": "",
                    "span_name": "MODEL_EXECUTE",
                    "parallel_group": "",
                    "stream_id": "",
                    "matched_stack_rows": [],
                    "message": "未能把 verify replay 证据归入任一 MODEL_EXECUTE 分段，无法确认 verify graph 起点。",
                }
            )
            return [], detection_errors
        if verify_index != 0:
            detection_errors.append(
                {
                    "span_id": str(ordered_markers[verify_index].get("span_id", "")).strip(),
                    "span_name": "MODEL_EXECUTE",
                    "parallel_group": str(ordered_markers[verify_index].get("parallel_group", "")).strip(),
                    "stream_id": str(ordered_markers[verify_index].get("stream_id", "")).strip(),
                    "matched_stack_rows": [],
                    "message": f"verify MODEL_EXECUTE 不是时间序列中的第一个 marker，当前 verify_index={verify_index}，与实际 profiling 规则不符。",
                }
            )
            return [], detection_errors

        phase_sequence = ["verify", "draft_prefill"] + (["draft_decode"] if len(ordered_markers) == 3 else [])
        phase_rows: list[dict[str, Any]] = []
        for index, marker in enumerate(ordered_markers):
            phase = phase_sequence[index]
            supporting_rows = [
                row
                for row in replay_support_rows
                if _segment_index_for_ts(ordered_markers, int(row.get("start_ns", 0) or 0)) == index
            ]
            supporting_span_ids = sorted({str(row.get("span_id", "")).strip() for row in supporting_rows if str(row.get("span_id", "")).strip()})
            if phase == "verify":
                phase_rows.append(
                    _base_phase_marker_row(
                        marker,
                        candidate_phase="verify",
                        matched_graph_kind="verify_graph_phase_start",
                        phase_source="model_execute_marker_verify_replay_confirmed",
                        phase_confidence="high",
                        replay_anchor_file="sglang/srt/hardware_backend/npu/graph_runner/npu_graph_runner.py",
                        reason="MODEL_EXECUTE marker aligned to spec_v2 verify start; verify identity confirmed by npu_graph_runner.py::replay evidence in the same phase segment.",
                        supporting_span_ids=supporting_span_ids,
                    )
                )
                continue
            phase_rows.append(
                _base_phase_marker_row(
                    marker,
                    candidate_phase=phase,
                    matched_graph_kind=f"{phase}_graph_phase_start",
                    phase_source="model_execute_marker_time_order",
                    phase_confidence="high",
                    reason=f"MODEL_EXECUTE marker assigned as {phase} start by spec_v2 MODEL_EXECUTE order after verify.",
                    supporting_span_ids=supporting_span_ids,
                )
            )
        return phase_rows, detection_errors

    if spec_mode == "decode_graph":
        if len(ordered_markers) != 1:
            detection_errors.append(
                {
                    "span_id": "",
                    "span_name": "MODEL_EXECUTE",
                    "parallel_group": "",
                    "stream_id": "",
                    "matched_stack_rows": [],
                    "message": f"decode_graph 场景要求时间窗内仅有 1 个 MODEL_EXECUTE，当前为 {len(ordered_markers)}。",
                }
            )
            return [], detection_errors
        return (
            [
                _base_phase_marker_row(
                    ordered_markers[0],
                    candidate_phase="decode",
                    matched_graph_kind="decode_graph_phase_start",
                    phase_source="model_execute_marker_decode_only",
                    phase_confidence="high",
                    reason="MODEL_EXECUTE marker assigned as decode graph start because speculative mode is disabled.",
                )
            ],
            detection_errors,
        )

    detection_errors.append(
        {
            "span_id": "",
            "span_name": "MODEL_EXECUTE",
            "parallel_group": "",
            "stream_id": "",
            "matched_stack_rows": [],
            "message": f"当前 spec_mode={spec_mode or '<missing>'} 不支持基于 MODEL_EXECUTE 识别 graph phase。",
        }
    )
    return [], detection_errors


def _merge_file_function_candidates(
    candidates: list[dict[str, Any]],
    *,
    op_name: str = "",
    semantic_class: str = "",
    stream_role: str = "",
    span_name: str = "",
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in candidates:
        repo_relative_path = str(candidate.get("repo_relative_path", "")).strip()
        symbol = str(candidate.get("symbol", "")).strip()
        entry_line = int(candidate.get("entry_line", 0) or 0)
        if not repo_relative_path or not symbol or entry_line <= 0:
            continue
        key = (repo_relative_path, symbol)
        bucket = merged.setdefault(
            key,
            {
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
        candidate["confidence"] = "medium"
        candidate["file_function"] = f"{candidate['repo_relative_path']}:{candidate['symbol']}"
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
            _normalize_text(candidate.get("repo_relative_path", "")),
            _normalize_text(candidate.get("symbol", "")),
            _semantic_category(semantic_class, stream_role),
        )
        candidate["orchestration_path_penalty_applied"] = _candidate_is_orchestration_heavy(
            _normalize_text(candidate.get("repo_relative_path", "")),
            _normalize_text(candidate.get("symbol", "")),
        )
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


def _merge_code_line_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _dedupe_code_line_candidates(candidates)


def _frame_matches_semantic_impl(frame: dict[str, Any], semantic_category: str) -> bool:
    path_text = _normalize_text(str(frame.get("repo_relative_path", "")))
    symbol_text = _normalize_text(str(frame.get("symbol", "")))
    return _candidate_has_implementation_hint(path_text, symbol_text, semantic_category)


def _frame_is_orchestration_heavy(frame: dict[str, Any]) -> bool:
    return _candidate_is_orchestration_heavy(
        _normalize_text(str(frame.get("repo_relative_path", ""))),
        _normalize_text(str(frame.get("symbol", ""))),
    )


def build_classified_evidence_rows_streaming(
    classified_path: Path,
    op_rows: list[dict[str, Any]],
    spec_mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    op_by_id = _op_rows_by_id(op_rows)
    external_rows: list[dict[str, Any]] = []
    replay_support_rows: list[dict[str, Any]] = []
    model_execute_spans: list[dict[str, Any]] = []
    replay_detection_errors: list[dict[str, Any]] = []
    for stream in iter_classified_streams(classified_path):
        for span in stream.get("spans", []):
            if str(span.get("span_name", "")).strip() == "MODEL_EXECUTE" and not bool(span.get("exclude_from_code_mapping", False)):
                model_execute_spans.append(span)
            repo_frames: list[dict[str, Any]] = []
            function_candidates: list[dict[str, Any]] = []
            code_line_candidates: list[dict[str, Any]] = []
            matched_rows = [op_by_id[op_row_id] for op_row_id in span.get("op_row_ids", []) if op_row_id in op_by_id]
            for matched in matched_rows:
                repo_frames.extend(matched.get("repo_frames", []))
                function_candidates.extend(matched.get("file_function_candidates", []))
                code_line_candidates.extend(matched.get("code_line_candidates", []))

            op_name_context = " ".join(
                sorted({str(row.get("op_name", "")).strip() for row in matched_rows if str(row.get("op_name", "")).strip()})
            )
            stream_role = str(span.get("stream_role", stream.get("stream_role", ""))).strip()
            semantic_class = str(span.get("semantic_class", "unknown")).strip() or "unknown"
            semantic_category = _semantic_category(semantic_class, stream_role)
            merged_function_candidates = _merge_file_function_candidates(
                function_candidates,
                op_name=op_name_context,
                semantic_class=semantic_class,
                stream_role=stream_role,
                span_name=str(span.get("span_name", "")).strip(),
            )
            primary_file_function = _select_primary_file_function(merged_function_candidates)
            merged_code_line_candidates = _merge_code_line_candidates(code_line_candidates)
            replay_anchor = _detect_replay_anchor_from_rows(matched_rows, spec_mode)
            replay_detection_error = str(replay_anchor.get("detection_error", "")).strip()
            if replay_detection_error:
                replay_detection_errors.append(
                    {
                        "span_id": str(span.get("span_id", "")).strip(),
                        "span_name": str(span.get("span_name", "")).strip(),
                        "parallel_group": str(span.get("parallel_group", "")).strip(),
                        "stream_id": str(span.get("stream_id", "")).strip(),
                        "matched_stack_rows": [row.get("op_row_id", "") for row in matched_rows],
                        "message": replay_detection_error,
                    }
                )
                continue
            is_graph_replay_candidate = bool(replay_anchor)
            if is_graph_replay_candidate:
                ensure(
                    all(
                        str(replay_anchor.get(key, "")).strip()
                        for key in ["matched_graph_kind", "candidate_phase", "phase_confidence", "phase_source"]
                    ),
                    f"Step4 replay anchor 识别结果缺少必要字段: span_id={span.get('span_id', '')}, replay_anchor={replay_anchor}",
                )

            if span.get("external_mapping_required", False) and not is_graph_replay_candidate:
                communication_impl_evidence_present = semantic_category == "communication" and any(
                    _frame_matches_semantic_impl(frame, "communication") for frame in repo_frames
                )
                compute_impl_evidence_present = semantic_category == "compute" and any(
                    _frame_matches_semantic_impl(frame, "compute") for frame in repo_frames
                )
                implementation_evidence_present = any(
                    _frame_matches_semantic_impl(frame, semantic_category) for frame in repo_frames
                ) if semantic_category in {"communication", "compute"} else any(
                    _frame_matches_semantic_impl(frame, "unknown") for frame in repo_frames
                )
                orchestration_frame_present = any(_frame_is_orchestration_heavy(frame) for frame in repo_frames)
                mapping_risk_flags: list[str] = []
                recommended_primary_location_kind = ""
                recommended_unresolved_reason = ""
                if semantic_category in {"communication", "compute"} and not implementation_evidence_present:
                    mapping_risk_flags.append("missing_impl_side_repo_frame")
                if semantic_category == "communication" and orchestration_frame_present and not communication_impl_evidence_present:
                    mapping_risk_flags.append("communication_only_orchestration_repo_frames")
                    recommended_primary_location_kind = "function_entry_fallback"
                    recommended_unresolved_reason = "communication_span_missing_impl_side_repo_frame"
                external_rows.append(
                    {
                        "span_id": span["span_id"],
                        "stream_id": span["stream_id"],
                        "parallel_group": span.get("parallel_group", ""),
                        "span_name": span["span_name"],
                        "semantic_class": semantic_class,
                        "stream_role": stream_role or semantic_class,
                        "scope_class": span.get("scope_class", ""),
                        "exclude_from_code_mapping": bool(span.get("exclude_from_code_mapping", False)),
                        "exclude_reason": str(span.get("exclude_reason", "")).strip(),
                        "external_mapping_required": bool(span.get("external_mapping_required", False)),
                        "has_stream_id": bool(span.get("has_stream_id", False)),
                        "target_gate_reason": "step3_external_mapping_required_and_not_graph_replay_candidate",
                        "stream_id_source": "classified_span_stream_id",
                        "task_ids": span.get("task_ids", []),
                        "op_row_ids": span.get("op_row_ids", []),
                        "matched_stack_rows": [row.get("op_row_id", "") for row in matched_rows],
                        "repo_frames": repo_frames,
                        "file_function_candidates": merged_function_candidates[:20],
                        "primary_file_function": primary_file_function,
                        "code_line_candidates": merged_code_line_candidates[:20],
                        "implementation_evidence_present": implementation_evidence_present,
                        "communication_impl_evidence_present": communication_impl_evidence_present,
                        "compute_impl_evidence_present": compute_impl_evidence_present,
                        "mapping_risk_flags": mapping_risk_flags,
                        "recommended_primary_location_kind": recommended_primary_location_kind,
                        "recommended_unresolved_reason": recommended_unresolved_reason,
                        "has_replay_anchor": any(bool(row.get("has_replay_anchor")) for row in matched_rows),
                        "has_spec_v2_anchor": any(bool(row.get("has_spec_v2_anchor")) for row in matched_rows),
                        "is_device_tensor_related": _device_tensor_related(
                            str(span.get("span_name", "")),
                            [str(row.get("op_name", "")) for row in matched_rows],
                        ),
                        "selection_notes": [
                            "Step4 直接保留 repo 调用栈文件:函数候选，不再输出 family/control/execution/device 锚点。",
                            "最终代码行应由 stack_mapper 结合 span 语义、左右 span 和并行结构，从 code_line_candidates 中正式选定。",
                            "若 communication span 缺少实现层 repo frame，应优先降级到 function_entry_fallback 或 unresolved，而不是把调度层函数包装成高质量精确定位。",
                        ],
                    }
                )

            if not is_graph_replay_candidate:
                continue
            replay_support_rows.append(
                {
                    "span_id": span["span_id"],
                    "parallel_group": span.get("parallel_group", ""),
                    "stream_id": span["stream_id"],
                    "start_ns": int(span.get("start_ns", 0) or 0),
                    "end_ns": int(span.get("end_ns", 0) or 0),
                    "span_name": span["span_name"],
                    "task_ids": span.get("task_ids", []),
                    "op_row_ids": span.get("op_row_ids", []),
                    "matched_stack_rows": [row.get("op_row_id", "") for row in matched_rows],
                    "file_function_candidates": merged_function_candidates[:20],
                    "primary_file_function": primary_file_function,
                    "matched_graph_kind": str(replay_anchor.get("matched_graph_kind", "")).strip(),
                    "candidate_phase": str(replay_anchor.get("candidate_phase", "")).strip(),
                    "phase_confidence": str(replay_anchor.get("phase_confidence", "")).strip(),
                    "phase_source": str(replay_anchor.get("phase_source", "")).strip(),
                    "replay_anchor_file": str(replay_anchor.get("replay_anchor_file", "")).strip(),
                    "replay_anchor_symbol": str(replay_anchor.get("replay_anchor_symbol", "")).strip(),
                    "evidence_sources": [
                        source
                        for source, enabled in [
                            ("operator_details.csv", bool(matched_rows)),
                            ("call_stack_replay_anchor", any(bool(row.get("has_replay_anchor")) for row in matched_rows)),
                            ("call_stack_spec_v2_anchor", any(bool(row.get("has_spec_v2_anchor")) for row in matched_rows)),
                        ]
                        if enabled
                    ],
                    "matched_frames": [
                        {
                            "repo_relative_path": frame.get("repo_relative_path", ""),
                            "line": frame.get("line", 0),
                            "symbol": frame.get("symbol", ""),
                        }
                        for row in matched_rows
                        for frame in row.get("repo_frames", [])
                    ][:20],
                    "reason": f"phase inferred from replay anchor evidence for span {span['span_id']}",
                }
            )
    phase_marker_rows, marker_detection_errors = _build_phase_marker_rows(
        model_execute_spans,
        replay_support_rows,
        spec_mode,
    )
    replay_detection_errors.extend(marker_detection_errors)
    return external_rows, phase_marker_rows, replay_detection_errors


def _slim_file_function(candidate: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return {}
    return {
        "file_function": str(candidate.get("file_function", "")).strip(),
        "repo_relative_path": str(candidate.get("repo_relative_path", "")).strip(),
        "symbol": str(candidate.get("symbol", "")).strip(),
        "entry_line": int(candidate.get("entry_line", 0) or 0),
        "line_candidates": candidate.get("line_candidates", [])[:10],
        "best_frame_index": int(candidate.get("best_frame_index", -1) if candidate.get("best_frame_index") is not None else -1),
        "score": int(candidate.get("score", 0) or 0),
        "score_breakdown": dict(candidate.get("score_breakdown", {}) or {}),
        "implementation_hint_matched": bool(candidate.get("implementation_hint_matched", False)),
        "orchestration_path_penalty_applied": bool(candidate.get("orchestration_path_penalty_applied", False)),
        "confidence": str(candidate.get("confidence", "")).strip(),
        "evidence_sources": candidate.get("evidence_sources", [])[:5],
    }


def _slim_line_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return {}
    return {
        "code_location": str(candidate.get("code_location", "")).strip(),
        "repo_relative_path": str(candidate.get("repo_relative_path", "")).strip(),
        "line": int(candidate.get("line", 0) or 0),
        "symbol": str(candidate.get("symbol", "")).strip(),
        "basis": candidate.get("basis", [])[:5],
        "confidence": str(candidate.get("confidence", "")).strip(),
    }


def slim_stack_evidence_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "op_row_id": str(row.get("op_row_id", "")).strip(),
        "op_name": str(row.get("op_name", "")).strip(),
        "raw_call_stack": str(row.get("raw_call_stack", "")).strip(),
        "repo_frames": row.get("repo_frames", [])[:20],
        "file_function_candidates": [_slim_file_function(item) for item in row.get("file_function_candidates", [])[:10]],
        "primary_file_function": _slim_file_function(row.get("primary_file_function") or {}),
        "code_line_candidates": [_slim_line_candidate(item) for item in row.get("code_line_candidates", [])[:10]],
        "has_spec_v2_anchor": bool(row.get("has_spec_v2_anchor", False)),
        "has_replay_anchor": bool(row.get("has_replay_anchor", False)),
    }


def slim_external_span_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "span_id": str(row.get("span_id", "")).strip(),
        "stream_id": str(row.get("stream_id", "")).strip(),
        "parallel_group": str(row.get("parallel_group", "")).strip(),
        "span_name": str(row.get("span_name", "")).strip(),
        "semantic_class": str(row.get("semantic_class", "unknown")).strip(),
        "stream_role": str(row.get("stream_role", "")).strip(),
        "scope_class": str(row.get("scope_class", "")).strip(),
        "exclude_from_code_mapping": bool(row.get("exclude_from_code_mapping", False)),
        "external_mapping_required": bool(row.get("external_mapping_required", False)),
        "has_stream_id": bool(row.get("has_stream_id", False)),
        "target_gate_reason": str(row.get("target_gate_reason", "")).strip(),
        "stream_id_source": str(row.get("stream_id_source", "")).strip(),
        "task_ids": row.get("task_ids", []),
        "op_row_ids": row.get("op_row_ids", []),
        "matched_stack_rows": row.get("matched_stack_rows", []),
        "file_function_candidates": [_slim_file_function(item) for item in row.get("file_function_candidates", [])[:10]],
        "primary_file_function": _slim_file_function(row.get("primary_file_function") or {}),
        "code_line_candidates": [_slim_line_candidate(item) for item in row.get("code_line_candidates", [])[:10]],
        "implementation_evidence_present": bool(row.get("implementation_evidence_present", False)),
        "communication_impl_evidence_present": bool(row.get("communication_impl_evidence_present", False)),
        "compute_impl_evidence_present": bool(row.get("compute_impl_evidence_present", False)),
        "mapping_risk_flags": row.get("mapping_risk_flags", [])[:5],
        "recommended_primary_location_kind": str(row.get("recommended_primary_location_kind", "")).strip(),
        "recommended_unresolved_reason": str(row.get("recommended_unresolved_reason", "")).strip(),
        "has_replay_anchor": bool(row.get("has_replay_anchor", False)),
        "has_spec_v2_anchor": bool(row.get("has_spec_v2_anchor", False)),
        "is_device_tensor_related": bool(row.get("is_device_tensor_related", False)),
        "selection_notes": row.get("selection_notes", [])[:5],
    }


def slim_graph_replay_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "span_id": str(row.get("span_id", "")).strip(),
        "parallel_group": str(row.get("parallel_group", "")).strip(),
        "stream_id": str(row.get("stream_id", "")).strip(),
        "start_ns": int(row.get("start_ns", 0) or 0),
        "end_ns": int(row.get("end_ns", 0) or 0),
        "span_name": str(row.get("span_name", "")).strip(),
        "task_ids": row.get("task_ids", []),
        "op_row_ids": row.get("op_row_ids", []),
        "matched_stack_rows": row.get("matched_stack_rows", []),
        "file_function_candidates": [_slim_file_function(item) for item in row.get("file_function_candidates", [])[:10]],
        "primary_file_function": _slim_file_function(row.get("primary_file_function") or {}),
        "matched_graph_kind": str(row.get("matched_graph_kind", "")).strip(),
        "candidate_phase": str(row.get("candidate_phase", "")).strip(),
        "phase_confidence": str(row.get("phase_confidence", "")).strip(),
        "phase_source": str(row.get("phase_source", "")).strip(),
        "replay_anchor_file": str(row.get("replay_anchor_file", "")).strip(),
        "replay_anchor_symbol": str(row.get("replay_anchor_symbol", "")).strip(),
        "evidence_sources": row.get("evidence_sources", [])[:10],
        "matched_frames": row.get("matched_frames", [])[:20],
        "reason": str(row.get("reason", "")).strip(),
    }


def build_stack_evidence_lite_output(output: dict[str, Any]) -> dict[str, Any]:
    rows = [slim_stack_evidence_row(row) for row in output.get("rows", []) if isinstance(row, dict)]
    external_span_rows = [slim_external_span_row(row) for row in output.get("external_span_rows", []) if isinstance(row, dict)]
    graph_replay_rows = [slim_graph_replay_row(row) for row in output.get("graph_replay_rows", []) if isinstance(row, dict)]
    has_spec_v2_anchor_row_count = sum(1 for row in rows if bool(row.get("has_spec_v2_anchor", False)))
    has_replay_anchor_row_count = sum(1 for row in rows if bool(row.get("has_replay_anchor", False)))
    return {
        "schema_version": str(output.get("schema_version", "stack_evidence_v2")),
        "repo_root": str(output.get("repo_root", "")),
        "source_path": str(output.get("source_path", "")),
        "lite": True,
        "rows": rows,
        "op_rows": rows,
        "external_span_rows": external_span_rows,
        "graph_phase_marker_rows": graph_replay_rows,
        "graph_replay_rows": graph_replay_rows,
        "summary": {
            "op_stack_row_count": len(rows),
            "external_span_row_count": len(external_span_rows),
            "graph_phase_marker_row_count": len(graph_replay_rows),
            "graph_replay_row_count": len(graph_replay_rows),
            "has_spec_v2_anchor_row_count": has_spec_v2_anchor_row_count,
            "has_replay_anchor_row_count": has_replay_anchor_row_count,
            "has_spec_v2_anchor_present": has_spec_v2_anchor_row_count > 0,
            "has_replay_anchor_present": has_replay_anchor_row_count > 0,
        },
    }


def build_stack_evidence_for_workspace(workspace_dir: Path) -> dict[str, Any]:
    state = load_state(workspace_dir)
    runtime_constraints = load_runtime_constraints(workspace_dir)
    spec_mode = str(runtime_constraints["spec_mode"]).strip()
    repo_root = Path(state["inputs"]["code_repo_path"])
    source_path = Path(state["artifacts"]["operator_slice_path"])
    classified_path = Path(state["artifacts"]["classified_spans_path"])
    rows = read_operator_rows(source_path)
    payload_rows = build_payload_rows(rows, repo_root)
    external_span_rows, graph_replay_rows, replay_detection_errors = build_classified_evidence_rows_streaming(
        classified_path,
        payload_rows,
        spec_mode,
    )
    ensure(
        not replay_detection_errors,
        "Step4 检测到未命中已支持 replay runner file 的 replay span，已拒绝继续静默分类："
        f"{replay_detection_errors[:10]}",
    )

    output = {
        "schema_version": "stack_evidence_v2",
        "repo_root": str(repo_root),
        "source_path": str(source_path),
        "rows": payload_rows,
        "op_rows": payload_rows,
        "external_span_rows": external_span_rows,
        "graph_phase_marker_rows": graph_replay_rows,
        "graph_replay_rows": graph_replay_rows,
        "summary": {
            "op_stack_row_count": len(payload_rows),
            "external_span_row_count": len(external_span_rows),
            "graph_phase_marker_row_count": len(graph_replay_rows),
            "graph_replay_row_count": len(graph_replay_rows),
        },
    }
    output_path = workspace_dir / "artifacts" / "mapping" / "stack_evidence.json"
    dump_json(output_path, output)
    lite_output_path = workspace_dir / "artifacts" / "mapping" / "stack_evidence_lite.json"
    dump_json(lite_output_path, build_stack_evidence_lite_output(output))
    state["artifacts"]["stack_evidence_path"] = str(output_path)
    state["artifacts"]["stack_evidence_lite_path"] = str(lite_output_path)
    state["flags"]["stack_evidence_built"] = True
    save_state(workspace_dir, state)
    return output


def main() -> int:
    args = build_parser().parse_args()
    build_stack_evidence_for_workspace(Path(args.workspace_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
