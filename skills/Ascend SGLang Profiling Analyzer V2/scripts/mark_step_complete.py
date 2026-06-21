from __future__ import annotations

import argparse
from pathlib import Path

from workflow_common import (
    compute_sha256,
    document_hashes,
    load_json,
    load_provenance_manifest,
    load_state,
    now_iso,
    required_step,
    save_state,
    write_error_context,
)


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="标记某个 step 已完成。")
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument("--step", required=True, type=int)
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def ensure_changed(workspace_dir: Path, state: dict, required_task_plan: bool) -> None:
    current_hashes = document_hashes(workspace_dir)
    baseline = state.get("document_hashes_baseline", {})
    ensure(current_hashes.get("findings.md") != baseline.get("findings.md") or current_hashes.get("progress.md") != baseline.get("progress.md"), "findings.md 或 progress.md 需要至少一个发生变化。")
    if required_task_plan:
        ensure(current_hashes.get("task_plan.md") != baseline.get("task_plan.md"), "task_plan.md 需要更新。")


def ensure_artifact(path_str: str, label: str) -> None:
    ensure(bool(path_str), f"{label} 未写入 state.artifacts。")
    ensure(Path(path_str).exists(), f"{label} 不存在: {path_str}")


def ensure_non_empty_rows_artifact(path_str: str, label: str) -> None:
    ensure_artifact(path_str, label)
    payload = load_json(Path(path_str))
    rows = payload.get("rows", [])
    ensure(isinstance(rows, list), f"{label}.rows 必须是列表。")
    ensure(rows, f"{label}.rows 不能为空；空 formal target / alignment 工件不得推进 step。")


def extract_graph_alignment_items(payload: dict) -> list[dict]:
    for key in ("items", "rows"):
        value = payload.get(key, [])
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def collect_step5_step6_readiness_issues(state: dict) -> list[str]:
    artifacts = state.get("artifacts", {})
    graph_plan = load_json(Path(artifacts["graph_execution_plan_path"]))
    graph_forward_context = load_json(Path(artifacts["graph_forward_context_path"]))
    graph_mapping_targets = load_json(Path(artifacts["graph_mapping_targets_path"]))
    graph_operator_spans = load_json(Path(artifacts["graph_operator_spans_path"]))
    graph_span_alignment = load_json(Path(artifacts["graph_span_alignment_path"]))

    graph_mapping_target_rows = graph_mapping_targets.get("rows", [])
    frozen_graph_span_ids = {
        str(row.get("span_id", "")).strip()
        for row in graph_mapping_target_rows
        if isinstance(row, dict) and str(row.get("span_id", "")).strip()
    }
    operator_span_rows = graph_operator_spans.get("rows", [])
    operator_span_ids = {
        str(row.get("graph_operator_span_id", "")).strip()
        for row in operator_span_rows
        if isinstance(row, dict) and str(row.get("graph_operator_span_id", "")).strip()
    }
    alignment_items = extract_graph_alignment_items(graph_span_alignment)
    alignment_span_ids = {
        str(item.get("span_id", "")).strip()
        for item in alignment_items
        if str(item.get("span_id", "")).strip()
    }

    issues: list[str] = []
    if graph_plan.get("mapping_granularity") != "per_span_forward_code":
        issues.append("graph_execution_plan.mapping_granularity 仍不是 per_span_forward_code。")
    if graph_forward_context.get("mapping_granularity") != "per_span_forward_code":
        issues.append("graph_forward_context.mapping_granularity 仍不是 per_span_forward_code。")
    if not frozen_graph_span_ids:
        issues.append("graph_mapping_targets.json 未提供任何正式 formal graph target。")
    if not operator_span_ids:
        issues.append("graph_operator_spans.json 缺少正式 operator spans。")
    if not alignment_items:
        issues.append("graph_span_alignment.json 缺少可消费 items/rows。")
    missing_frozen_graph_span_ids = sorted(frozen_graph_span_ids - alignment_span_ids)
    if missing_frozen_graph_span_ids:
        issues.append(
            "graph_span_alignment 未覆盖全部 formal graph target spans: "
            + ", ".join(missing_frozen_graph_span_ids[:10])
        )
    for index, item in enumerate(alignment_items):
        row_path = f"graph_span_alignment[{index}]"
        span_id = str(item.get("span_id", "")).strip()
        graph_operator_span_id = str(item.get("graph_operator_span_id", "")).strip()
        location_kind = str(item.get("location_kind", "")).strip()
        operator_evidence_kind = str(item.get("operator_evidence_kind", "")).strip()
        requires_further_drilldown = item.get("requires_further_drilldown")
        if not span_id:
            issues.append(f"{row_path} 缺少 span_id。")
        elif span_id not in frozen_graph_span_ids:
            issues.append(f"{row_path}.span_id={span_id} 超出 graph_mapping_targets formal target 范围。")
        if not graph_operator_span_id:
            issues.append(f"{row_path} 缺少 graph_operator_span_id。")
        elif graph_operator_span_id not in operator_span_ids:
            issues.append(f"{row_path}.graph_operator_span_id={graph_operator_span_id} 无法回溯到 graph_operator_spans.json。")
        if location_kind != "operator_call":
            issues.append(f"{row_path}.location_kind 不是 operator_call: {location_kind or '<missing>'}")
        if requires_further_drilldown is not False:
            issues.append(f"{row_path}.requires_further_drilldown 必须为 false，当前为 {requires_further_drilldown!r}")
        if operator_evidence_kind not in ALLOWED_OPERATOR_EVIDENCE_KINDS:
            issues.append(f"{row_path}.operator_evidence_kind 非法或缺失: {operator_evidence_kind or '<missing>'}")
    return issues


