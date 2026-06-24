from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import Any

from agent_contracts import AGENT_CONFIG, effective_agent_config, resolve_workspace_paths
from merge_timeline_review_patch import merge_timeline_review_patch_for_workspace
from normalize_graph_review_result import normalize_graph_review_result_file
from workflow_common import (
    child_run_logs_dir,
    collect_existing_file_hashes,
    extract_graph_alignment_rows,
    graph_source_line_violation,
    dump_json,
    list_workspace_temp_scripts,
    load_json,
    load_provenance_manifest,
    load_state,
    now_iso,
    provenance_manifest_path,
    record_agent_status,
    read_repo_source_line,
    save_provenance_manifest,
    save_state,
    write_error_context,
    write_finalize_audit_record,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校验子 agent 输出并回写状态。")
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument("--agent-name", required=True, choices=sorted(AGENT_CONFIG))
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


ZERO_LINE_CODE_LOCATION_RE = re.compile(r"^.+:0$")
CODE_LOCATION_RE = re.compile(r"^(?P<path>.+):(?P<line>\d+)$")
SELF_CALL_RE = re.compile(r"\bself\.\w+\s*\(")
CONSTRUCTOR_LINE_RE = re.compile(r"^\s*self\.\w+\s*=\s*[A-Za-z_][\w\.]*\(")
ALLOWED_OPERATOR_EVIDENCE_KINDS = {
    "torch_call",
    "torch_functional_call",
    "torch_npu_call",
    "npu_custom_op",
    "triton_call",
    "tensor_expression",
    "collective_call",
    "device_cache_op",
}
ALLOWED_GRAPH_LOCATION_KINDS = {
    "operator_call",
    "module_call_anchor",
    "graph_replay_entry",
    "constructor_line",
}
STACK_MAPPER_ORCHESTRATION_PATH_KEYWORDS = (
    "speculative/",
    "scheduler",
    "schedule_batch",
    "worker",
    "prefill_delayer",
    "event_loop",
)
STACK_MAPPER_COLLAPSE_RATIO_THRESHOLD = 0.05
STACK_MAPPER_COLLAPSE_UNIQUE_SPAN_NAME_THRESHOLD = 8


def _dispatch_payload(workspace_dir: Path, state: dict[str, Any], agent_name: str) -> dict[str, Any]:
    dispatch_path = str(state.get("agents", {}).get(agent_name, {}).get("dispatch_ready_path", "")).strip()
    ensure(dispatch_path, f"{agent_name} 缺少 dispatch_ready_path。")
    path = Path(dispatch_path)
    ensure(path.exists(), f"{agent_name} dispatch_ready_path 不存在: {path}")
    return load_json(path)


def ensure_no_new_temp_scripts(workspace_dir: Path, state: dict[str, Any], agent_name: str) -> None:
    dispatch_payload = _dispatch_payload(workspace_dir, state, agent_name)
    baseline = {str(item) for item in dispatch_payload.get("workspace_temp_script_baseline", [])}
    current = set(list_workspace_temp_scripts(workspace_dir))
    unexpected = sorted(current - baseline)
    ensure(
        not unexpected,
        f"{agent_name} 调度后检测到新增临时脚本，禁止 finalize: {unexpected[:10]}",
    )


def ensure_dispatch_completion_marker(workspace_dir: Path, state: dict[str, Any], agent_name: str) -> dict[str, Any]:
    dispatch_payload = _dispatch_payload(workspace_dir, state, agent_name)
    marker_path_raw = str(dispatch_payload.get("completion_marker_path", "")).strip()
    ensure(marker_path_raw, f"{agent_name} dispatch 缺少 completion_marker_path，需重新 prepare。")
    marker_path = Path(marker_path_raw)
    ensure(
        marker_path.exists(),
        f"{agent_name} 缺少子 agent completion marker: {marker_path}。"
        "必须在真实 Task(...) 返回后先运行 scripts/record_subagent_completion.py，再进入 finalize。",
    )
    marker_payload = load_json(marker_path)
    ensure(
        str(marker_payload.get("agent_name", "")).strip() == agent_name,
        f"{agent_name} completion marker agent_name 不匹配: {marker_payload.get('agent_name')!r}",
    )
    dispatch_id = str(dispatch_payload.get("dispatch_id", "")).strip()
    ensure(dispatch_id, f"{agent_name} dispatch 缺少 dispatch_id，需重新 prepare。")
    ensure(
        str(marker_payload.get("dispatch_id", "")).strip() == dispatch_id,
        f"{agent_name} completion marker dispatch_id 不匹配，可能仍是旧 dispatch 的遗留标记。",
    )
    ensure(
        int(marker_payload.get("step", 0) or 0) == int(dispatch_payload.get("step", 0) or 0),
        f"{agent_name} completion marker step 与当前 dispatch 不一致。",
    )
    ensure(
        str(marker_payload.get("query_snapshot_path", "")).strip() == str(dispatch_payload.get("query_snapshot_path", "")).strip(),
        f"{agent_name} completion marker query_snapshot_path 与当前 dispatch 不一致。",
    )
    dispatch_query_snapshot_sha256 = str(dispatch_payload.get("query_snapshot_sha256", "")).strip()
    marker_query_snapshot_sha256 = str(marker_payload.get("query_snapshot_sha256", "")).strip()
    if dispatch_query_snapshot_sha256:
        ensure(marker_query_snapshot_sha256, f"{agent_name} completion marker 缺少 query_snapshot_sha256。")
        ensure(
            marker_query_snapshot_sha256 == dispatch_query_snapshot_sha256,
            f"{agent_name} completion marker query_snapshot_sha256 与当前 dispatch 不一致。",
        )
    ensure(
        str(marker_payload.get("completion_source", "")).strip() in {"task_subagent", "task_resume"},
        f"{agent_name} completion marker completion_source 非法: {marker_payload.get('completion_source')!r}",
    )
    dispatch_main_agent_role = str(dispatch_payload.get("main_agent_role", "")).strip()
    if dispatch_main_agent_role:
        ensure(
            str(marker_payload.get("main_agent_role", "")).strip() == dispatch_main_agent_role,
            f"{agent_name} completion marker main_agent_role 与当前 dispatch 不一致。",
        )
    dispatch_subagent_role = str(dispatch_payload.get("subagent_role", "")).strip()
    if dispatch_subagent_role:
        ensure(
            str(marker_payload.get("subagent_role", "")).strip() == dispatch_subagent_role,
            f"{agent_name} completion marker subagent_role 与当前 dispatch 不一致。",
        )
    if bool(dispatch_payload.get("task_required", False)):
        ensure(
            bool(marker_payload.get("task_required", False)),
            f"{agent_name} completion marker 未声明 task_required=true。",
        )
    if bool(dispatch_payload.get("task_receipt_required", False)):
        task_call_id = str(marker_payload.get("task_call_id", "")).strip()
        subagent_id = str(marker_payload.get("subagent_id", "")).strip()
        ensure(task_call_id or subagent_id, f"{agent_name} completion marker 缺少 task_call_id/subagent_id。")
    dispatch_allowed_scripts = [
        str(item).strip()
        for item in dispatch_payload.get("allowed_official_scripts", [])
        if str(item).strip()
    ]
    marker_allowed_scripts = [
        str(item).strip()
        for item in marker_payload.get("allowed_official_scripts", [])
        if str(item).strip()
    ]
    ensure(
        marker_allowed_scripts == dispatch_allowed_scripts,
        f"{agent_name} completion marker allowed_official_scripts 与当前 dispatch 不一致。",
    )
    ensure(str(marker_payload.get("completed_at", "")).strip(), f"{agent_name} completion marker 缺少 completed_at。")
    return marker_payload


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wrapper_lock_path(workspace_dir: Path, agent_name: str, step: int) -> tuple[Path, str] | None:
    logs_dir = child_run_logs_dir(workspace_dir)
    if agent_name == "profiling_preprocessor" and step == 1:
        return logs_dir / "step1_wrapper.lock.json", "Step 1"
    if agent_name == "profiling_preprocessor" and step == 2:
        return logs_dir / "step2_wrapper.lock.json", "Step 2"
    if agent_name == "timeline_analyst" and step == 3:
        return logs_dir / "step3_wrapper.lock.json", "Step 3"
    if agent_name == "step4_bootstrap_runner" and step == 4:
        return logs_dir / "step4_bootstrap.lock.json", "Step 4A bootstrap"
    if agent_name == "graph_bootstrap_runner" and step == 5:
        return logs_dir / "step5_graph_bootstrap.lock.json", "Step 5A graph bootstrap"
    if agent_name == "artifact_renderer" and step == 6:
        return logs_dir / "step6_wrapper.lock.json", "Step 6"
    return None


def validate_wrapper_terminal_state(
    workspace_dir: Path,
    agent_name: str,
    step: int,
    completion_payload: dict[str, Any],
) -> None:
    spec = _wrapper_lock_path(workspace_dir, agent_name, step)
    if spec is None:
        return
    lock_path, step_label = spec
    ensure(lock_path.exists(), f"{agent_name} finalize 前缺少 {step_label} wrapper lock: {lock_path}")
    lock_payload = load_json(lock_path)
    lock_status = str(lock_payload.get("status", "")).strip()
    lock_pid = int(lock_payload.get("pid", 0) or 0)
    lock_pid_alive = _process_is_alive(lock_pid)
    ensure(
        lock_status in {"passed", "failed"},
        f"{agent_name} finalize 前 {step_label} wrapper lock 必须已收口为 passed/failed。"
        f" 当前 status={lock_status!r}, pid={lock_pid}, pid_alive={lock_pid_alive}, lock={lock_path}",
    )
    ensure(
        str(completion_payload.get("wrapper_lock_path", "")).strip() == str(lock_path),
        f"{agent_name} completion marker 缺少或使用了错误的 {step_label} wrapper lock 路径。",
    )
    ensure(
        str(completion_payload.get("wrapper_lock_status", "")).strip() == lock_status,
        f"{agent_name} completion marker 中记录的 wrapper lock 状态与当前 {step_label} wrapper lock 不一致。",
    )
    ensure(
        int(completion_payload.get("wrapper_lock_pid", 0) or 0) == lock_pid,
        f"{agent_name} completion marker 中记录的 wrapper pid 与当前 {step_label} wrapper lock 不一致。",
    )
    ensure(
        bool(completion_payload.get("wrapper_lock_pid_alive", False)) == lock_pid_alive,
        f"{agent_name} completion marker 中记录的 wrapper pid_alive 与当前 {step_label} wrapper lock 不一致。",
    )
    ensure(
        str(completion_payload.get("wrapper_lock_ended_at", "")).strip() == str(lock_payload.get("ended_at", "")).strip(),
        f"{agent_name} completion marker 中记录的 wrapper ended_at 与当前 {step_label} wrapper lock 不一致。",
    )


def finalized_artifact_paths(
    workspace_dir: Path,
    state: dict[str, Any],
    agent_name: str,
    output_files: list[Path],
) -> dict[str, Path]:
    artifacts = state.get("artifacts", {})
    step = int(state.get("current_step", 0))
    paths = {f"output:{path.name}": path for path in output_files if path.exists()}
    key_map: dict[tuple[str, int], list[str]] = {
        ("profiling_preprocessor", 1): [
            "trace_slice_path",
            "kernel_slice_path",
            "operator_slice_path",
            "task_time_slice_path",
            "op_summary_slice_path",
            "python_tracer_index_path",
        ],
        ("profiling_preprocessor", 2): ["timeline_index_path"],
        ("timeline_analyst", 3): ["classified_spans_path", "scope_gate_result_path"],
        ("step4_bootstrap_runner", 4): [
            "step4_bootstrap_result_path",
            "repo_divergence_report_path",
            "runtime_constraints_path",
            "stack_evidence_path",
            "stack_evidence_lite_path",
            "stack_call_paths_path",
            "external_mapping_targets_path",
            "graph_phase_stack_evidence_path",
            "graph_execution_plan_path",
            "graph_mapping_targets_path",
        ],
        ("graph_bootstrap_runner", 5): [
            "graph_bootstrap_result_path",
            "graph_execution_plan_path",
            "graph_mapping_targets_path",
            "graph_forward_context_path",
            "graph_seed_context_path",
            "graph_operator_spans_path",
        ],
        ("stack_mapper", 4): [
            "stack_evidence_path",
            "stack_evidence_lite_path",
            "stack_call_paths_path",
            "external_mapping_targets_path",
            "external_span_mapping_path",
            "graph_phase_stack_evidence_path",
        ],
        ("graph_path_analyst", 5): [
            "graph_execution_plan_path",
            "graph_mapping_targets_path",
            "graph_forward_context_path",
            "graph_operator_spans_path",
            "graph_span_candidates_path",
            "forward_segment_template_path",
            "graph_span_alignment_path",
        ],
        ("artifact_renderer", 6): [
            "span_code_mapping_path",
            "annotated_trace_path",
            "stream_span_timeline_path",
        ],
        ("artifact_validator", 7): ["validation_result_path"],
    }
    for artifact_key in key_map.get((agent_name, step), []):
        raw_path = str(artifacts.get(artifact_key, "")).strip()
        if raw_path:
            path = Path(raw_path)
            if path.exists():
                paths[f"artifact:{artifact_key}"] = path
    return paths


def _load_graph_operator_span_ids(state: dict[str, Any]) -> set[str]:
    raw_path = str(state.get("artifacts", {}).get("graph_operator_spans_path", "")).strip()
    if not raw_path:
        return set()
    path = Path(raw_path)
    if not path.exists():
        return set()
    payload = load_json(path)
    return {
        str(item.get("graph_operator_span_id", "")).strip()
        for item in payload.get("rows", [])
        if isinstance(item, dict) and str(item.get("graph_operator_span_id", "")).strip()
    }


def update_finalize_provenance(
    workspace_dir: Path,
    state: dict[str, Any],
    agent_name: str,
    output_files: list[Path],
) -> None:
    approved_paths = finalized_artifact_paths(workspace_dir, state, agent_name, output_files)
    hashes = collect_existing_file_hashes({label: path for label, path in approved_paths.items()})
    manifest = load_provenance_manifest(workspace_dir)
    artifacts_manifest = manifest.setdefault("artifacts", {})
    approved_at = now_iso()
    for label, sha256 in hashes.items():
        artifacts_manifest[label] = {
            "path": str(approved_paths[label]),
            "sha256": sha256,
            "approved_by_agent": agent_name,
            "approved_at": approved_at,
        }
    history = manifest.setdefault("history", [])
    history.append(
        {
            "approved_by_agent": agent_name,
            "approved_at": approved_at,
            "step": int(state.get("current_step", 0)),
            "artifact_labels": sorted(hashes.keys()),
        }
    )
    save_provenance_manifest(workspace_dir, manifest)
    orchestration = state.setdefault("orchestration", {})
    orchestration["last_finalize_agent"] = agent_name
    orchestration["last_finalize_at"] = approved_at
    orchestration["last_finalize_hashes"] = hashes
    dispatch_payload = _dispatch_payload(workspace_dir, state, agent_name)
    orchestration["last_allowed_scripts"] = dispatch_payload.get("allowed_official_scripts", [])
    orchestration["provenance_verified"] = True
    orchestration["illegal_temp_script_detected"] = False
    orchestration["last_provenance_error"] = ""


def collect_invalid_zero_line_paths(payload: Any, current_path: str = "payload") -> list[str]:
    findings: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            child_path = f"{current_path}.{key}"
            if key == "line":
                try:
                    if int(value) <= 0:
                        findings.append(child_path)
                except (TypeError, ValueError):
                    findings.append(child_path)
            elif key == "code_location" and isinstance(value, str) and ZERO_LINE_CODE_LOCATION_RE.match(value.strip()):
                findings.append(child_path)
            findings.extend(collect_invalid_zero_line_paths(value, child_path))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            findings.extend(collect_invalid_zero_line_paths(item, f"{current_path}[{index}]"))
    return findings


def _primary_code_location_for_quality_signal(row: dict[str, Any]) -> str:
    primary_code_location = str(row.get("primary_code_location", "")).strip()
    if primary_code_location:
        return primary_code_location
    code_line_candidates = row.get("code_line_candidates", [])
    if isinstance(code_line_candidates, list):
        for candidate in code_line_candidates:
            if not isinstance(candidate, dict):
                continue
            code_location = str(candidate.get("code_location", "")).strip()
            if code_location:
                return code_location
    return ""


def _primary_file_function_for_quality_signal(row: dict[str, Any]) -> str:
    primary_file_function = row.get("primary_file_function", {})
    if not isinstance(primary_file_function, dict):
        return ""
    repo_relative_path = str(primary_file_function.get("repo_relative_path", "")).strip()
    symbol = str(primary_file_function.get("symbol", "")).strip()
    if repo_relative_path and symbol:
        return f"{repo_relative_path}:{symbol}"
    return ""


def _stack_mapper_path_class(primary_file_function: str) -> str:
    path_part = primary_file_function.split(":", 1)[0].lower()
    if any(keyword in path_part for keyword in STACK_MAPPER_ORCHESTRATION_PATH_KEYWORDS):
        return "orchestration"
    if path_part:
        return "implementation_or_other"
    return "unknown"


def enrich_stack_mapper_quality_signals(payload: dict[str, Any]) -> bool:
    external_rows = payload.get("external_span_mapping_payload", {}).get("rows", [])
    if not isinstance(external_rows, list):
        return False

    code_location_counts: dict[str, int] = {}
    file_function_counts: dict[str, int] = {}
    file_function_semantic_classes: dict[str, set[str]] = {}
    file_function_span_names: dict[str, set[str]] = {}
    for row in external_rows:
        if not isinstance(row, dict):
            continue
        primary_code_location = _primary_code_location_for_quality_signal(row)
        if primary_code_location:
            code_location_counts[primary_code_location] = code_location_counts.get(primary_code_location, 0) + 1
        primary_file_function = _primary_file_function_for_quality_signal(row)
        if primary_file_function:
            file_function_counts[primary_file_function] = file_function_counts.get(primary_file_function, 0) + 1
            semantic_class = str(row.get("semantic_class", row.get("stream_role", "unknown"))).strip() or "unknown"
            span_name = str(row.get("span_name", "")).strip()
            file_function_semantic_classes.setdefault(primary_file_function, set()).add(semantic_class)
            if span_name:
                file_function_span_names.setdefault(primary_file_function, set()).add(span_name)

    quality_signals = dict(payload.get("quality_signals", {}) or {})
    changed = False
    if code_location_counts:
        top_code_location, top_code_location_count = max(
            code_location_counts.items(),
            key=lambda item: (item[1], item[0]),
        )
        if quality_signals.get("top_repeated_primary_code_location") != top_code_location:
            quality_signals["top_repeated_primary_code_location"] = top_code_location
            changed = True
        if int(quality_signals.get("top_repeated_primary_code_location_count", -1) or -1) != top_code_location_count:
            quality_signals["top_repeated_primary_code_location_count"] = top_code_location_count
            changed = True
    if file_function_counts:
        top_file_function, top_file_function_count = max(
            file_function_counts.items(),
            key=lambda item: (item[1], item[0]),
        )
        resolved_external_row_count = sum(file_function_counts.values())
        top_ratio = (
            float(top_file_function_count) / float(resolved_external_row_count)
            if resolved_external_row_count > 0
            else 0.0
        )
        semantic_class_count = len(file_function_semantic_classes.get(top_file_function, set()))
        unique_span_name_count = len(file_function_span_names.get(top_file_function, set()))
        path_class = _stack_mapper_path_class(top_file_function)
        collapse_warning = bool(
            path_class == "orchestration"
            and top_ratio > STACK_MAPPER_COLLAPSE_RATIO_THRESHOLD
            and (semantic_class_count >= 2 or unique_span_name_count >= STACK_MAPPER_COLLAPSE_UNIQUE_SPAN_NAME_THRESHOLD)
        )
        if quality_signals.get("top_repeated_primary_file_function") != top_file_function:
            quality_signals["top_repeated_primary_file_function"] = top_file_function
            changed = True
        if int(quality_signals.get("top_repeated_primary_file_function_count", -1) or -1) != top_file_function_count:
            quality_signals["top_repeated_primary_file_function_count"] = top_file_function_count
            changed = True
        if int(quality_signals.get("resolved_external_row_count", -1) or -1) != resolved_external_row_count:
            quality_signals["resolved_external_row_count"] = resolved_external_row_count
            changed = True
        if float(quality_signals.get("top_repeated_primary_file_function_ratio", -1.0) or -1.0) != round(top_ratio, 6):
            quality_signals["top_repeated_primary_file_function_ratio"] = round(top_ratio, 6)
            changed = True
        if int(quality_signals.get("top_repeated_primary_file_function_semantic_class_count", -1) or -1) != semantic_class_count:
            quality_signals["top_repeated_primary_file_function_semantic_class_count"] = semantic_class_count
            changed = True
        if int(quality_signals.get("top_repeated_primary_file_function_unique_span_name_count", -1) or -1) != unique_span_name_count:
            quality_signals["top_repeated_primary_file_function_unique_span_name_count"] = unique_span_name_count
            changed = True
        if quality_signals.get("top_repeated_primary_file_function_path_class") != path_class:
            quality_signals["top_repeated_primary_file_function_path_class"] = path_class
            changed = True
        if bool(quality_signals.get("collapse_warning", False)) != collapse_warning:
            quality_signals["collapse_warning"] = collapse_warning
            changed = True
    if changed:
        payload["quality_signals"] = quality_signals
    return changed


def collect_code_locations(payload: Any, current_path: str = "payload") -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            child_path = f"{current_path}.{key}"
            if key == "code_location" and isinstance(value, str) and value.strip():
                findings.append((child_path, value.strip()))
            findings.extend(collect_code_locations(value, child_path))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            findings.extend(collect_code_locations(item, f"{current_path}[{index}]"))
    return findings


def _read_repo_source_line(repo_root: Path, code_location: str) -> tuple[str, str, int] | None:
    match = CODE_LOCATION_RE.match(str(code_location).strip())
    if not match:
        return None
    repo_relative_path = match.group("path").strip()
    line_number = int(match.group("line"))
    line_text = read_repo_source_line(repo_root, code_location)
    if not line_text:
        return None
    return repo_relative_path, line_text, line_number


def _graph_location_violation_reason(repo_root: Path, code_location: str) -> str:
    source_line = _read_repo_source_line(repo_root, code_location)
    if source_line is None:
        return ""
    repo_relative_path, line_text, _line_number = source_line
    reason = graph_source_line_violation(code_location, repo_root)
    if reason:
        return reason
    if "graph_runner" in repo_relative_path and ".replay(" in line_text.strip():
        return "graph_runner_replay_entry"
    return ""


def _extract_graph_alignment_items(payload: Any) -> list[dict[str, Any]]:
    return extract_graph_alignment_rows(payload)


def _normalized_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _require_string_list(value: Any, label: str) -> list[str]:
    ensure(isinstance(value, list), f"{label} 必须是列表。")
    normalized = [str(item).strip() for item in value if str(item).strip()]
    ensure(normalized, f"{label} 不能为空列表。")
    return normalized


def _require_rows_payload(payload: Any, label: str) -> list[dict[str, Any]]:
    ensure(isinstance(payload, dict) and payload, f"{label} 必须是非空对象。")
    rows = payload.get("rows", [])
    ensure(isinstance(rows, list), f"{label}.rows 必须是列表。")
    dict_rows = [item for item in rows if isinstance(item, dict)]
    ensure(
        dict_rows,
        f"{label}.rows 不能为空；正式结构必须写成 {{\"status\": ..., \"row_count\": N, \"rows\": [{{...}}]}}。",
    )
    return dict_rows


def _require_object_list(value: Any, label: str, *, non_empty: bool = False) -> list[dict[str, Any]]:
    ensure(isinstance(value, list), f"{label} 必须是列表。")
    rows = [item for item in value if isinstance(item, dict)]
    ensure(len(rows) == len(value), f"{label} 必须只包含对象。")
    if non_empty:
        ensure(rows, f"{label} 不能为空列表。")
    return rows


def _validate_decision_templates(value: Any, label: str, *, require_non_empty: bool) -> list[dict[str, Any]]:
    rows = _require_object_list(value, label, non_empty=require_non_empty)
    violations: list[str] = []
    for index, row in enumerate(rows):
        row_path = f"{label}[{index}]"
        template_key = str(row.get("template_key", "")).strip()
        if not template_key:
            violations.append(f"{row_path}.template_key missing")
        selected_code_location = str(row.get("selected_code_location", "")).strip()
        if not selected_code_location:
            violations.append(f"{row_path}.selected_code_location missing")
        selected_source_line_text = str(row.get("selected_source_line_text", "")).strip()
        if not selected_source_line_text:
            violations.append(f"{row_path}.selected_source_line_text missing")
        candidate_rows = row.get("candidate_code_locations", [])
        if not isinstance(candidate_rows, list) or not candidate_rows:
            violations.append(f"{row_path}.candidate_code_locations missing")
            continue
        candidate_locations: list[str] = []
        for candidate_index, candidate in enumerate(candidate_rows):
            candidate_path = f"{row_path}.candidate_code_locations[{candidate_index}]"
            if not isinstance(candidate, dict):
                violations.append(f"{candidate_path} not object")
                continue
            candidate_code_location = str(candidate.get("code_location", "")).strip()
            if not candidate_code_location:
                violations.append(f"{candidate_path}.code_location missing")
            candidate_locations.append(candidate_code_location)
        if selected_code_location and selected_code_location not in candidate_locations:
            violations.append(f"{row_path}.selected_code_location not present in candidate_code_locations")
        rejected_candidates = row.get("rejected_candidates", [])
        if not isinstance(rejected_candidates, list):
            violations.append(f"{row_path}.rejected_candidates must be list")
        elif len(candidate_locations) > 1 and not rejected_candidates:
            violations.append(f"{row_path}.rejected_candidates missing while multiple candidates exist")
        else:
            for candidate_index, candidate in enumerate(rejected_candidates):
                candidate_path = f"{row_path}.rejected_candidates[{candidate_index}]"
                if not isinstance(candidate, dict):
                    violations.append(f"{candidate_path} not object")
                    continue
                if not str(candidate.get("code_location", "")).strip():
                    violations.append(f"{candidate_path}.code_location missing")
                if not str(candidate.get("rejection_reason", "")).strip():
                    violations.append(f"{candidate_path}.rejection_reason missing")
    ensure(
        not violations,
        "graph_path_analyst 的 decision_templates 缺少模板级候选比较/最终选择/排除说明："
        + "; ".join(violations[:20]),
    )
    return rows


def _validate_template_summary_rows(value: Any, label: str, *, require_non_empty: bool) -> list[dict[str, Any]]:
    rows = _require_object_list(value, label, non_empty=require_non_empty)
    violations: list[str] = []
    for index, row in enumerate(rows):
        row_path = f"{label}[{index}]"
        if not str(row.get("template_key", "")).strip():
            violations.append(f"{row_path}.template_key missing")
        affected_span_count = row.get("affected_span_count")
        if not isinstance(affected_span_count, int) or affected_span_count <= 0:
            violations.append(f"{row_path}.affected_span_count invalid")
        if require_non_empty and not str(row.get("stuck_at", "")).strip():
            violations.append(f"{row_path}.stuck_at missing")
        if require_non_empty and not str(row.get("why_not_freezable", "")).strip():
            violations.append(f"{row_path}.why_not_freezable missing")
    ensure(
        not violations,
        "graph_path_analyst 的模板级摘要字段不完整："
        + "; ".join(violations[:20]),
    )
    return rows


def _validate_remaining_candidates_summary(value: Any, label: str) -> list[dict[str, Any]]:
    rows = _require_object_list(value, label, non_empty=True)
    violations: list[str] = []
    for index, row in enumerate(rows):
        row_path = f"{label}[{index}]"
        if not str(row.get("template_key", "")).strip():
            violations.append(f"{row_path}.template_key missing")
        affected_span_count = row.get("affected_span_count")
        if not isinstance(affected_span_count, int) or affected_span_count <= 0:
            violations.append(f"{row_path}.affected_span_count invalid")
        candidate_rows = row.get("candidate_code_locations", [])
        if not isinstance(candidate_rows, list) or len(candidate_rows) < 2:
            violations.append(f"{row_path}.candidate_code_locations must contain at least 2 candidates")
            continue
        for candidate_index, candidate in enumerate(candidate_rows):
            candidate_path = f"{row_path}.candidate_code_locations[{candidate_index}]"
            if not isinstance(candidate, dict):
                violations.append(f"{candidate_path} not object")
                continue
            if not str(candidate.get("code_location", "")).strip():
                violations.append(f"{candidate_path}.code_location missing")
        if not str(row.get("why_candidates_remain_tied", "")).strip():
            violations.append(f"{row_path}.why_candidates_remain_tied missing")
    ensure(
        not violations,
        "graph_path_analyst 的 remaining_candidates_summary 不完整："
        + "; ".join(violations[:20]),
    )
    return rows


def _validate_elimination_attempt_rows(value: Any, label: str) -> list[dict[str, Any]]:
    rows = _require_object_list(value, label, non_empty=True)
    violations: list[str] = []
    for index, row in enumerate(rows):
        row_path = f"{label}[{index}]"
        if not str(row.get("dimension", "")).strip():
            violations.append(f"{row_path}.dimension missing")
        templates = row.get("attempted_on_templates", [])
        if not isinstance(templates, list) or not [str(item).strip() for item in templates if str(item).strip()]:
            violations.append(f"{row_path}.attempted_on_templates missing")
        if not str(row.get("outcome", "")).strip():
            violations.append(f"{row_path}.outcome missing")
    ensure(
        not violations,
        "graph_path_analyst 的 elimination_attempts 不完整："
        + "; ".join(violations[:20]),
    )
    return rows


def _blocking_issues_only_describe_direct_provenance_gap(blocking_issues: list[str]) -> bool:
    if not blocking_issues:
        return False
    normalized = [item.strip().lower() for item in blocking_issues if item.strip()]
    if not normalized:
        return False
    weak_tokens = [
        "python frame",
        "node_id",
        "graph_node_id",
        "direct provenance",
        "direct source",
        "direct mapping",
        "graph capture",
        "replay time",
        "源码 provenance",
        "python 源码",
        "没有 frame",
        "缺少 frame",
        "直接映射",
    ]
    substantive_tokens = [
        "multiple candidates",
        "多个候选",
        "remain tied",
        "并列候选",
        "cannot disambiguate",
        "无法区分",
        "same local decision area",
        "同等合理候选",
    ]
    has_weak = any(any(token in issue for token in weak_tokens) for issue in normalized)
    has_substantive = any(any(token in issue for token in substantive_tokens) for issue in normalized)
    return has_weak and not has_substantive


def _load_repo_file_facts(state: dict[str, Any]) -> tuple[Path, set[str], set[str]]:
    repo_root = Path(state["inputs"]["code_repo_path"])
    report_path_raw = str(state.get("artifacts", {}).get("repo_divergence_report_path", "")).strip()
    report = load_json(Path(report_path_raw)) if report_path_raw and Path(report_path_raw).exists() else {}
    existing_files = {
        str(item).strip()
        for item in report.get("existing_files", [])
        if str(item).strip()
    }
    missing_files = {
        str(item).strip()
        for item in report.get("missing_files", [])
        if str(item).strip()
    }
    for row in report.get("checked_files", []):
        if not isinstance(row, dict):
            continue
        relative_path = str(row.get("relative_path", "")).strip()
        if not relative_path:
            continue
        if row.get("exists"):
            existing_files.add(relative_path)
        else:
            missing_files.add(relative_path)
    return repo_root, existing_files, missing_files


def _validate_graph_plan_updates_schema(graph_plan_updates: dict[str, Any], label: str) -> None:
    if "identified_graph_span_ids" in graph_plan_updates:
        _require_string_list(graph_plan_updates.get("identified_graph_span_ids"), f"{label}.identified_graph_span_ids")
    if "phase_windows" in graph_plan_updates:
        ensure(isinstance(graph_plan_updates.get("phase_windows"), list), f"{label}.phase_windows 必须是列表。")
    if "mapping_granularity" in graph_plan_updates:
        ensure(str(graph_plan_updates.get("mapping_granularity", "")).strip(), f"{label}.mapping_granularity 不能为空。")
    if "status" in graph_plan_updates:
        ensure(str(graph_plan_updates.get("status", "")).strip(), f"{label}.status 不能为空。")


def _note_claims_missing_or_unavailable(note: str) -> bool:
    normalized = str(note or "").strip().lower()
    if not normalized:
        return False
    return any(
        token in normalized
        for token in [
            "not present",
            "missing",
            "unavailable",
            "缺失",
            "不存在",
            "不可用",
        ]
    )


def _load_external_mapping_target_rows(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_path = str(state.get("artifacts", {}).get("external_mapping_targets_path", "")).strip()
    ensure(raw_path, "缺少 state.artifacts.external_mapping_targets_path。")
    path = Path(raw_path)
    ensure(path.exists(), f"external_mapping_targets.json 不存在: {path}")
    payload = load_json(path)
    rows = payload.get("rows", [])
    ensure(isinstance(rows, list), "external_mapping_targets.json.rows 必须是列表。")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        span_id = str(row.get("span_id", "")).strip()
        if span_id:
            result[span_id] = row
    return result


def _load_graph_mapping_target_rows(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_path = str(state.get("artifacts", {}).get("graph_mapping_targets_path", "")).strip()
    ensure(raw_path, "缺少 state.artifacts.graph_mapping_targets_path。")
    path = Path(raw_path)
    ensure(path.exists(), f"graph_mapping_targets.json 不存在: {path}")
    payload = load_json(path)
    rows = payload.get("rows", [])
    ensure(isinstance(rows, list), "graph_mapping_targets.json.rows 必须是列表。")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        span_id = str(row.get("span_id", "")).strip()
        if span_id:
            result[span_id] = row
    return result


def collect_frozen_graph_target_ids(graph_mapping_targets: dict[str, Any]) -> set[str]:
    rows = graph_mapping_targets.get("rows", [])
    if not isinstance(rows, list):
        return set()
    return {
        str(row.get("span_id", "")).strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("span_id", "")).strip()
    }


def collect_frozen_graph_operator_span_map(graph_operator_spans: dict[str, Any]) -> dict[str, str]:
    operator_span_map: dict[str, str] = {}
    for row in graph_operator_spans.get("rows", []):
        if not isinstance(row, dict):
            continue
        graph_operator_span_id = str(row.get("graph_operator_span_id", "")).strip()
        span_id = str(row.get("span_id", "")).strip()
        if graph_operator_span_id and span_id:
            operator_span_map[graph_operator_span_id] = span_id
    return operator_span_map


def _load_graph_scope(state: dict[str, Any]) -> tuple[set[str], dict[str, str]]:
    artifacts = state.get("artifacts", {})
    graph_mapping_targets_path = Path(str(artifacts.get("graph_mapping_targets_path", "")).strip())
    graph_operator_spans_path = Path(str(artifacts.get("graph_operator_spans_path", "")).strip())
    ensure(graph_mapping_targets_path.exists(), f"缺少 graph_mapping_targets.json: {graph_mapping_targets_path}")
    ensure(graph_operator_spans_path.exists(), f"缺少 graph_operator_spans.json: {graph_operator_spans_path}")
    graph_mapping_targets = load_json(graph_mapping_targets_path)
    graph_operator_spans = load_json(graph_operator_spans_path)
    allowed_graph_span_ids = collect_frozen_graph_target_ids(graph_mapping_targets)
    operator_span_map = collect_frozen_graph_operator_span_map(graph_operator_spans)
    operator_span_target_ids = set(operator_span_map.values())
    ensure(operator_span_map, "graph_operator_spans.json 缺少正式 graph operator spans。")
    ensure(
        operator_span_target_ids <= allowed_graph_span_ids,
        "graph_operator_spans.json 越过了 graph_mapping_targets formal target set。"
        f" out_of_scope={sorted(operator_span_target_ids - allowed_graph_span_ids)[:20]}",
    )
    ensure(
        operator_span_target_ids == allowed_graph_span_ids,
        "graph_operator_spans.json 与 graph_mapping_targets formal target set 不一致。"
        f" missing_operator_targets={sorted(allowed_graph_span_ids - operator_span_target_ids)[:20]}",
    )
    return allowed_graph_span_ids, operator_span_map


def validate_primary_payload(agent_name: str, payload: dict[str, Any]) -> None:
    status = str(payload.get("status", "")).strip()
    ensure(status, f"{agent_name} 正式 JSON 缺少 status。")
    if agent_name == "profiling_preprocessor":
        if "slice_counts" in payload:
            slice_counts = payload.get("slice_counts", {})
            for key in ["trace_events", "kernel_rows", "operator_rows", "task_time_rows", "op_summary_rows"]:
                ensure(key in slice_counts, f"{agent_name} 缺少 slice_counts.{key}")
                ensure(int(slice_counts.get(key, 0)) >= 0, f"{agent_name} slice_counts.{key} 非法。")
            ensure(int(slice_counts.get("trace_events", 0)) > 0, "Step 1 必须至少切出一条 trace event。")
            ensure(int(slice_counts.get("kernel_rows", 0)) > 0, "Step 1 必须至少切出一条 kernel 记录。")
        if "timeline_index_summary" in payload:
            summary = payload.get("timeline_index_summary", {})
            for key in ["stream_count", "task_count", "op_count", "trace_span_count"]:
                ensure(key in summary, f"{agent_name} 缺少 timeline_index_summary.{key}")
                ensure(int(summary.get(key, 0)) >= 0, f"{agent_name} timeline_index_summary.{key} 非法。")
            ensure(int(summary.get("stream_count", 0)) > 0, "Step 2 必须至少识别一个 stream。")
            ensure(int(summary.get("trace_span_count", 0)) > 0, "Step 2 必须至少识别一个 trace span。")
    elif agent_name == "timeline_analyst":
        review_scope = payload.get("review_scope", {})
        ensure(isinstance(review_scope, dict), "timeline_analyst.review_scope 必须是对象。")
        allowed_mutation_fields = payload.get("allowed_mutation_fields", [])
        ensure(isinstance(allowed_mutation_fields, list), "timeline_analyst.allowed_mutation_fields 必须是列表。")
        ensure(isinstance(payload.get("mutation_summary", {}), dict), "timeline_analyst.mutation_summary 必须是对象。")
        ensure(isinstance(payload.get("blocking_issues", []), list), "timeline_analyst.blocking_issues 必须是列表。")
        ensure(isinstance(payload.get("stream_updates", []), list), "timeline_analyst.stream_updates 必须是列表。")
        ensure(isinstance(payload.get("span_updates", []), list), "timeline_analyst.span_updates 必须是列表。")
    elif agent_name == "artifact_renderer":
        stats = payload.get("annotated_trace_stats", {})
        for key in ["mapped_event_count", "args_code_location_count", "top_level_code_location_count"]:
            ensure(key in stats, f"{agent_name} 缺少 annotated_trace_stats.{key}")
            ensure(int(stats.get(key, 0)) >= 0, f"{agent_name} annotated_trace_stats.{key} 非法。")
        ensure(int(stats.get("top_level_code_location_count", 0)) == 0, "annotated trace 顶层不得存在 code_location。")
    elif agent_name == "graph_path_analyst":
        review_outcome = str(payload.get("review_outcome", "")).strip()
        ensure(review_outcome in {"approved", "blocked"}, "graph_path_analyst.review_outcome 必须为 approved 或 blocked。")
        ensure(str(payload.get("reviewed_mapping_granularity", "")).strip(), "graph_path_analyst 缺少 reviewed_mapping_granularity。")
        ensure(str(payload.get("review_summary", "")).strip(), "graph_path_analyst 缺少 review_summary。")
        ensure(isinstance(payload.get("knowledge_reference_check", {}), dict), "graph_path_analyst.knowledge_reference_check 必须是对象。")
        ensure(isinstance(payload.get("rules_conformance_check", {}), dict), "graph_path_analyst.rules_conformance_check 必须是对象。")
        ensure(isinstance(payload.get("repo_file_evidence_check", {}), dict), "graph_path_analyst.repo_file_evidence_check 必须是对象。")
        ensure(isinstance(payload.get("path_reconstruction", {}), dict), "graph_path_analyst.path_reconstruction 必须是对象。")
        ensure(isinstance(payload.get("span_alignment", {}), dict), "graph_path_analyst.span_alignment 必须是对象。")
        if "decision_templates" in payload:
            _validate_decision_templates(payload.get("decision_templates", []), "decision_templates", require_non_empty=False)
        if "unresolved_template_summary" in payload:
            _validate_template_summary_rows(
                payload.get("unresolved_template_summary", []),
                "unresolved_template_summary",
                require_non_empty=False,
            )
        if "resolved_template_summary" in payload:
            _validate_template_summary_rows(
                payload.get("resolved_template_summary", []),
                "resolved_template_summary",
                require_non_empty=False,
            )
        promotion = payload.get("artifact_promotion", {})
        ensure(isinstance(promotion, dict), "graph_path_analyst.artifact_promotion 必须是对象。")
        if status == "partial":
            ensure(review_outcome == "blocked", "graph_path_analyst status=partial 时 review_outcome 必须为 blocked。")
            blocking_issues = _normalized_string_list(payload.get("blocking_issues", []))
            ensure(blocking_issues, "graph_path_analyst status=partial 时必须提供非空 blocking_issues。")
            _validate_template_summary_rows(
                payload.get("unresolved_template_summary", []),
                "unresolved_template_summary",
                require_non_empty=True,
            )
            for key in [
                "graph_span_candidates_payload",
                "forward_segment_template_payload",
            ]:
                ensure(
                    isinstance(promotion.get(key, {}), dict) and bool(promotion.get(key)),
                    f"graph_path_analyst status=partial 时必须提供 artifact_promotion.{key}。",
                )
            graph_plan_updates = promotion.get("graph_execution_plan_updates", {}) or {}
            graph_forward_context_updates = promotion.get("graph_forward_context_updates", {}) or {}
            if graph_plan_updates:
                _validate_graph_plan_updates_schema(
                    graph_plan_updates,
                    "artifact_promotion.graph_execution_plan_updates",
                )
                ensure(
                    str(graph_plan_updates.get("status", "")).strip() == "partial",
                    "graph_path_analyst status=partial 时 artifact_promotion.graph_execution_plan_updates.status 必须为 partial。",
                )
            if graph_forward_context_updates:
                ensure(
                    str(graph_forward_context_updates.get("status", "")).strip() == "partial",
                    "graph_path_analyst status=partial 时 artifact_promotion.graph_forward_context_updates.status 必须为 partial。",
                )
            _require_rows_payload(
                promotion.get("graph_span_candidates_payload", {}),
                "artifact_promotion.graph_span_candidates_payload",
            )
            _require_rows_payload(
                promotion.get("forward_segment_template_payload", {}),
                "artifact_promotion.forward_segment_template_payload",
            )
            if promotion.get("graph_span_alignment_payload"):
                alignment_items = _extract_graph_alignment_items(promotion.get("graph_span_alignment_payload", {}))
                partial_alignment_violations: list[str] = []
                unresolved_rows = 0
                for index, item in enumerate(alignment_items):
                    item_path = f"artifact_promotion.graph_span_alignment_payload.items[{index}]"
                    if not str(item.get("span_id", "")).strip():
                        partial_alignment_violations.append(f"{item_path}.span_id missing")
                    if not str(item.get("graph_operator_span_id", "")).strip():
                        partial_alignment_violations.append(f"{item_path}.graph_operator_span_id missing")
                    location_kind = str(item.get("location_kind", "")).strip()
                    if "location_kind" not in item:
                        partial_alignment_violations.append(f"{item_path}.location_kind missing")
                    elif location_kind not in ALLOWED_GRAPH_LOCATION_KINDS:
                        partial_alignment_violations.append(f"{item_path}.location_kind={location_kind or '<missing>'}")
                    if "operator_evidence_kind" not in item:
                        partial_alignment_violations.append(f"{item_path}.operator_evidence_kind missing")
                    if "requires_further_drilldown" not in item:
                        partial_alignment_violations.append(f"{item_path}.requires_further_drilldown missing")
                    if item.get("requires_further_drilldown") is True or location_kind != "operator_call":
                        unresolved_rows += 1
                ensure(
                    not partial_alignment_violations,
                    "graph_path_analyst status=partial 时若提交 graph_span_alignment_payload，仍需保持结构化字段完整："
                    f"{partial_alignment_violations[:20]}",
                )
                ensure(
                    unresolved_rows > 0,
                    "graph_path_analyst status=partial 时若提交 graph_span_alignment_payload，至少要保留明确未完成下钻的条目。",
                )
        if status == "passed":
            ensure(review_outcome == "approved", "graph_path_analyst status=passed 时 review_outcome 必须为 approved。")
            ensure(
                isinstance(promotion.get("graph_execution_plan_updates", {}), dict)
                and bool(promotion.get("graph_execution_plan_updates")),
                "graph_path_analyst status=passed 时必须提供 artifact_promotion.graph_execution_plan_updates。",
            )
            ensure(
                isinstance(promotion.get("graph_forward_context_updates", {}), dict)
                and bool(promotion.get("graph_forward_context_updates")),
                "graph_path_analyst status=passed 时必须提供 artifact_promotion.graph_forward_context_updates。",
            )
            for key in [
                "graph_span_candidates_payload",
                "forward_segment_template_payload",
                "graph_span_alignment_payload",
            ]:
                ensure(
                    isinstance(promotion.get(key, {}), dict) and bool(promotion.get(key)),
                    f"graph_path_analyst status=passed 时必须提供 artifact_promotion.{key}。",
                )
            _validate_graph_plan_updates_schema(
                promotion.get("graph_execution_plan_updates", {}) or {},
                "artifact_promotion.graph_execution_plan_updates",
            )
            _require_rows_payload(
                promotion.get("graph_span_candidates_payload", {}),
                "artifact_promotion.graph_span_candidates_payload",
            )
            _require_rows_payload(
                promotion.get("forward_segment_template_payload", {}),
                "artifact_promotion.forward_segment_template_payload",
            )
            ensure(
                _extract_graph_alignment_items(promotion.get("graph_span_alignment_payload", {})),
                "graph_path_analyst status=passed 时 graph_span_alignment_payload 必须提供非空逐 span items/rows。",
            )
            _validate_decision_templates(payload.get("decision_templates", []), "decision_templates", require_non_empty=True)
            invalid_zero_line_paths = collect_invalid_zero_line_paths(payload)
            ensure(
                not invalid_zero_line_paths,
                "graph_path_analyst status=passed 时不得包含 line<=0 或 code_location 以 :0 结尾的占位定位，"
                f"发现位置: {invalid_zero_line_paths[:12]}",
            )
    elif agent_name == "stack_mapper":
        ensure(isinstance(payload.get("coverage", {}), dict), "stack_mapper.coverage 必须是对象。")
        ensure(
            isinstance(payload.get("external_span_mapping_payload", {}), dict),
            "stack_mapper.external_span_mapping_payload 必须是对象。",
        )
        ensure(isinstance(payload.get("evidence_inputs", {}), dict), "stack_mapper.evidence_inputs 必须是对象。")
        if status == "passed":
            ensure(
                isinstance(payload.get("external_span_mapping_payload", {}).get("rows", []), list),
                "stack_mapper status=passed 时 external_span_mapping_payload.rows 必须是列表。",
            )
    elif agent_name == "step4_bootstrap_runner":
        ensure(status == "passed", "step4_bootstrap_runner 只允许输出 status=passed。")
        ensure(str(payload.get("bootstrap_target", "")).strip() == "step4_stack_mapper", "step4_bootstrap_runner.bootstrap_target 必须为 step4_stack_mapper。")
        ensure(bool(payload.get("required_artifacts_ready")), "step4_bootstrap_runner.required_artifacts_ready 必须为 true。")
        ensure(bool(payload.get("required_flags_ready")), "step4_bootstrap_runner.required_flags_ready 必须为 true。")
        ready_summary = payload.get("ready_summary", {})
        ensure(isinstance(ready_summary, dict), "step4_bootstrap_runner.ready_summary 必须是对象。")
        ensure(bool(ready_summary.get("ready")), "step4_bootstrap_runner.ready_summary.ready 必须为 true。")
        blocking_issues = payload.get("blocking_issues", [])
        ensure(isinstance(blocking_issues, list), "step4_bootstrap_runner.blocking_issues 必须是列表。")
        ensure(not blocking_issues, "step4_bootstrap_runner.blocking_issues 必须为空列表。")
    elif agent_name == "graph_bootstrap_runner":
        ensure(status == "passed", "graph_bootstrap_runner 只允许输出 status=passed。")
        ensure(str(payload.get("bootstrap_target", "")).strip() == "step5_graph_path_analyst", "graph_bootstrap_runner.bootstrap_target 必须为 step5_graph_path_analyst。")
        ensure(bool(payload.get("required_artifacts_ready")), "graph_bootstrap_runner.required_artifacts_ready 必须为 true。")
        ensure(bool(payload.get("required_flags_ready")), "graph_bootstrap_runner.required_flags_ready 必须为 true。")
        ready_summary = payload.get("ready_summary", {})
        ensure(isinstance(ready_summary, dict), "graph_bootstrap_runner.ready_summary 必须是对象。")
        ensure(bool(ready_summary.get("ready")), "graph_bootstrap_runner.ready_summary.ready 必须为 true。")
        blocking_issues = payload.get("blocking_issues", [])
        ensure(isinstance(blocking_issues, list), "graph_bootstrap_runner.blocking_issues 必须是列表。")
        ensure(not blocking_issues, "graph_bootstrap_runner.blocking_issues 必须为空列表。")


def validate_stack_mapper_target_scope(state: dict[str, Any], payload: dict[str, Any]) -> None:
    target_rows = _load_external_mapping_target_rows(state)
    target_span_ids = set(target_rows.keys())
    external_rows = payload.get("external_span_mapping_payload", {}).get("rows", [])
    if not isinstance(external_rows, list):
        return
    violations: list[str] = []
    seen_span_ids: set[str] = set()
    for index, row in enumerate(external_rows):
        if not isinstance(row, dict):
            continue
        span_id = str(row.get("span_id", "")).strip()
        row_path = f"external_span_mapping_payload.rows[{index}]"
        if not span_id:
            violations.append(f"{row_path}.span_id missing")
            continue
        if span_id not in target_span_ids:
            violations.append(f"{row_path}.span_id={span_id} 不在 external_mapping_targets.json 中")
        if span_id in seen_span_ids:
            violations.append(f"{row_path}.span_id={span_id} duplicated")
        seen_span_ids.add(span_id)
    coverage = payload.get("coverage", {})
    if isinstance(coverage, dict) and target_span_ids:
        if "approved_external_target_count" in coverage:
            approved_count = int(coverage.get("approved_external_target_count", -1) or -1)
            if approved_count != len(target_span_ids):
                violations.append(
                    "coverage.approved_external_target_count 与 external_mapping_targets.json.summary.approved_target_count 不一致"
                )
        mapped_count = int(coverage.get("mapped_external_target_count", 0) or 0)
        unresolved_count = int(coverage.get("unresolved_external_target_count", 0) or 0)
        if mapped_count < 0 or unresolved_count < 0:
            violations.append("coverage 中的 mapped/unresolved external target 计数不能为负数")
        if mapped_count + unresolved_count > len(target_span_ids):
            violations.append("coverage.mapped_external_target_count + unresolved_external_target_count 超过冻结 target set")
    ensure(
        not violations,
        "stack_mapper 输出越过了 Step4 冻结 target set 或 coverage 与冻结 target set 不一致："
        + "; ".join(violations[:20]),
    )


def validate_stack_mapper_evidence_consistency(workspace_dir: Path, state: dict[str, Any], payload: dict[str, Any]) -> None:
    evidence_inputs = payload.get("evidence_inputs", {})
    flags = state.get("flags", {})
    artifacts = state.get("artifacts", {})

    stack_call_paths_path = Path(str(artifacts.get("stack_call_paths_path", "")).strip()) if artifacts.get("stack_call_paths_path") else None
    stack_call_paths_available = bool(flags.get("stack_call_paths_built")) and bool(stack_call_paths_path and stack_call_paths_path.exists())
    if stack_call_paths_available:
        ensure(
            bool(evidence_inputs.get("stack_call_paths_built")),
            "stack_mapper.evidence_inputs.stack_call_paths_built 与真实工作区不一致：当前 stack_call_paths.json 已存在且 flag=true。",
        )
        ensure(
            not _note_claims_missing_or_unavailable(str(evidence_inputs.get("stack_call_paths_note", ""))),
            "stack_mapper.evidence_inputs.stack_call_paths_note 错误地声称 stack_call_paths.json 缺失或不可用。",
        )

    python_tracer_index_path = (
        Path(str(artifacts.get("python_tracer_index_path", "")).strip()) if artifacts.get("python_tracer_index_path") else None
    )
    python_tracer_available = bool(flags.get("python_tracer_index_built")) and bool(
        python_tracer_index_path and python_tracer_index_path.exists()
    )
    if python_tracer_available:
        python_tracer_payload = load_json(python_tracer_index_path)
        tracer_stats = python_tracer_payload.get("stats", {})
        expected_total = int(tracer_stats.get("total_frame_count", 0) or 0)
        expected_repo = int(tracer_stats.get("repo_frame_count", 0) or 0)
        if "python_tracer_frames" in evidence_inputs:
            ensure(
                int(evidence_inputs.get("python_tracer_frames", -1)) == expected_total,
                "stack_mapper.evidence_inputs.python_tracer_frames 与 python_tracer_index.json.stats.total_frame_count 不一致。",
            )
        if "python_tracer_repo_frames" in evidence_inputs:
            ensure(
                int(evidence_inputs.get("python_tracer_repo_frames", -1)) == expected_repo,
                "stack_mapper.evidence_inputs.python_tracer_repo_frames 与 python_tracer_index.json.stats.repo_frame_count 不一致。",
            )
        if expected_repo > 0:
            ensure(
                not _note_claims_missing_or_unavailable(str(evidence_inputs.get("python_tracer_note", ""))),
                "stack_mapper.evidence_inputs.python_tracer_note 错误地声称 python tracer 不可用。",
            )
            ensure(
                "all classified as external_python" not in str(evidence_inputs.get("python_tracer_note", "")).lower(),
                "stack_mapper.evidence_inputs.python_tracer_note 仍沿用旧结论：全部 external_python。",
            )


def _has_valid_file_function(anchor: Any) -> bool:
    return (
        isinstance(anchor, dict)
        and bool(str(anchor.get("repo_relative_path", "")).strip())
        and bool(str(anchor.get("symbol", "")).strip())
        and int(anchor.get("entry_line", anchor.get("line", 0)) or 0) > 0
    )


def validate_stack_mapper_location_quality(payload: dict[str, Any]) -> None:
    status = str(payload.get("status", "")).strip()
    external_rows = payload.get("external_span_mapping_payload", {}).get("rows", [])
    if not isinstance(external_rows, list) or not external_rows:
        return

    contradictions: list[str] = []
    for index, row in enumerate(external_rows):
        row_path = f"external_span_mapping_payload.rows[{index}]"
        if not isinstance(row, dict):
            continue
        primary_kind = str(row.get("primary_code_location_kind", "")).strip()
        file_function_candidates = row.get("file_function_candidates", [])
        if not isinstance(file_function_candidates, list):
            file_function_candidates = []
        primary_file_function = row.get("primary_file_function", {})
        code_line_candidates = row.get("code_line_candidates", [])
        if not isinstance(code_line_candidates, list):
            code_line_candidates = []
        has_code_line_candidates = any(
            isinstance(candidate, dict) and bool(str(candidate.get("code_location", "")).strip())
            for candidate in code_line_candidates
        )

        if primary_kind == "function_entry_fallback" and not _has_valid_file_function(primary_file_function):
            contradictions.append(f"{row_path}.primary_code_location_kind=function_entry_fallback but primary_file_function missing")
        if primary_kind in {"call_stack_line_candidate", "python_tracer_line_candidate", "neighbor_span_refine", "semantic_line_selection"} and not has_code_line_candidates:
            contradictions.append(f"{row_path}.primary_code_location_kind={primary_kind} but code_line_candidates missing")
        if primary_file_function and not _has_valid_file_function(primary_file_function):
            contradictions.append(f"{row_path}.primary_file_function invalid")
        if primary_file_function and file_function_candidates:
            primary_key = (
                str(primary_file_function.get("repo_relative_path", "")).strip(),
                str(primary_file_function.get("symbol", "")).strip(),
            )
            candidate_keys = {
                (
                    str(candidate.get("repo_relative_path", "")).strip(),
                    str(candidate.get("symbol", "")).strip(),
                )
                for candidate in file_function_candidates
                if isinstance(candidate, dict)
            }
            if primary_key not in candidate_keys:
                contradictions.append(f"{row_path}.primary_file_function not present in file_function_candidates")

    ensure(
        not contradictions,
        "stack_mapper 定位结果与文件:函数/代码行候选明显自相矛盾："
        + "; ".join(contradictions[:20]),
    )


def validate_stack_mapper_semantic_collapse_quality(payload: dict[str, Any]) -> None:
    external_rows = payload.get("external_span_mapping_payload", {}).get("rows", [])
    if not isinstance(external_rows, list) or not external_rows:
        return
    quality_signals = dict(payload.get("quality_signals", {}) or {})
    resolved_external_row_count = int(quality_signals.get("resolved_external_row_count", 0) or 0)
    if resolved_external_row_count <= 0:
        return
    top_ratio = float(quality_signals.get("top_repeated_primary_file_function_ratio", 0.0) or 0.0)
    path_class = str(quality_signals.get("top_repeated_primary_file_function_path_class", "")).strip()
    semantic_class_count = int(quality_signals.get("top_repeated_primary_file_function_semantic_class_count", 0) or 0)
    unique_span_name_count = int(quality_signals.get("top_repeated_primary_file_function_unique_span_name_count", 0) or 0)
    collapse_warning = bool(quality_signals.get("collapse_warning", False))
    ensure(
        not (
            collapse_warning
            and path_class == "orchestration"
            and top_ratio > STACK_MAPPER_COLLAPSE_RATIO_THRESHOLD
            and (semantic_class_count >= 2 or unique_span_name_count >= STACK_MAPPER_COLLAPSE_UNIQUE_SPAN_NAME_THRESHOLD)
        ),
        "stack_mapper 输出出现高频协调层函数吞并异构 span 的语义塌缩："
        f" top={quality_signals.get('top_repeated_primary_file_function', '<missing>')},"
        f" ratio={top_ratio:.4f}, semantic_classes={semantic_class_count}, unique_span_names={unique_span_name_count}",
    )


def validate_stack_mapper_communication_downgrade(payload: dict[str, Any]) -> None:
    external_rows = payload.get("external_span_mapping_payload", {}).get("rows", [])
    if not isinstance(external_rows, list) or not external_rows:
        return
    violations: list[str] = []
    precise_location_kinds = {
        "call_stack_line_candidate",
        "python_tracer_line_candidate",
        "neighbor_span_refine",
        "semantic_line_selection",
    }
    for index, row in enumerate(external_rows):
        if not isinstance(row, dict):
            continue
        row_path = f"external_span_mapping_payload.rows[{index}]"
        semantic_category = str(row.get("semantic_class", row.get("stream_role", "unknown"))).strip()
        primary_kind = str(row.get("primary_code_location_kind", "")).strip()
        implementation_evidence_present = bool(row.get("implementation_evidence_present", False))
        recommended_primary_location_kind = str(row.get("recommended_primary_location_kind", "")).strip()
        recommendation_reason = str(row.get("recommended_unresolved_reason", "")).strip()
        unresolved_reason = str(row.get("unresolved_reason", "")).strip()
        selection_reason = str(row.get("selection_reason", "")).strip()
        if semantic_category == "communication" and not implementation_evidence_present:
            if recommended_primary_location_kind == "function_entry_fallback" and primary_kind in precise_location_kinds:
                violations.append(
                    f"{row_path} communication span 缺少实现层 repo frame，却仍输出精确 code line kind={primary_kind}"
                )
            if primary_kind == "function_entry_fallback" and not (unresolved_reason or selection_reason or recommendation_reason):
                violations.append(f"{row_path} function_entry_fallback 缺少 why-not-resolved 说明")
    ensure(
        not violations,
        "stack_mapper communication span 降级策略不一致："
        + "; ".join(violations[:20]),
    )


def validate_graph_path_location_quality(state: dict[str, Any], payload: dict[str, Any]) -> None:
    if str(payload.get("status", "")).strip() != "passed":
        return
    repo_root = Path(state["inputs"]["code_repo_path"])
    graph_operator_span_ids = _load_graph_operator_span_ids(state)
    ensure(graph_operator_span_ids, "graph_path_analyst finalize 时缺少 graph_operator_spans.json 或其中没有正式 operator spans。")
    promotion = payload.get("artifact_promotion", {})
    graph_alignment_payload = {}
    if isinstance(promotion, dict):
        graph_alignment_payload = promotion.get("graph_span_alignment_payload", {}) or {}
    graph_alignment_source = graph_alignment_payload or payload.get("span_alignment", {})
    code_locations = collect_code_locations(graph_alignment_source, "graph_span_alignment")
    violations: list[str] = []
    for payload_path, code_location in code_locations:
        reason = _graph_location_violation_reason(repo_root, code_location)
        if reason:
            violations.append(f"{payload_path} -> {code_location} ({reason})")
    items = _extract_graph_alignment_items(graph_alignment_source)
    for index, item in enumerate(items):
        item_path = f"graph_span_alignment.items[{index}]"
        if not str(item.get("span_id", "")).strip():
            violations.append(f"{item_path}.span_id=<missing>")
        graph_operator_span_id = str(item.get("graph_operator_span_id", "")).strip()
        if not graph_operator_span_id:
            violations.append(f"{item_path}.graph_operator_span_id=<missing>")
        elif graph_operator_span_id not in graph_operator_span_ids:
            violations.append(f"{item_path}.graph_operator_span_id={graph_operator_span_id} unresolved")
        location_kind = str(item.get("location_kind", "")).strip()
        operator_evidence_kind = str(item.get("operator_evidence_kind", "")).strip()
        requires_further_drilldown = item.get("requires_further_drilldown")
        if location_kind != "operator_call":
            violations.append(f"{item_path}.location_kind={location_kind or '<missing>'}")
        if operator_evidence_kind not in ALLOWED_OPERATOR_EVIDENCE_KINDS:
            violations.append(f"{item_path}.operator_evidence_kind={operator_evidence_kind or '<missing>'}")
        if requires_further_drilldown is not False:
            violations.append(f"{item_path}.requires_further_drilldown={requires_further_drilldown!r}")
    ensure(
        not violations,
        "graph_path_analyst status=passed 时最终 code_location 不得停在模块调用边界、构造行或 replay() 入口，且必须显式满足 "
        "location_kind=operator_call / operator_evidence_kind 合法 / requires_further_drilldown=false，"
        f"发现位置: {violations[:20]}",
    )


def validate_graph_path_target_scope(state: dict[str, Any], payload: dict[str, Any]) -> None:
    allowed_graph_target_ids, operator_span_map = _load_graph_scope(state)
    promotion = payload.get("artifact_promotion", {}) or {}
    violations: list[str] = []

    graph_plan_updates = promotion.get("graph_execution_plan_updates", {}) or {}
    if isinstance(graph_plan_updates, dict) and "identified_graph_span_ids" in graph_plan_updates:
        for span_id in [str(item).strip() for item in graph_plan_updates.get("identified_graph_span_ids", []) if str(item).strip()]:
            if span_id not in allowed_graph_target_ids:
                violations.append(
                    "artifact_promotion.graph_execution_plan_updates.identified_graph_span_ids contains span outside graph_mapping_targets "
                    f"{span_id}"
                )
    if isinstance(graph_plan_updates, dict):
        for window_index, window in enumerate(graph_plan_updates.get("phase_windows", [])):
            if not isinstance(window, dict):
                continue
            row_path = f"artifact_promotion.graph_execution_plan_updates.phase_windows[{window_index}]"
            for span_item in window.get("span_ids", []):
                normalized = str(span_item).strip()
                if normalized and normalized not in allowed_graph_target_ids:
                    violations.append(f"{row_path}.span_ids contains span outside graph_mapping_targets {normalized}")

    candidate_rows = promotion.get("graph_span_candidates_payload", {}).get("rows", [])
    if isinstance(candidate_rows, list):
        for index, row in enumerate(candidate_rows):
            if not isinstance(row, dict):
                continue
            row_path = f"artifact_promotion.graph_span_candidates_payload.rows[{index}]"
            span_id = str(row.get("span_id", "")).strip()
            if span_id and span_id not in allowed_graph_target_ids:
                violations.append(f"{row_path}.span_id={span_id} 不在 graph_mapping_targets formal target 范围内")
            for span_item in row.get("span_ids", []):
                normalized = str(span_item).strip()
                if normalized and normalized not in allowed_graph_target_ids:
                    violations.append(f"{row_path}.span_ids contains span outside graph_mapping_targets {normalized}")

    alignment_rows = _extract_graph_alignment_items(promotion.get("graph_span_alignment_payload", {}) or {})
    for index, row in enumerate(alignment_rows):
        if not isinstance(row, dict):
            continue
        row_path = f"artifact_promotion.graph_span_alignment_payload.rows[{index}]"
        span_id = str(row.get("span_id", "")).strip()
        graph_operator_span_id = str(row.get("graph_operator_span_id", "")).strip()
        if span_id and span_id not in allowed_graph_target_ids:
            violations.append(f"{row_path}.span_id={span_id} 不在 graph_mapping_targets formal target 范围内")
        if not graph_operator_span_id:
            continue
        expected_span_id = operator_span_map.get(graph_operator_span_id)
        if not expected_span_id:
            violations.append(f"{row_path}.graph_operator_span_id={graph_operator_span_id} 无法在 graph_operator_spans.json 回溯")
            continue
        if span_id and span_id != expected_span_id:
            violations.append(
                f"{row_path}.span_id={span_id} 与 graph_operator_span_id={graph_operator_span_id} 对应 span_id={expected_span_id} 不一致"
            )

    ensure(
        not violations,
        "graph_path_analyst 输出越过了 Step5 既有 formal graph target / operator skeleton 的正式范围："
        + "; ".join(violations[:20]),
    )


def validate_graph_path_knowledge_checks(payload: dict[str, Any]) -> None:
    knowledge_check = payload.get("knowledge_reference_check", {}) or {}
    rules_check = payload.get("rules_conformance_check", {}) or {}
    base_violations: list[str] = []
    for key in [
        "model_config_and_launch_fields_read",
        "sglang_path_map_read",
        "forward_analysis_rules_read",
        "repo_and_profiling_override_acknowledged",
    ]:
        if key not in knowledge_check:
            base_violations.append(f"knowledge_reference_check.{key} missing")
    if "notes" not in knowledge_check:
        base_violations.append("knowledge_reference_check.notes missing")
    for key in [
        "checked_against_forward_analysis_rules",
        "status",
        "summary",
        "violations",
    ]:
        if key not in rules_check:
            base_violations.append(f"rules_conformance_check.{key} missing")
    ensure(
        not base_violations,
        "graph_path_analyst 缺少知识文档阅读/规则符合性检查字段："
        + "; ".join(base_violations[:20]),
    )

    if str(payload.get("status", "")).strip() != "passed":
        return

    passed_violations: list[str] = []
    for key in [
        "model_config_and_launch_fields_read",
        "sglang_path_map_read",
        "forward_analysis_rules_read",
        "repo_and_profiling_override_acknowledged",
    ]:
        if knowledge_check.get(key) is not True:
            passed_violations.append(f"knowledge_reference_check.{key}={knowledge_check.get(key)!r}")
    if rules_check.get("checked_against_forward_analysis_rules") is not True:
        passed_violations.append(
            "rules_conformance_check.checked_against_forward_analysis_rules="
            f"{rules_check.get('checked_against_forward_analysis_rules')!r}"
        )
    if str(rules_check.get("status", "")).strip() not in {"aligned", "repo_override"}:
        passed_violations.append(f"rules_conformance_check.status={rules_check.get('status')!r}")
    if not str(rules_check.get("summary", "")).strip():
        passed_violations.append("rules_conformance_check.summary empty")
    if not isinstance(rules_check.get("violations"), list):
        passed_violations.append("rules_conformance_check.violations not list")
    ensure(
        not passed_violations,
        "graph_path_analyst status=passed 时必须显式确认已先阅读 3 份 knowledge 文档，并完成 forward_analysis_rules 符合性检查；"
        f"发现问题: {passed_violations[:20]}",
    )


def validate_graph_path_repo_file_evidence(state: dict[str, Any], payload: dict[str, Any]) -> None:
    evidence = payload.get("repo_file_evidence_check", {}) or {}
    base_violations: list[str] = []
    for key in [
        "checked_against_repo_divergence_report",
        "existing_files_relied_on",
        "missing_files_relied_on",
        "contradictions",
    ]:
        if key not in evidence:
            base_violations.append(f"repo_file_evidence_check.{key} missing")
    ensure(
        not base_violations,
        "graph_path_analyst 缺少 repo 文件存在性核对字段：" + "; ".join(base_violations[:20]),
    )

    ensure(
        evidence.get("checked_against_repo_divergence_report") is True,
        "graph_path_analyst 必须显式确认已对照 repo_divergence_report.json 复核文件存在性。",
    )
    ensure(
        isinstance(evidence.get("existing_files_relied_on"), list),
        "repo_file_evidence_check.existing_files_relied_on 必须是列表。",
    )
    ensure(
        isinstance(evidence.get("missing_files_relied_on"), list),
        "repo_file_evidence_check.missing_files_relied_on 必须是列表。",
    )
    ensure(
        isinstance(evidence.get("contradictions"), list),
        "repo_file_evidence_check.contradictions 必须是列表。",
    )

    repo_root, repo_existing_files, repo_missing_files = _load_repo_file_facts(state)
    contradictions = [str(item).strip() for item in evidence.get("contradictions", []) if str(item).strip()]
    validation_violations: list[str] = []

    for relative_path in [str(item).strip() for item in evidence.get("existing_files_relied_on", []) if str(item).strip()]:
        exists_in_repo = (repo_root / relative_path).exists()
        if not exists_in_repo:
            validation_violations.append(f"existing_files_relied_on 声称存在但 repo 中不存在: {relative_path}")
        if relative_path in repo_missing_files and relative_path not in repo_existing_files:
            validation_violations.append(f"existing_files_relied_on 与 repo_divergence_report.json 冲突: {relative_path}")

    for relative_path in [str(item).strip() for item in evidence.get("missing_files_relied_on", []) if str(item).strip()]:
        exists_in_repo = (repo_root / relative_path).exists()
        if exists_in_repo:
            validation_violations.append(f"missing_files_relied_on 声称缺失但 repo 中存在: {relative_path}")
        if relative_path in repo_existing_files:
            validation_violations.append(f"missing_files_relied_on 与 repo_divergence_report.json 冲突: {relative_path}")

    if contradictions:
        validation_violations.append(
            "repo_file_evidence_check.contradictions 非空；该字段只允许保留仍未消解的 repo 文件事实冲突。"
            f" 若只是上游 task/seed/plan 输入之间的描述不一致，请改写到 blocking_issues/review_summary/notes: {contradictions[:10]}"
        )

    ensure(
        not validation_violations,
        "graph_path_analyst 的文件存在性结论与 repo 实际情况或 repo_divergence_report.json 冲突："
        f"{validation_violations[:20]}",
    )


def validate_graph_path_template_evidence(payload: dict[str, Any]) -> None:
    status = str(payload.get("status", "")).strip()
    decision_templates = _validate_decision_templates(
        payload.get("decision_templates", []),
        "decision_templates",
        require_non_empty=(status == "passed"),
    )
    decision_template_keys = {
        str(row.get("template_key", "")).strip()
        for row in decision_templates
        if str(row.get("template_key", "")).strip()
    }
    if status == "partial":
        unresolved_rows = _validate_template_summary_rows(
            payload.get("unresolved_template_summary", []),
            "unresolved_template_summary",
            require_non_empty=True,
        )
        remaining_candidates_rows = _validate_remaining_candidates_summary(
            payload.get("remaining_candidates_summary", []),
            "remaining_candidates_summary",
        )
        _validate_elimination_attempt_rows(payload.get("elimination_attempts", []), "elimination_attempts")
        _require_string_list(payload.get("non_disambiguating_evidence", []), "non_disambiguating_evidence")
        ensure(
            str(payload.get("why_further_inference_is_not_possible", "")).strip(),
            "graph_path_analyst status=partial 时 why_further_inference_is_not_possible 不能为空。",
        )
        blocking_issues = _normalized_string_list(payload.get("blocking_issues", []))
        ensure(blocking_issues, "graph_path_analyst status=partial 时 blocking_issues 不能为空。")
        ensure(
            not _blocking_issues_only_describe_direct_provenance_gap(blocking_issues),
            "graph_path_analyst status=partial 不能仅以缺少 Python frame / node_id / direct provenance 作为阻塞理由；"
            "必须证明存在多个经结构化排除后仍无法消解的候选。",
        )
        unresolved_keys = {
            str(row.get("template_key", "")).strip()
            for row in unresolved_rows
            if str(row.get("template_key", "")).strip()
        }
        ensure(unresolved_keys, "graph_path_analyst status=partial 时 unresolved_template_summary 不能为空。")
        remaining_template_keys = {
            str(row.get("template_key", "")).strip()
            for row in remaining_candidates_rows
            if str(row.get("template_key", "")).strip()
        }
        ensure(
            unresolved_keys.issubset(remaining_template_keys),
            "graph_path_analyst status=partial 时 unresolved_template_summary 的每个模板都必须出现在 remaining_candidates_summary 中。",
        )
        return

    if status != "passed":
        return

    blocking_issues = payload.get("blocking_issues", [])
    ensure(
        isinstance(blocking_issues, list) and len(blocking_issues) == 0,
        "graph_path_analyst status=passed 时 blocking_issues 必须为空列表。",
    )

    promotion = payload.get("artifact_promotion", {}) or {}
    alignment_rows = _extract_graph_alignment_items(promotion.get("graph_span_alignment_payload", {}) or {})
    row_violations: list[str] = []
    row_template_keys: set[str] = set()
    for index, row in enumerate(alignment_rows):
        row_path = f"artifact_promotion.graph_span_alignment_payload.rows[{index}]"
        template_key = str(row.get("template_key", "")).strip()
        if not template_key:
            row_violations.append(f"{row_path}.template_key missing")
        else:
            row_template_keys.add(template_key)
            if template_key not in decision_template_keys:
                row_violations.append(f"{row_path}.template_key={template_key} missing in decision_templates")
        if not str(row.get("selected_source_line_text", "")).strip():
            row_violations.append(f"{row_path}.selected_source_line_text missing")
    ensure(
        not row_violations,
        "graph_path_analyst status=passed 时缺少 row 级最小证明字段，或 row/template 绑定不完整："
        + "; ".join(row_violations[:20]),
    )


def ensure_state_artifact(state: dict[str, Any], agent_name: str, artifact_key: str, expected_path: Path) -> None:
    artifacts = state.get("artifacts", {})
    actual_path = str(artifacts.get(artifact_key, "")).strip()
    ensure(actual_path, f"{agent_name} 缺少 state.artifacts.{artifact_key}。")
    ensure(actual_path == str(expected_path), f"{agent_name} 的 {artifact_key} 路径错误: {actual_path}")
    ensure(expected_path.exists(), f"{agent_name} 缺少工件: {expected_path}")


def validate_profiling_preprocessor_outputs(workspace_dir: Path, state: dict[str, Any], current_step: int) -> None:
    if current_step == 1:
        expected_artifacts = {
            "trace_slice_path": workspace_dir / "artifacts" / "slices" / "trace_slice.json",
            "kernel_slice_path": workspace_dir / "artifacts" / "slices" / "kernel_details_slice.csv",
            "operator_slice_path": workspace_dir / "artifacts" / "slices" / "operator_details_slice.csv",
            "task_time_slice_path": workspace_dir / "artifacts" / "slices" / "task_time_slice.csv",
            "op_summary_slice_path": workspace_dir / "artifacts" / "slices" / "op_summary_slice.csv",
            "python_tracer_index_path": workspace_dir / "artifacts" / "stacks" / "python_tracer_index.json",
        }
        for artifact_key, expected_path in expected_artifacts.items():
            ensure_state_artifact(state, "profiling_preprocessor", artifact_key, expected_path)
        ensure(bool(state.get("flags", {}).get("slicing_done")), "profiling_preprocessor Step 1 结束后 slicing_done 必须为 true。")
        ensure(
            bool(state.get("flags", {}).get("python_tracer_index_built")),
            "profiling_preprocessor Step 1 结束后 python_tracer_index_built 必须为 true。",
        )
    elif current_step == 2:
        ensure_state_artifact(
            state,
            "profiling_preprocessor",
            "timeline_index_path",
            workspace_dir / "artifacts" / "index" / "timeline_index.json",
        )
        ensure(
            bool(state.get("flags", {}).get("timeline_index_built")),
            "profiling_preprocessor Step 2 结束后 timeline_index_built 必须为 true。",
        )
    else:
        raise ValueError(f"profiling_preprocessor 不支持的 step: {current_step}")


def validate_artifact_renderer_outputs(workspace_dir: Path, state: dict[str, Any]) -> None:
    ensure_state_artifact(
        state,
        "artifact_renderer",
        "graph_operator_spans_path",
        workspace_dir / "artifacts" / "graph" / "graph_operator_spans.json",
    )
    ensure_state_artifact(
        state,
        "artifact_renderer",
        "span_code_mapping_path",
        workspace_dir / "artifacts" / "mapping" / "span_code_mapping.json",
    )
    ensure_state_artifact(
        state,
        "artifact_renderer",
        "annotated_trace_path",
        workspace_dir / "output" / "trace_view.annotated.json",
    )
    ensure_state_artifact(
        state,
        "artifact_renderer",
        "stream_span_timeline_path",
        workspace_dir / "artifacts" / "timeline" / "stream_span_timeline.json",
    )

    flags = state.get("flags", {})
    ensure(bool(flags.get("span_mapping_done")), "artifact_renderer 结束后 span_mapping_done 必须为 true。")
    ensure(bool(flags.get("annotated_trace_generated")), "artifact_renderer 结束后 annotated_trace_generated 必须为 true。")
    ensure(bool(flags.get("timeline_generated")), "artifact_renderer 结束后 timeline_generated 必须为 true。")


def apply_graph_review_promotion(workspace_dir: Path, state: dict[str, Any], payload: dict[str, Any]) -> None:
    status = str(payload.get("status", "")).strip()
    if status not in {"passed", "partial"}:
        return
    review_outcome = str(payload.get("review_outcome", "")).strip()
    if (status == "passed" and review_outcome != "approved") or (status == "partial" and review_outcome != "blocked"):
        return

    artifacts = state.get("artifacts", {})
    graph_plan_path = Path(artifacts["graph_execution_plan_path"])
    graph_forward_context_path = Path(artifacts["graph_forward_context_path"])
    graph_operator_spans_path = Path(artifacts["graph_operator_spans_path"])
    graph_plan = load_json(graph_plan_path)
    graph_forward_context = load_json(graph_forward_context_path)
    graph_operator_spans = load_json(graph_operator_spans_path)

    promotion = payload.get("artifact_promotion", {})
    graph_plan_updates = dict(promotion.get("graph_execution_plan_updates", {}) or {})
    graph_forward_context_updates = dict(promotion.get("graph_forward_context_updates", {}) or {})
    graph_span_candidates_payload = dict(promotion.get("graph_span_candidates_payload", {}) or {})
    forward_segment_template_payload = dict(promotion.get("forward_segment_template_payload", {}) or {})
    graph_span_alignment_payload = dict(promotion.get("graph_span_alignment_payload", {}) or {})

    if status == "passed" and not graph_plan_updates:
        for key in [
            "mode",
            "mapping_granularity",
            "identified_graph_span_ids",
            "precise_span_mappings",
            "mapping_limitations",
            "warnings",
            "summary",
        ]:
            if key in payload:
                graph_plan_updates[key] = payload[key]
        graph_plan_updates.setdefault("status", "passed")

    if status == "passed" and not graph_forward_context_updates:
        graph_forward_context_updates = {
            "status": graph_plan_updates.get("status", "passed"),
            "mapping_granularity": graph_plan_updates.get(
                "mapping_granularity",
                str(payload.get("reviewed_mapping_granularity", "")).strip(),
            ),
        }
        if "mapping_limitations" in graph_plan_updates:
            graph_forward_context_updates["mapping_limitations"] = graph_plan_updates["mapping_limitations"]

    review_meta = {
        "promoted_from": str(workspace_dir / "output" / "graph_review_result.json"),
        "review_status": str(payload.get("status", "")).strip(),
        "review_outcome": str(payload.get("review_outcome", "")).strip(),
        "reviewed_mapping_granularity": str(payload.get("reviewed_mapping_granularity", "")).strip(),
    }
    graph_operator_spans["review_metadata"] = review_meta

    dump_json(graph_operator_spans_path, graph_operator_spans)

    graph_span_candidates_path = workspace_dir / "artifacts" / "graph" / "graph_span_candidates.json"
    forward_segment_template_path = workspace_dir / "artifacts" / "graph" / "forward_segment_template.json"
    dump_json(graph_span_candidates_path, graph_span_candidates_payload)
    dump_json(forward_segment_template_path, forward_segment_template_payload)

    artifacts["graph_span_candidates_path"] = str(graph_span_candidates_path)
    artifacts["forward_segment_template_path"] = str(forward_segment_template_path)
    artifacts["graph_operator_spans_path"] = str(graph_operator_spans_path)
    flags = state.setdefault("flags", {})
    flags["graph_operator_spans_built"] = True
    flags["graph_span_identified"] = True
    flags["forward_segment_template_built"] = True

    if status == "passed":
        graph_plan.update(graph_plan_updates)
        graph_forward_context.update(graph_forward_context_updates)
        graph_plan["review_metadata"] = review_meta
        graph_forward_context["review_metadata"] = review_meta
        dump_json(graph_plan_path, graph_plan)
        dump_json(graph_forward_context_path, graph_forward_context)
        graph_span_alignment_path = workspace_dir / "artifacts" / "graph" / "graph_span_alignment.json"
        dump_json(graph_span_alignment_path, graph_span_alignment_payload)
        artifacts["graph_span_alignment_path"] = str(graph_span_alignment_path)
        flags["graph_span_alignment_built"] = True
    else:
        flags["graph_span_alignment_built"] = False
        artifacts.pop("graph_span_alignment_path", None)
    save_state(workspace_dir, state)


def apply_stack_mapping_promotion(workspace_dir: Path, state: dict[str, Any], payload: dict[str, Any]) -> None:
    status = str(payload.get("status", "")).strip()
    if status not in {"passed", "partial"}:
        return

    external_span_mapping_payload = dict(payload.get("external_span_mapping_payload", {}) or {})
    if not external_span_mapping_payload:
        return

    external_span_mapping_path = workspace_dir / "artifacts" / "mapping" / "external_span_mapping.json"
    dump_json(external_span_mapping_path, external_span_mapping_payload)
    state["artifacts"]["external_span_mapping_path"] = str(external_span_mapping_path)
    state.setdefault("flags", {})["external_span_mapping_built"] = True
    save_state(workspace_dir, state)


def apply_step4_bootstrap_promotion(workspace_dir: Path, state: dict[str, Any], payload: dict[str, Any]) -> None:
    status = str(payload.get("status", "")).strip()
    if status != "passed":
        return
    step4_bootstrap_result_path = workspace_dir / "output" / "step4_bootstrap_result.json"
    state["artifacts"]["step4_bootstrap_result_path"] = str(step4_bootstrap_result_path)
    save_state(workspace_dir, state)


def apply_graph_bootstrap_promotion(workspace_dir: Path, state: dict[str, Any], payload: dict[str, Any]) -> None:
    status = str(payload.get("status", "")).strip()
    if status != "passed":
        return
    graph_bootstrap_result_path = workspace_dir / "output" / "graph_bootstrap_result.json"
    state["artifacts"]["graph_bootstrap_result_path"] = str(graph_bootstrap_result_path)
    state["artifacts"]["graph_forward_context_path"] = str(workspace_dir / "artifacts" / "graph" / "graph_forward_context.json")
    state["artifacts"]["graph_seed_context_path"] = str(workspace_dir / "input" / "graph_seed_context.json")
    state["artifacts"]["graph_operator_spans_path"] = str(workspace_dir / "artifacts" / "graph" / "graph_operator_spans.json")
    flags = state.setdefault("flags", {})
    flags["graph_forward_context_built"] = True
    flags["graph_seed_context_built"] = True
    flags["graph_operator_spans_built"] = True
    save_state(workspace_dir, state)


def _resolve_workspace_relative_path(workspace_dir: Path, raw_path: str) -> Path:
    candidate = Path(str(raw_path).strip())
    if candidate.is_absolute():
        return candidate
    return workspace_dir / candidate


def validate_timeline_analysis_output(
    workspace_dir: Path,
    state: dict[str, Any],
    config: dict[str, Any],
    patch_payload: dict[str, Any],
) -> None:
    analysis_path = workspace_dir / "output" / "timeline_analysis.json"
    ensure(analysis_path.exists(), f"timeline_analyst 缺少 timeline_analysis.json: {analysis_path}")
    analysis_payload = load_json(analysis_path)
    analysis_schema_paths = [
        Path(state["skill_dir"]) / item
        for item in config.get("secondary_contract_schema_files", [])
        if str(item).strip()
    ]
    ensure(analysis_schema_paths, "timeline_analyst 缺少 secondary_contract_schema_files，无法校验 timeline_analysis 合同。")
    for schema_path in analysis_schema_paths:
        ensure(schema_path.exists() and schema_path.is_file(), f"timeline_analyst analysis schema 不存在: {schema_path}")
    ensure(str(analysis_payload.get("status", "")).strip() == "passed", "timeline_analysis.json.status 必须为 passed。")
    ensure(str(analysis_payload.get("source", "")).strip(), "timeline_analysis.json.source 不能为空。")
    base_artifacts = analysis_payload.get("base_artifacts", {})
    ensure(isinstance(base_artifacts, dict), "timeline_analysis.json.base_artifacts 必须是对象。")
    classified_base_path_raw = str(base_artifacts.get("classified_spans_base_path", "")).strip()
    scope_gate_base_path_raw = str(base_artifacts.get("scope_gate_result_base_path", "")).strip()
    ensure(classified_base_path_raw, "timeline_analysis.json.base_artifacts.classified_spans_base_path 不能为空。")
    ensure(scope_gate_base_path_raw, "timeline_analysis.json.base_artifacts.scope_gate_result_base_path 不能为空。")
    classified_base_path = _resolve_workspace_relative_path(workspace_dir, classified_base_path_raw)
    scope_gate_base_path = _resolve_workspace_relative_path(workspace_dir, scope_gate_base_path_raw)
    ensure(classified_base_path.exists(), f"timeline_analysis.json 指向的 classified_spans.base.json 不存在: {classified_base_path}")
    ensure(scope_gate_base_path.exists(), f"timeline_analysis.json 指向的 scope_gate_result.base.json 不存在: {scope_gate_base_path}")

    summary = analysis_payload.get("review_patch_summary", {})
    ensure(isinstance(summary, dict), "timeline_analysis.json.review_patch_summary 必须是对象。")
    ensure(isinstance(analysis_payload.get("mutation_summary", {}), dict), "timeline_analysis.json.mutation_summary 必须是对象。")
    streams = analysis_payload.get("streams", [])
    parallel_groups = analysis_payload.get("parallel_groups", [])
    notes = analysis_payload.get("notes", [])
    ensure(isinstance(streams, list), "timeline_analysis.json.streams 必须是列表。")
    ensure(isinstance(parallel_groups, list), "timeline_analysis.json.parallel_groups 必须是列表。")
    ensure(isinstance(notes, list), "timeline_analysis.json.notes 必须是列表。")
    ensure(all(isinstance(item, str) for item in notes), "timeline_analysis.json.notes 只能包含字符串。")
    ensure(
        int(summary.get("stream_update_count", -1)) == len(patch_payload.get("stream_updates", [])),
        "timeline_analysis.json.review_patch_summary.stream_update_count 与 patch 不一致。",
    )
    ensure(
        int(summary.get("span_update_count", -1)) == len(patch_payload.get("span_updates", [])),
        "timeline_analysis.json.review_patch_summary.span_update_count 与 patch 不一致。",
    )
    base_scope_gate_payload = load_json(scope_gate_base_path)
    runtime_control_violations = base_scope_gate_payload.get("violations", {}).get("unexpected_runtime_control_semantic", [])
    if isinstance(runtime_control_violations, list) and runtime_control_violations:
        notes_text = "\n".join(str(item) for item in notes)
        ensure(
            "runtime_control" in notes_text,
            "base scope gate 暴露出 runtime_control 进入 semantic 集合时，timeline_analysis.json.notes 必须显式解释。",
        )


def apply_timeline_review_promotion(
    workspace_dir: Path,
    state: dict[str, Any],
    config: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    validate_timeline_analysis_output(workspace_dir, state, config, payload)
    merge_result = merge_timeline_review_patch_for_workspace(workspace_dir)
    reviewed_classified_path = Path(merge_result["classified_spans_reviewed_path"])
    reviewed_scope_gate_path = Path(merge_result["scope_gate_result_reviewed_path"])
    ensure(reviewed_classified_path.exists(), f"缺少 reviewed classified 输出: {reviewed_classified_path}")
    ensure(reviewed_scope_gate_path.exists(), f"缺少 reviewed scope gate 输出: {reviewed_scope_gate_path}")
    reviewed_scope_gate = load_json(reviewed_scope_gate_path)
    ensure(
        str(reviewed_scope_gate.get("status", "")).strip() == "passed",
        "Step 3 reviewed scope gate 未通过，禁止 promotion。",
    )

    canonical_classified_path = workspace_dir / "artifacts" / "classification" / "classified_spans.json"
    canonical_scope_gate_path = workspace_dir / "output" / "scope_gate_result.json"
    dump_json(canonical_classified_path, load_json(reviewed_classified_path))
    dump_json(canonical_scope_gate_path, reviewed_scope_gate)

    flags = state.setdefault("flags", {})
    flags["classification_done"] = True
    flags["hardware_scope_classified"] = True
    flags["scope_gate_passed"] = True
    state["artifacts"]["classified_spans_path"] = str(canonical_classified_path)
    state["artifacts"]["scope_gate_result_path"] = str(canonical_scope_gate_path)
    save_state(workspace_dir, state)


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    audit_payload: dict[str, Any] = {
        "schema_version": "finalize_audit_v1",
        "agent_name": args.agent_name,
        "attempted_at": now_iso(),
        "step": int(state.get("current_step", 0) or 0),
        "status": "failed",
        "errors": [],
    }
    try:
        config = effective_agent_config(args.agent_name, int(state["current_step"]))
        dispatch_payload = _dispatch_payload(workspace_dir, state, args.agent_name)
        completion_payload = ensure_dispatch_completion_marker(workspace_dir, state, args.agent_name)
        validate_wrapper_terminal_state(workspace_dir, args.agent_name, int(config["step"]), completion_payload)
        output_files = resolve_workspace_paths(workspace_dir, config["output_files"])
        for path in output_files:
            ensure(path.exists(), f"{args.agent_name} 缺少正式输出文件: {path}")

        primary_output = output_files[0]
        if args.agent_name == "graph_path_analyst":
            normalize_graph_review_result_file(primary_output)
        primary_payload = load_json(primary_output)
        if args.agent_name == "stack_mapper" and enrich_stack_mapper_quality_signals(primary_payload):
            dump_json(primary_output, primary_payload)
        validate_primary_payload(args.agent_name, primary_payload)
        status = str(primary_payload.get("status", "")).strip()
        ensure(status in config["allowed_status"], f"{args.agent_name} 输出状态 `{status}` 不在允许集合 {sorted(config['allowed_status'])}。")
        ensure_no_new_temp_scripts(workspace_dir, state, args.agent_name)
        if args.agent_name == "profiling_preprocessor":
            validate_profiling_preprocessor_outputs(workspace_dir, state, int(config["step"]))
        elif args.agent_name == "timeline_analyst":
            apply_timeline_review_promotion(workspace_dir, state, config, primary_payload)
        elif args.agent_name == "step4_bootstrap_runner":
            apply_step4_bootstrap_promotion(workspace_dir, state, primary_payload)
        elif args.agent_name == "graph_bootstrap_runner":
            apply_graph_bootstrap_promotion(workspace_dir, state, primary_payload)
        elif args.agent_name == "stack_mapper":
            validate_stack_mapper_target_scope(state, primary_payload)
            validate_stack_mapper_evidence_consistency(workspace_dir, state, primary_payload)
            validate_stack_mapper_location_quality(primary_payload)
            validate_stack_mapper_semantic_collapse_quality(primary_payload)
            validate_stack_mapper_communication_downgrade(primary_payload)
            apply_stack_mapping_promotion(workspace_dir, state, primary_payload)
        elif args.agent_name == "graph_path_analyst":
            validate_graph_path_target_scope(state, primary_payload)
            validate_graph_path_knowledge_checks(primary_payload)
            validate_graph_path_repo_file_evidence(state, primary_payload)
            validate_graph_path_template_evidence(primary_payload)
            validate_graph_path_location_quality(state, primary_payload)
            apply_graph_review_promotion(workspace_dir, state, primary_payload)
        elif args.agent_name == "artifact_renderer":
            validate_artifact_renderer_outputs(workspace_dir, state)

        query_snapshot = state.get("agents", {}).get(args.agent_name, {}).get("last_query_snapshot", "")
        ensure(query_snapshot, f"{args.agent_name} 缺少 last_query_snapshot，需先执行 prepare_agent_dispatch.py。")
        record_agent_status(
            workspace_dir,
            args.agent_name,
            status,
            query_snapshot,
            str(primary_output),
        )
        state = load_state(workspace_dir)
        agent_slot = state.setdefault("agents", {}).setdefault(args.agent_name, {})
        agent_slot["last_completion_marker_path"] = str(dispatch_payload.get("completion_marker_path", ""))
        agent_slot["last_completion_recorded_at"] = str(completion_payload.get("completed_at", "")).strip()
        update_finalize_provenance(workspace_dir, state, args.agent_name, output_files)
        orchestration = state.setdefault("orchestration", {})
        orchestration["mode"] = "main_agent_with_subagents"
        orchestration["active_agent"] = ""
        orchestration["active_dispatch_path"] = ""
        orchestration["active_dispatch_id"] = ""
        orchestration["active_completion_marker_path"] = ""
        orchestration["dispatch_temp_script_baseline"] = []
        orchestration["last_finalize_record_path"] = ""
        state["last_finalize_agent"] = args.agent_name
        state["last_finalize_at"] = now_iso()
        if args.agent_name == "artifact_validator" and int(state.get("current_step", 0) or 0) == 7:
            state["next_action"] = "run_final_gate"
        else:
            state["next_action"] = f"run_step_{state['current_step']}"

        audit_payload.update(
            {
                "status": "passed",
                "dispatch_id": str(dispatch_payload.get("dispatch_id", "")).strip(),
                "completion_marker_path": str(dispatch_payload.get("completion_marker_path", "")).strip(),
                "query_snapshot_path": query_snapshot,
                "query_snapshot_sha256": str(dispatch_payload.get("query_snapshot_sha256", "")).strip(),
                "task_call_id": str(completion_payload.get("task_call_id", "")).strip(),
                "subagent_id": str(completion_payload.get("subagent_id", "")).strip(),
                "allowed_official_scripts": list(dispatch_payload.get("allowed_official_scripts", [])),
                "primary_output_path": str(primary_output),
                "output_hashes": collect_existing_file_hashes(
                    {f"output:{path.name}": path for path in output_files if path.exists() and path.is_file()}
                ),
                "artifact_hashes": dict(orchestration.get("last_finalize_hashes", {})),
                "final_status": status,
            }
        )
        audit_path = write_finalize_audit_record(workspace_dir, args.agent_name, audit_payload)
        orchestration["last_finalize_record_path"] = str(audit_path)
        state["last_finalize_record_path"] = str(audit_path)
        save_state(workspace_dir, state)
        return 0
    except Exception as exc:
        failure_state = load_state(workspace_dir)
        orchestration = failure_state.setdefault("orchestration", {})
        write_error_context(
            workspace_dir,
            {
                "task_type": "debug_failure",
                "failed_step": int(failure_state.get("current_step", 0) or 0),
                "failed_component": "finalize_agent_dispatch.py",
                "error_type": "finalize_failed",
                "error_message": str(exc),
                "related_files": [str(path) for path in output_files if path.exists()] if "output_files" in locals() else [],
                "previous_fixes": [],
            },
        )
        failure_state["status"] = "blocked"
        failure_state["next_action"] = "call_profiling_debugger"
        failure_state.setdefault("flags", {})["debug_fix_pending"] = True
        orchestration["active_agent"] = ""
        orchestration["active_dispatch_path"] = ""
        orchestration["active_dispatch_id"] = ""
        orchestration["active_completion_marker_path"] = ""
        orchestration["dispatch_temp_script_baseline"] = []
        orchestration["last_provenance_error"] = str(exc)
        orchestration["provenance_verified"] = False
        audit_payload["errors"] = [str(exc)]
        audit_path = write_finalize_audit_record(workspace_dir, args.agent_name, audit_payload)
        orchestration["last_finalize_record_path"] = str(audit_path)
        failure_state["last_finalize_record_path"] = str(audit_path)
        save_state(workspace_dir, failure_state)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
