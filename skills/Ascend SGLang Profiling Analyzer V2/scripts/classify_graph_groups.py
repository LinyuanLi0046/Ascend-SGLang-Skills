from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from build_runtime_constraints import build_runtime_constraints_for_workspace, load_model_configs, parse_launch_fields
from check_repo_divergence import check_repo_divergence_for_workspace
from workflow_common import dump_json, load_json, load_state, save_state

PRIMARY_NOTIFY_WAIT_TYPES = {"NOTIFY_WAIT", "Notify_Wait"}
FALLBACK_NOTIFY_WAIT_TYPES = {"NOTIFY_WAIT_SQE"}


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="识别 graph groups / phases 并生成 graph_execution_plan.json。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def read_launch_text(state: dict[str, Any]) -> str:
    input_resolution_path = str(state.get("artifacts", {}).get("input_resolution_path", "")).strip()
    if input_resolution_path:
        candidate = Path(input_resolution_path)
        if candidate.exists() and candidate.is_file():
            normalized_launch_text = str(
                load_json(candidate).get("launch", {}).get("normalized_launch_text", "")
            ).strip()
            if normalized_launch_text:
                return normalized_launch_text
    launch_file = str(state["inputs"].get("launch_command_file", "")).strip()
    launch_text = str(state["inputs"].get("launch_command_text", "")).strip()
    if launch_text:
        return launch_text
    if launch_file and Path(launch_file).exists():
        launch_text = Path(launch_file).read_text(encoding="utf-8")
    return launch_text


def parse_launch_context(launch_text: str) -> dict[str, Any]:
    normalized = launch_text.lower()
    return {
        "launch_text": launch_text,
        "has_nextn": "nextn" in normalized,
        "has_eagle": "eagle" in normalized,
        "has_spec_v2": "spec-v2" in normalized or "spec_v2" in normalized or "eagle" in normalized or "nextn" in normalized,
        "has_graph_enabled": "--disable-cuda-graph" not in normalized and "--disable-graph" not in normalized,
    }


def detect_mode(launch_context: dict[str, Any]) -> str:
    if launch_context["has_spec_v2"] and launch_context["has_graph_enabled"]:
        return "spec_v2"
    if launch_context["has_graph_enabled"]:
        return "decode_graph"
    return "unknown"


def collect_graph_candidate_spans(
    classified: dict[str, Any],
    phase_lookup: dict[str, dict[str, Any]],
    anchor_groups: set[str],
    mode: str,
) -> list[dict[str, Any]]:
    rows = []
    phase_span_ids = set(phase_lookup.keys())
    for stream in classified.get("streams", []):
        for span in stream.get("spans", []):
            if span.get("exclude_from_code_mapping", False):
                continue
            span_id = str(span.get("span_id", ""))
            semantic_class = str(span.get("semantic_class", "unknown"))
            parallel_group = str(span.get("parallel_group", "")).strip()
            if span_id in phase_span_ids:
                rows.append(span)
            elif parallel_group and parallel_group in anchor_groups and semantic_class in {"compute", "communication", "runtime_control"}:
                rows.append(span)
            elif mode == "decode_graph" and not anchor_groups and semantic_class in {"compute", "communication", "runtime_control"}:
                rows.append(span)
    rows.sort(key=lambda item: (item["start_ns"], item["end_ns"], item["stream_id"], item["span_id"]))
    return rows