def fail_step5_completion(workspace_dir: Path, state: dict, issues: list[str]) -> int:
    write_error_context(
        workspace_dir,
        {
            "task_type": "debug_failure",
            "failed_step": 5,
            "failed_component": "mark_step_complete.py",
            "error_type": "step5_not_ready_for_step6",
            "error_message": "; ".join(issues[:20]),
            "related_files": [
                state.get("artifacts", {}).get("graph_execution_plan_path", ""),
                state.get("artifacts", {}).get("graph_mapping_targets_path", ""),
                state.get("artifacts", {}).get("graph_forward_context_path", ""),
                state.get("artifacts", {}).get("graph_operator_spans_path", ""),
                state.get("artifacts", {}).get("graph_span_alignment_path", ""),
                state.get("agents", {}).get("graph_path_analyst", {}).get("last_output_path", ""),
            ],
            "timestamp": now_iso(),
        },
    )
    state["status"] = "blocked"
    state["current_step"] = 5
    state["next_action"] = "call_profiling_debugger"
    state.setdefault("flags", {})["debug_fix_pending"] = True
    save_state(workspace_dir, state)
    return 1


def ensure_agent_status(workspace_dir: Path, state: dict, agent_name: str, allowed_status: set[str]) -> None:
    agent_state = state["agents"].get(agent_name, {})
    ensure(agent_state.get("last_status") in allowed_status, f"{agent_name} 状态不满足要求: {agent_state.get('last_status')}")
    output_path = agent_state.get("last_output_path", "")
    ensure(output_path, f"{agent_name} 缺少 last_output_path。")
    ensure(Path(output_path).exists(), f"{agent_name} 输出文件不存在: {output_path}")
    query_snapshot = agent_state.get("last_query_snapshot", "")
    ensure(query_snapshot and Path(query_snapshot).exists(), f"{agent_name} query 快照不存在。")
    index_jsonl = workspace_dir / "logs" / "agent_calls" / "index.jsonl"
    ensure(index_jsonl.exists(), "缺少 agent 调用审计索引 index.jsonl。")


def ensure_state_consistency(workspace_dir: Path, state: dict, step: int) -> None:
    flags = state.get("flags", {})
    orchestration = state.get("orchestration", {})
    expected_finalize_agent = {
        1: "profiling_preprocessor",
        2: "profiling_preprocessor",
        3: "timeline_analyst",
        4: "stack_mapper",
        5: "graph_path_analyst",
        6: "artifact_renderer",
        7: "artifact_validator",
    }.get(step, "")
    if expected_finalize_agent:
        ensure(
            str(orchestration.get("last_finalize_agent", "")).strip() == expected_finalize_agent,
            f"Step {step} 完成前 last_finalize_agent 必须为 {expected_finalize_agent}。",
        )
        finalize_record_path = str(orchestration.get("last_finalize_record_path", "")).strip()
        ensure(finalize_record_path, f"Step {step} 完成前缺少 last_finalize_record_path。")
        ensure(Path(finalize_record_path).exists(), f"Step {step} 的 finalize 审计文件不存在: {finalize_record_path}")
    if step == 7:
        validation_path = str(state.get("artifacts", {}).get("validation_result_path", "")).strip()
        ensure(validation_path, "Step 7 缺少 validation_result_path。")
        validation = load_json(Path(validation_path))
        ensure(
            bool(flags.get("validation_passed")) == (validation.get("status") == "passed"),
            "state.flags.validation_passed 与 validation_result.json.status 不一致。",
        )
    if state.get("status") in {"passed", "completed", "awaiting_final_gate"}:
        ensure(not bool(flags.get("debug_fix_pending")), "完成态前不得残留 debug_fix_pending=true。")
    if step >= 6 and state.get("status") in {"passed", "completed", "awaiting_final_gate"}:
        ensure(not bool(flags.get("has_unresolved_semantic_spans")), "完成态前不得残留 has_unresolved_semantic_spans=true。")
    ensure(not bool(orchestration.get("illegal_temp_script_detected")), "检测到非法临时脚本污染，禁止推进 step。")


def ensure_provenance_consistency(workspace_dir: Path, state: dict, step: int) -> None:
    manifest = load_provenance_manifest(workspace_dir)
    artifacts_manifest = manifest.get("artifacts", {})
    artifacts = state.get("artifacts", {})
    required_keys_by_step = {
        1: ["trace_slice_path", "kernel_slice_path", "operator_slice_path", "task_time_slice_path", "op_summary_slice_path", "python_tracer_index_path"],
        2: ["timeline_index_path"],
        3: ["classified_spans_path"],
        4: ["stack_evidence_path", "stack_evidence_lite_path", "stack_call_paths_path", "external_mapping_targets_path", "external_span_mapping_path", "graph_phase_stack_evidence_path"],
        5: ["graph_execution_plan_path", "graph_mapping_targets_path", "graph_forward_context_path", "graph_operator_spans_path", "graph_span_candidates_path", "forward_segment_template_path", "graph_span_alignment_path"],
        6: ["span_code_mapping_path", "annotated_trace_path", "stream_span_timeline_path"],
        7: ["validation_result_path"],
    }
    for artifact_key in required_keys_by_step.get(step, []):
        raw_path = str(artifacts.get(artifact_key, "")).strip()
        ensure(raw_path, f"{artifact_key} 缺少正式路径，无法校验 provenance。")
        path = Path(raw_path)
        ensure(path.exists(), f"{artifact_key} 正式文件不存在: {path}")
        label = f"artifact:{artifact_key}"
        entry = artifacts_manifest.get(label)
        ensure(isinstance(entry, dict), f"workspace_provenance.json 缺少 {label}。")
        ensure(str(entry.get("path", "")).strip() == str(path), f"{label} 的 provenance path 与当前 state 不一致。")
        ensure(str(entry.get("sha256", "")).strip() == compute_sha256(path), f"{label} 在 finalize 之后发生了修改。")