def build_phase_lookup_from_stack_evidence(graph_phase_stack_evidence: dict[str, Any]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in graph_phase_stack_evidence.get("rows", []):
        span_id = str(row.get("span_id", "")).strip()
        candidate_phase = str(row.get("candidate_phase", "")).strip()
        phase_confidence = str(row.get("phase_confidence", "")).strip()
        if span_id and candidate_phase and candidate_phase != "unknown" and phase_confidence in {"high", "medium"}:
            lookup[span_id] = {
                "span_id": span_id,
                "phase": candidate_phase,
                "phase_confidence": phase_confidence,
                "matched_graph_kind": str(row.get("matched_graph_kind", "")).strip(),
                "phase_source": str(row.get("phase_source", "graph_phase_stack_evidence")).strip()
                or "graph_phase_stack_evidence",
                "parallel_group": str(row.get("parallel_group", "")).strip(),
                "replay_anchor_file": str(row.get("replay_anchor_file", "")).strip(),
                "stream_id": str(row.get("stream_id", "")).strip(),
                "start_ns": int(row.get("start_ns", 0) or 0),
                "end_ns": int(row.get("end_ns", 0) or 0),
            }
    return lookup


def build_anchor_groups(phase_lookup: dict[str, dict[str, Any]]) -> set[str]:
    return {
        str(item.get("parallel_group", "")).strip()
        for item in phase_lookup.values()
        if str(item.get("parallel_group", "")).strip()
    }


def build_time_segments(group_spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not group_spans:
        return []
    ordered = sorted(group_spans, key=lambda item: (int(item["start_ns"]), int(item["end_ns"]), str(item["span_id"])))
    positive_gaps = [
        max(0, int(curr["start_ns"]) - int(prev["end_ns"]))
        for prev, curr in zip(ordered, ordered[1:])
        if int(curr["start_ns"]) > int(prev["end_ns"])
    ]
    baseline_gap = sorted(positive_gaps)[len(positive_gaps) // 2] if positive_gaps else 0
    gap_threshold = max(20_000, baseline_gap * 5)
    segments: list[dict[str, Any]] = []
    current_segment = {
        "start_ns": int(ordered[0]["start_ns"]),
        "end_ns": int(ordered[0]["end_ns"]),
        "span_ids": [str(ordered[0].get("span_id", ""))],
    }
    for span in ordered[1:]:
        start_ns = int(span["start_ns"])
        end_ns = int(span["end_ns"])
        span_id = str(span.get("span_id", ""))
        gap = max(0, start_ns - int(current_segment["end_ns"]))
        if gap > gap_threshold:
            segments.append(current_segment)
            current_segment = {
                "start_ns": start_ns,
                "end_ns": end_ns,
                "span_ids": [span_id],
            }
            continue
        current_segment["end_ns"] = max(int(current_segment["end_ns"]), end_ns)
        current_segment["span_ids"].append(span_id)
    segments.append(current_segment)
    return segments


def collect_notify_wait_rows(timeline_index: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    trace_spans = timeline_index.get("trace_spans", [])
    ensure(isinstance(trace_spans, list), "timeline_index.trace_spans 缺失或结构非法，无法定位 NOTIFY_WAIT。")
    for span in trace_spans:
        task_type = str(span.get("name", "")).strip()
        if task_type not in PRIMARY_NOTIFY_WAIT_TYPES | FALLBACK_NOTIFY_WAIT_TYPES:
            continue
        start_ns = int(span.get("start_ns", 0) or 0)
        end_ns = int(span.get("end_ns", 0) or 0)
        if end_ns <= start_ns:
            continue
        candidate_stream_ids = [str(item).strip() for item in span.get("candidate_stream_ids", []) if str(item).strip()]
        stream_id = candidate_stream_ids[0] if candidate_stream_ids else str(span.get("stream_id", "")).strip()
        ensure(stream_id, f"NOTIFY_WAIT trace span 缺少可用 stream_id: span_id={span.get('span_id', '')}")
        rows.append(
            {
                "source": "trace_spans",
                "trace_span_id": str(span.get("span_id", "")).strip(),
                "trace_event_index": int(span.get("trace_event_index", 0) or 0),
                "task_compound_id": str(span.get("span_id", "")).strip(),
                "task_id": str(span.get("span_id", "")).strip(),
                "stream_id": stream_id,
                "task_type": task_type,
                "start_ns": start_ns,
                "end_ns": end_ns,
                "dur_ns": end_ns - start_ns,
            }
        )
    rows.sort(key=lambda item: (item["stream_id"], item["start_ns"], item["end_ns"], item["task_compound_id"]))
    ensure(rows, "timeline_index.trace_spans 中未找到可信 NOTIFY_WAIT/NOTIFY_WAIT_SQE，Step5 拒绝再静默 fallback。")
    return rows


def _notify_wait_type_rank(task_type: str) -> int:
    if task_type in PRIMARY_NOTIFY_WAIT_TYPES:
        return 0
    if task_type in FALLBACK_NOTIFY_WAIT_TYPES:
        return 1
    return 2


def select_notify_wait_task(
    anchor_start_ns: int,
    anchor_end_ns: int,
    notify_wait_tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    if anchor_start_ns <= 0:
        return {}
    effective_anchor_end_ns = anchor_end_ns if anchor_end_ns >= anchor_start_ns else anchor_start_ns
    covering_tasks = []
    following_tasks = []
    for task in notify_wait_tasks:
        task_start_ns = int(task.get("start_ns", 0) or 0)
        task_end_ns = int(task.get("end_ns", 0) or 0)
        if task_end_ns < anchor_start_ns:
            continue
        if task_start_ns <= effective_anchor_end_ns <= task_end_ns:
            covering_tasks.append(
                {
                    **task,
                    "anchor_gap_ns": 0,
                    "selection_rank_ns": task_start_ns,
                }
            )
            continue
        if task_start_ns >= effective_anchor_end_ns:
            following_tasks.append(
                {
                    **task,
                    "anchor_gap_ns": task_start_ns - effective_anchor_end_ns,
                    "selection_rank_ns": task_start_ns,
                }
            )
    if covering_tasks:
        selected = min(
            covering_tasks,
            key=lambda item: (
                _notify_wait_type_rank(str(item.get("task_type", ""))),
                int(item.get("selection_rank_ns", 0) or 0),
                int(item.get("end_ns", 0) or 0),
                str(item.get("task_compound_id", "")),
            ),
        )
        return {
            **selected,
            "boundary_source": "first_notify_wait_after_model_execute",
            "boundary_relation": "covers_model_execute_end",
            "boundary_confidence": "high" if str(selected.get("task_type", "")) in PRIMARY_NOTIFY_WAIT_TYPES else "medium",
        }
    if following_tasks:
        selected = min(
            following_tasks,
            key=lambda item: (
                _notify_wait_type_rank(str(item.get("task_type", ""))),
                int(item.get("selection_rank_ns", 0) or 0),
                int(item.get("end_ns", 0) or 0),
                str(item.get("task_compound_id", "")),
            ),
        )
        return {
            **selected,
            "boundary_source": "first_notify_wait_after_model_execute",
            "boundary_relation": "first_notify_wait_after_model_execute",
            "boundary_confidence": "high" if str(selected.get("task_type", "")) in PRIMARY_NOTIFY_WAIT_TYPES else "medium",
        }
    return {}


def filter_spans_in_window(group_spans: list[dict[str, Any]], start_ns: int, end_ns: int) -> list[dict[str, Any]]:
    if end_ns < start_ns:
        return []
    rows = [
        span
        for span in group_spans
        if int(span.get("end_ns", 0) or 0) >= start_ns and int(span.get("start_ns", 0) or 0) <= end_ns
    ]
    rows.sort(key=lambda item: (int(item["start_ns"]), int(item["end_ns"]), str(item.get("span_id", ""))))
    return rows


def build_phase_windows(
    mode: str,
    spans: list[dict[str, Any]],
    phase_lookup: dict[str, dict[str, Any]],
    notify_wait_tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not spans:
        return []
    anchor_rows = sorted(
        phase_lookup.values(),
        key=lambda item: (
            int(item.get("start_ns", 0) or 0),
            int(item.get("end_ns", 0) or 0),
            str(item.get("span_id", "")),
        ),
    )
    ensure(anchor_rows, "Step5 缺少高/中置信度 MODEL_EXECUTE phase marker 证据，拒绝继续生成 phase windows。")
    windows: list[dict[str, Any]] = []
    for anchor in anchor_rows:
        notify_wait_task = select_notify_wait_task(
            int(anchor.get("start_ns", 0) or 0),
            int(anchor.get("end_ns", 0) or 0),
            notify_wait_tasks,
        )
        ensure(
            bool(notify_wait_task),
            "Step5 未能在 MODEL_EXECUTE marker 结束后找到第一个合法 NOTIFY_WAIT task，已拒绝退回 group span end fallback："
            f"span_id={anchor.get('span_id', '')}, phase={anchor.get('phase', '')}, "
            f"anchor_start_ns={anchor.get('start_ns', 0)}, anchor_end_ns={anchor.get('end_ns', 0)}",
        )
        window_start_ns = int(anchor.get("start_ns", 0) or 0)
        window_end_ns = int(notify_wait_task.get("end_ns", 0) or 0)
        window_spans = filter_spans_in_window(spans, window_start_ns, window_end_ns)
        ensure(
            bool(window_spans),
            "Step5 根据 MODEL_EXECUTE marker + NOTIFY_WAIT 得到的 phase window 未覆盖任何 graph candidate span："
            f"span_id={anchor.get('span_id', '')}, phase={anchor.get('phase', '')}",
        )
        boundary_stream_id = str(notify_wait_task.get("stream_id", "")).strip()
        boundary_task_type = str(notify_wait_task.get("task_type", "")).strip()
        boundary_start_ns = int(notify_wait_task.get("start_ns", 0) or 0)
        boundary_end_ns = int(notify_wait_task.get("end_ns", 0) or 0)
        boundary_duration_ns = int(notify_wait_task.get("dur_ns", 0) or 0)
        boundary_task_count = 1
        boundary_gap_ns = int(notify_wait_task.get("anchor_gap_ns", 0) or 0)
        boundary_source = str(notify_wait_task.get("boundary_source", "first_notify_wait_after_model_execute")).strip()
        boundary_relation = str(notify_wait_task.get("boundary_relation", "first_notify_wait_after_model_execute")).strip()
        boundary_confidence = str(notify_wait_task.get("boundary_confidence", "medium")).strip()

        phase = "decode" if mode == "decode_graph" else str(anchor.get("phase", "")).strip()
        phase_confidence = str(anchor.get("phase_confidence", "")).strip() or ("high" if mode == "decode_graph" else "medium")
        phase_source = str(anchor.get("phase_source", "")).strip() or "graph_phase_stack_evidence"
        time_segments = build_time_segments(window_spans)
        merged = False
        for existing in windows:
            same_phase = str(existing.get("phase", "")) == phase
            same_boundary = (
                str(existing.get("boundary_stream_id", "")) == boundary_stream_id
                and int(existing.get("boundary_start_ns", 0) or 0) == boundary_start_ns
                and int(existing.get("boundary_end_ns", 0) or 0) == boundary_end_ns
            )
            if not (same_phase and same_boundary):
                continue
            existing_span_ids = set(str(item) for item in existing.get("span_ids", []))
            existing_span_ids.update(str(span.get("span_id", "")) for span in window_spans)
            existing["span_ids"] = sorted(existing_span_ids)
            existing["anchor_span_ids"] = sorted(
                set(str(item) for item in existing.get("anchor_span_ids", [])) | {str(anchor.get("span_id", "")).strip()}
            )
            existing["start_ns"] = min(int(existing.get("start_ns", 0) or 0), window_start_ns)
            existing["anchor_start_ns"] = min(int(existing.get("anchor_start_ns", 0) or 0), int(anchor.get("start_ns", 0) or 0))
            existing["anchor_end_ns"] = max(int(existing.get("anchor_end_ns", 0) or 0), int(anchor.get("end_ns", 0) or 0))
            existing["time_segments"] = build_time_segments(
                filter_spans_in_window(spans, int(existing["start_ns"]), int(existing["end_ns"]))
            )
            existing["stream_ids"] = sorted({str(span.get("stream_id", "")) for span in filter_spans_in_window(spans, int(existing["start_ns"]), int(existing["end_ns"]))})
            merged = True
            break
        if merged:
            continue
        windows.append(
            {
                "phase_window_id": "",
                "phase": phase,
                "phase_confidence": phase_confidence,
                "phase_source": phase_source,
                "parallel_group": str(anchor.get("parallel_group", "")).strip(),
                "span_ids": [str(span.get("span_id", "")) for span in window_spans],
                "time_segments": time_segments,
                "start_ns": window_start_ns,
                "end_ns": window_end_ns,
                "stream_ids": sorted({str(span.get("stream_id", "")) for span in window_spans}),
                "anchor_span_id": str(anchor.get("span_id", "")).strip(),
                "anchor_span_ids": [str(anchor.get("span_id", "")).strip()],
                "anchor_start_ns": int(anchor.get("start_ns", 0) or 0),
                "anchor_end_ns": int(anchor.get("end_ns", 0) or 0),
                "anchor_stream_id": str(anchor.get("stream_id", "")).strip(),
                "replay_anchor_file": str(anchor.get("replay_anchor_file", "")).strip(),
                "phase_marker_kind": str(anchor.get("marker_kind", "")).strip(),
                "boundary_source": boundary_source,
                "boundary_relation": boundary_relation,
                "boundary_confidence": boundary_confidence,
                "boundary_stream_id": boundary_stream_id,
                "boundary_task_type": boundary_task_type,
                "boundary_start_ns": boundary_start_ns,
                "boundary_end_ns": boundary_end_ns,
                "boundary_duration_ns": boundary_duration_ns,
                "boundary_task_count": boundary_task_count,
                "boundary_gap_ns": boundary_gap_ns,
                "expected_stream_roles": ["compute", "communication"],
            }
        )
    windows.sort(key=lambda item: (int(item.get("start_ns", 0) or 0), int(item.get("end_ns", 0) or 0), str(item.get("phase", ""))))
    for index, window in enumerate(windows, start=1):
        window["phase_window_id"] = f"phase_window_{index:04d}"
    return windows


def build_graph_groups(
    mode: str,
    candidate_spans: list[dict[str, Any]],
    phase_windows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    if mode == "spec_v2":
        for phase_order, phase in enumerate(["verify", "draft_prefill", "draft_decode"], start=1):
            matching_windows = [window for window in phase_windows if str(window.get("phase", "")) == phase]
            if not matching_windows:
                continue
            span_ids = [span_id for window in matching_windows for span_id in window.get("span_ids", [])]
            groups.append(
                {
                    "graph_group_id": f"{phase}_graph_0001",
                    "graph_mode": "speculative",
                    "speculative_mode": "spec_v2",
                    "phase": phase,
                    "phase_order": phase_order,
                    "span_ids": span_ids,
                    "phase_window_ids": [str(window.get("phase_window_id", "")) for window in matching_windows],
                    "time_ranges": [
                        {
                            "start_ns": int(window.get("start_ns", 0) or 0),
                            "end_ns": int(window.get("end_ns", 0) or 0),
                        }
                        for window in matching_windows
                    ],
                    "expected_stream_roles": ["compute"],
                    "reason": f"classified as speculative {phase} graph from launch/config and Step4 MODEL_EXECUTE phase markers",
                }
            )
        return groups

    if mode == "decode_graph":
        if not phase_windows:
            return []
        groups.append(
            {
                "graph_group_id": "decode_graph_0001",
                "graph_mode": "decode_only",
                "speculative_mode": "disabled",
                "phase": "decode",
                "phase_order": 1,
                "span_ids": [span["span_id"] for span in candidate_spans],
                "phase_window_ids": [str(window.get("phase_window_id", "")) for window in phase_windows],
                "time_ranges": [
                    {
                        "start_ns": int(window.get("start_ns", 0) or 0),
                        "end_ns": int(window.get("end_ns", 0) or 0),
                    }
                    for window in phase_windows
                ],
                "expected_stream_roles": ["compute"],
                "reason": "graph spans grouped as decode-only graph because launch/config do not indicate speculative spec-v2; phase windows prefer MODEL_EXECUTE markers + NOTIFY_WAIT boundaries when available.",
            }
        )
        return groups

    return []


def build_graph_execution_plan_for_workspace(workspace_dir: Path) -> dict[str, Any]:
    check_repo_divergence_for_workspace(workspace_dir)
    runtime_constraints = build_runtime_constraints_for_workspace(workspace_dir)
    state = load_state(workspace_dir)
    model_root = Path(state["inputs"]["model_root_path"])
    classified = load_json(Path(state["artifacts"]["classified_spans_path"]))
    stack_evidence = load_json(Path(state["artifacts"]["stack_evidence_path"]))
    graph_phase_stack_evidence = load_json(Path(state["artifacts"]["graph_phase_stack_evidence_path"]))
    timeline_index = load_json(Path(state["artifacts"]["timeline_index_path"]))

    launch_context = parse_launch_context(read_launch_text(state))
    launch_context["parsed_launch_fields"] = runtime_constraints.get("parsed_launch_fields", {})
    launch_context["model_path"] = runtime_constraints.get("model_path", "")
    launch_context["draft_model_path"] = runtime_constraints.get("draft_model_path", "")
    launch_context["speculative_algorithm"] = runtime_constraints.get("speculative_algorithm", "")
    launch_context["normalized_speculative_algorithm"] = runtime_constraints.get("normalized_speculative_algorithm", "")
    launch_context["phase_candidates"] = runtime_constraints.get("phase_candidates", [])
    model_context = dict(runtime_constraints.get("primary_model_context", {}) or load_model_configs(model_root))
    draft_model_path = str(runtime_constraints.get("draft_model_path", "")).strip()
    model_context["draft_model_context"] = dict(runtime_constraints.get("draft_model_context", {}))
    if draft_model_path:
        model_context["draft_model_root"] = draft_model_path
    model_context["parsed_launch_fields"] = runtime_constraints.get("parsed_launch_fields", {})
    mode = detect_mode(launch_context)
    phase_lookup = build_phase_lookup_from_stack_evidence(graph_phase_stack_evidence)
    anchor_groups = build_anchor_groups(phase_lookup)

    dump_json(workspace_dir / "input" / "launch_command.json", launch_context)
    dump_json(workspace_dir / "input" / "model_context.json", model_context)

    graph_candidate_spans = collect_graph_candidate_spans(classified, phase_lookup, anchor_groups, mode)
    notify_wait_rows = collect_notify_wait_rows(timeline_index)
    phase_windows = build_phase_windows(mode, graph_candidate_spans, phase_lookup, notify_wait_rows)
    graph_groups = build_graph_groups(mode, graph_candidate_spans, phase_windows)
    identified_graph_span_ids = []
    for group in graph_groups:
        identified_graph_span_ids.extend(str(item) for item in group.get("span_ids", []))

    warnings = []
    preconditions = runtime_constraints.get("step5_preconditions", {})
    if not preconditions.get("ready", False):
        warnings.extend(str(item) for item in preconditions.get("blockers", []))
    if not any(row.get("has_spec_v2_anchor") for row in stack_evidence.get("rows", [])):
        warnings.append("stack_evidence 中未发现明确 spec-v2 anchor，当前 graph inventory 主要依赖 MODEL_EXECUTE markers、launch/context 与已分类 span。")
    if mode != "spec_v2":
        warnings.append("当前 launch/context 未能强证实 spec-v2，graph inventory 以更保守的 graph mode / phase 分类输出。")
    if not graph_candidate_spans:
        warnings.append("Step4 graph_phase_stack_evidence 中未识别到可用 MODEL_EXECUTE phase markers，Step5 不应继续尝试 graph 内路径重建。")
    missing_expected_phases = []
    if mode == "spec_v2":
        actual_phases = {str(window.get("phase", "")).strip() for window in phase_windows}
        missing_expected_phases = [phase for phase in ["verify", "draft_prefill", "draft_decode"] if phase not in actual_phases]
        if missing_expected_phases:
            warnings.append(f"spec_v2 当前缺少以下真实 phase windows: {missing_expected_phases}")
    notify_wait_bounded_windows = phase_windows

    output = {
        "schema_version": "graph_execution_plan_v2",
        "status": "blocked" if not preconditions.get("ready", False) or not graph_candidate_spans else "partial",
        "summary": "graph groups and phases reconstructed from launch context, model config, MODEL_EXECUTE phase markers, and NOTIFY_WAIT device boundaries.",
        "errors": list(preconditions.get("blockers", [])),
        "warnings": warnings,
        "mode": mode,
        "graph_mode": "speculative" if mode == "spec_v2" else ("decode_only" if mode == "decode_graph" else "unknown"),
        "mapping_granularity": "phase_inventory_only",
        "mapping_limitations": [
            "当前文件只负责 graph inventory 和 phase 分类，不负责 graph 内真实代码路径重建。",
            "graph 内每个硬件 span 到模型 forward / operator 代码行的精确映射必须由 graph_path_analyst 完成。",
        ],
        "precondition_status": {
            "ready": bool(preconditions.get("ready", False)) and bool(graph_candidate_spans) and mode in {"spec_v2", "decode_graph"},
            "runtime_constraints_ready": bool(preconditions.get("ready", False)),
            "graph_candidate_span_count": len(graph_candidate_spans),
            "notify_wait_row_count": len(notify_wait_rows),
            "notify_wait_task_count": len(notify_wait_rows),
            "notify_wait_bounded_window_count": len(notify_wait_bounded_windows),
            "missing_expected_phases": missing_expected_phases,
            "blockers": list(preconditions.get("blockers", [])),
        },
        "graph_groups": graph_groups,
        "phase_plan": graph_groups,
        "phase_windows": phase_windows,
        "identified_graph_span_ids": identified_graph_span_ids,
        "layer_patterns": [
            {
                "pattern_id": "layer_0001",
                "description": "P0 当前仅输出 graph inventory / phase skeleton，不在本脚本中展开 forward 细节。",
            }
        ],
        "mapping_hints": [],
    }
    output_path = workspace_dir / "artifacts" / "graph" / "graph_execution_plan.json"
    dump_json(output_path, output)
    state["artifacts"]["graph_execution_plan_path"] = str(output_path)
    state["flags"]["graph_path_built"] = True
    save_state(workspace_dir, state)
    return output


def main() -> int:
    args = build_parser().parse_args()
    build_graph_execution_plan_for_workspace(Path(args.workspace_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