def step_requirements(workspace_dir: Path, state: dict, step: int) -> None:
    flags = state["flags"]
    artifacts = state["artifacts"]
    output_dir = workspace_dir / "output"
    if step == 1:
        ensure(
            flags["input_contract_valid"]
            and flags["raw_profiling_discovered"]
            and flags["slicing_done"]
            and flags["python_tracer_index_built"],
            "Step 1 flags 不完整。",
        )
        for key in ["trace_slice_path", "kernel_slice_path", "operator_slice_path", "task_time_slice_path", "op_summary_slice_path"]:
            ensure_artifact(artifacts[key], key)
        ensure((output_dir / "preprocess_step1_result.json").exists(), "缺少 preprocess_step1_result.json。")
        ensure_agent_status(workspace_dir, state, "profiling_preprocessor", {"passed"})
    elif step == 2:
        ensure(flags["timeline_index_built"], "Step 2 需要 timeline_index_built=true。")
        ensure_artifact(artifacts["timeline_index_path"], "timeline_index_path")
        ensure((output_dir / "preprocess_step2_result.json").exists(), "缺少 preprocess_step2_result.json。")
        ensure_agent_status(workspace_dir, state, "profiling_preprocessor", {"passed"})
    elif step == 3:
        ensure(flags["classification_done"], "Step 3 需要 classification_done=true。")
        ensure_artifact(artifacts["classified_spans_path"], "classified_spans_path")
        ensure((output_dir / "timeline_analysis.json").exists(), "缺少 timeline_analysis.json。")
        ensure_agent_status(workspace_dir, state, "timeline_analyst", {"passed"})
    elif step == 4:
        ensure(flags["stack_evidence_built"], "Step 4 需要 stack_evidence_built=true。")
        ensure(flags["stack_call_paths_built"], "Step 4 需要 stack_call_paths_built=true。")
        ensure(flags["external_mapping_targets_built"], "Step 4 需要 external_mapping_targets_built=true。")
        ensure(flags["external_span_mapping_built"], "Step 4 需要 external_span_mapping_built=true。")
        ensure(flags["graph_phase_stack_evidence_built"], "Step 4 需要 graph_phase_stack_evidence_built=true。")
        ensure_artifact(artifacts["stack_evidence_path"], "stack_evidence_path")
        ensure_artifact(artifacts["stack_call_paths_path"], "stack_call_paths_path")
        ensure_artifact(artifacts["external_mapping_targets_path"], "external_mapping_targets_path")
        ensure_artifact(artifacts["external_span_mapping_path"], "external_span_mapping_path")
        ensure_artifact(artifacts["graph_phase_stack_evidence_path"], "graph_phase_stack_evidence_path")
        ensure((output_dir / "stack_mapping_result.json").exists(), "缺少 stack_mapping_result.json。")
        ensure_agent_status(workspace_dir, state, "stack_mapper", {"passed", "partial"})
    elif step == 5:
        ensure(flags["graph_path_built"], "Step 5 需要 graph_path_built=true。")
        ensure(flags.get("graph_mapping_targets_built"), "Step 5 需要 graph_mapping_targets_built=true。")
        ensure(flags["graph_forward_context_built"], "Step 5 需要 graph_forward_context_built=true。")
        ensure(flags["graph_operator_spans_built"], "Step 5 需要 graph_operator_spans_built=true。")
        ensure(flags["graph_span_identified"], "Step 5 需要 graph_span_identified=true。")
        ensure(flags["forward_segment_template_built"], "Step 5 需要 forward_segment_template_built=true。")
        ensure(flags["graph_span_alignment_built"], "Step 5 需要 graph_span_alignment_built=true。")
        ensure_artifact(artifacts["graph_execution_plan_path"], "graph_execution_plan_path")
        ensure_artifact(artifacts["graph_mapping_targets_path"], "graph_mapping_targets_path")
        ensure_artifact(artifacts["graph_forward_context_path"], "graph_forward_context_path")
        ensure_artifact(artifacts["graph_operator_spans_path"], "graph_operator_spans_path")
        ensure_artifact(artifacts["graph_span_candidates_path"], "graph_span_candidates_path")
        ensure_artifact(artifacts["forward_segment_template_path"], "forward_segment_template_path")
        ensure_artifact(artifacts["graph_span_alignment_path"], "graph_span_alignment_path")
        ensure_non_empty_rows_artifact(artifacts["graph_mapping_targets_path"], "graph_mapping_targets_path")
        ensure_non_empty_rows_artifact(artifacts["graph_operator_spans_path"], "graph_operator_spans_path")
        ensure_non_empty_rows_artifact(artifacts["graph_span_alignment_path"], "graph_span_alignment_path")
        ensure_agent_status(workspace_dir, state, "graph_path_analyst", {"passed", "partial"})
    elif step == 6:
        ensure(flags["span_mapping_done"] and flags["annotated_trace_generated"] and flags["timeline_generated"], "Step 6 输出 flags 不完整。")
        for key in ["span_code_mapping_path", "annotated_trace_path", "stream_span_timeline_path"]:
            ensure_artifact(artifacts[key], key)
        ensure((output_dir / "render_result.json").exists(), "缺少 render_result.json。")
        ensure_agent_status(workspace_dir, state, "artifact_renderer", {"passed"})
    elif step == 7:
        ensure_artifact(artifacts["validation_result_path"], "validation_result_path")
        validation = load_json(Path(artifacts["validation_result_path"]))
        validation_status = str(validation.get("status", "")).strip()
        ensure(validation_status in {"passed", "failed"}, "validation_result.json.status 必须为 passed 或 failed。")
        ensure_agent_status(workspace_dir, state, "artifact_validator", {"passed", "failed"})
    else:
        raise ValueError(f"不支持的 step: {step}")


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    ensure(state["current_step"] == args.step, f"当前 current_step={state['current_step']}，不是 {args.step}")
    required_step(args.step)
    required_task_plan = bool(state.get("flags", {}).get("task_plan_refresh_required", False))
    ensure_changed(workspace_dir, state, required_task_plan=required_task_plan)
    step_requirements(workspace_dir, state, args.step)
    ensure_state_consistency(workspace_dir, state, args.step)
    ensure_provenance_consistency(workspace_dir, state, args.step)
    if args.step == 5:
        graph_path_status = str(state.get("agents", {}).get("graph_path_analyst", {}).get("last_status", "")).strip()
        readiness_issues = collect_step5_step6_readiness_issues(state)
        if graph_path_status != "passed":
            readiness_issues.insert(
                0,
                f"graph_path_analyst 当前 status={graph_path_status!r}；partial 允许正式审计与 artifact promotion，但不允许 mark Step 5 complete 并推进到 Step 6。",
            )
        if readiness_issues:
            return fail_step5_completion(workspace_dir, state, readiness_issues)

    state["last_completed_step"] = args.step
    state["current_step"] = args.step + 1 if args.step < 7 else 7
    state["next_action"] = "run_final_gate" if args.step == 7 else f"run_step_{args.step + 1}"
    state["status"] = "awaiting_final_gate" if args.step == 7 else "in_progress"
    state.setdefault("step_history", []).append(
        {
            "step": args.step,
            "name": STEP_NAME(args.step),
            "status": "completed",
            "at": state["updated_at"],
            "finalize_agent": str(state.get("orchestration", {}).get("last_finalize_agent", "")).strip(),
            "finalize_record_path": str(state.get("orchestration", {}).get("last_finalize_record_path", "")).strip(),
        }
    )
    state["document_hashes_baseline"] = document_hashes(workspace_dir)
    save_state(workspace_dir, state)
    return 0


def STEP_NAME(step: int) -> str:
    return required_step(step)


if __name__ == "__main__":
    raise SystemExit(main())
