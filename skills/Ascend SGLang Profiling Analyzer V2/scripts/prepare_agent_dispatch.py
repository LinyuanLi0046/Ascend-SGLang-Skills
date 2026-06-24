from __future__ import annotations

import argparse
from pathlib import Path

from agent_contracts import AGENT_CONFIG, effective_agent_config, resolve_workspace_paths
from build_agent_query import build_query_bundle, write_query_artifacts
from build_runtime_constraints import _select_existing_config_file
from discover_inputs import discover_inputs_for_workspace
from resolve_step1_inputs import resolve_inputs_for_workspace
from step4_bootstrap_plan import (
    TARGET_SCRIPT_SEQUENCE as STEP4_TARGET_SCRIPT_SEQUENCE,
    build_readiness_snapshot as build_step4_readiness_snapshot,
)
from step5_graph_bootstrap_plan import (
    TARGET_SCRIPT_SEQUENCE as STEP5_TARGET_SCRIPT_SEQUENCE,
    build_readiness_snapshot as build_step5_readiness_snapshot,
)
from write_agent_task_input import TASK_FILENAME_BY_AGENT, build_payload as build_task_input_payload
from workflow_common import (
    dispatch_completion_marker_path,
    dump_json,
    effective_substep,
    list_workspace_temp_scripts,
    load_json,
    load_state,
    now_iso,
    save_state,
    write_error_context,
)

VALID_SPEC_MODES = {"spec_v2", "decode_graph", "disabled"}
STEP4_BOOTSTRAP_SCRIPTS = [f"scripts/{item}" for item in STEP4_TARGET_SCRIPT_SEQUENCE["step4_stack_mapper"]]
STEP5_GRAPH_BOOTSTRAP_SCRIPTS = [f"scripts/{item}" for item in STEP5_TARGET_SCRIPT_SEQUENCE["step5_graph_path_analyst"]]
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="为主 agent 准备一次正式子 agent 调度。")
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument("--agent-name", required=True, choices=sorted(AGENT_CONFIG))
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def log(message: str) -> None:
    print(f"[prepare-dispatch] {message}", flush=True)


def require_state_artifact_path(state: dict[str, object], artifact_key: str, label: str) -> Path:
    raw_path = str(state.get("artifacts", {}).get(artifact_key, "")).strip()
    ensure(raw_path, f"缺少 state.artifacts.{artifact_key}，说明 {label} 未正确完成。")
    path = Path(raw_path)
    ensure(path.exists() and path.is_file(), f"{label} 不存在或不是文件: {path}")
    return path


def ensure_runtime_constraints_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    runtime_constraints_path = require_state_artifact_path(
        state,
        "runtime_constraints_path",
        "runtime_constraints.json",
    )
    runtime_constraints = load_json(runtime_constraints_path)
    spec_mode = str(runtime_constraints.get("spec_mode", "")).strip()
    ensure(
        spec_mode in VALID_SPEC_MODES,
        f"runtime_constraints.json 缺少有效 spec_mode，当前为 {spec_mode or '<missing>'}。",
    )
    ensure(bool(state.get("flags", {}).get("runtime_constraints_built")), "runtime_constraints_built 未置为 true。")


def ensure_step4_stack_evidence_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    require_state_artifact_path(state, "stack_evidence_path", "stack_evidence.json")
    require_state_artifact_path(state, "stack_evidence_lite_path", "stack_evidence_lite.json")
    ensure(bool(state.get("flags", {}).get("stack_evidence_built")), "stack_evidence_built 未置为 true。")


def ensure_step4_graph_phase_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    require_state_artifact_path(state, "graph_phase_stack_evidence_path", "graph_phase_stack_evidence.json")
    ensure(bool(state.get("flags", {}).get("graph_phase_stack_evidence_built")), "graph_phase_stack_evidence_built 未置为 true。")


def ensure_step4_graph_plan_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    require_state_artifact_path(state, "graph_execution_plan_path", "graph_execution_plan.json")


def ensure_step4_graph_mapping_targets_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    require_state_artifact_path(state, "graph_mapping_targets_path", "graph_mapping_targets.json")
    ensure(bool(state.get("flags", {}).get("graph_mapping_targets_built")), "graph_mapping_targets_built 未置为 true。")


def ensure_step4_external_targets_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    require_state_artifact_path(state, "external_mapping_targets_path", "external_mapping_targets.json")
    flags = state.get("flags", {})
    ensure(bool(flags.get("graph_phase_stack_evidence_built")), "graph_phase_stack_evidence_built 未置为 true。")
    ensure(bool(flags.get("graph_mapping_targets_built")), "graph_mapping_targets_built 未置为 true。")
    ensure(bool(flags.get("external_mapping_targets_built")), "external_mapping_targets_built 未置为 true。")


def ensure_step4_support_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    require_state_artifact_path(state, "stack_call_paths_path", "stack_call_paths.json")
    ensure(bool(state.get("flags", {}).get("stack_call_paths_built")), "stack_call_paths_built 未置为 true。")


def ensure_step5_graph_forward_context_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    require_state_artifact_path(state, "graph_forward_context_path", "graph_forward_context.json")
    ensure(bool(state.get("flags", {}).get("graph_forward_context_built")), "graph_forward_context_built 未置为 true。")


def ensure_step5_graph_seed_context_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    require_state_artifact_path(state, "graph_seed_context_path", "graph_seed_context.json")
    ensure(bool(state.get("flags", {}).get("graph_seed_context_built")), "graph_seed_context_built 未置为 true。")


def ensure_step5_graph_operator_spans_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    require_state_artifact_path(state, "graph_operator_spans_path", "graph_operator_spans.json")
    ensure(bool(state.get("flags", {}).get("graph_operator_spans_built")), "graph_operator_spans_built 未置为 true。")


def current_substep(state: dict) -> str:
    return effective_substep(state, int(state.get("current_step", 0) or 0))


def ensure_step4_agent_matches_substep(state: dict, agent_name: str) -> None:
    if int(state.get("current_step", 0) or 0) != 4:
        return
    substep = current_substep(state)
    if agent_name == "step4_bootstrap_runner":
        ensure(substep == "A", f"step4_bootstrap_runner 只能在 Step 4A 调度，当前 substep={substep or '<missing>'}。")
    if agent_name == "stack_mapper":
        ensure(substep == "B", f"stack_mapper 只能在 Step 4B 调度，当前 substep={substep or '<missing>'}。")


def ensure_step5_agent_matches_substep(state: dict, agent_name: str) -> None:
    if int(state.get("current_step", 0) or 0) != 5:
        return
    substep = current_substep(state)
    if agent_name == "graph_bootstrap_runner":
        ensure(substep == "A", f"graph_bootstrap_runner 只能在 Step 5A 调度，当前 substep={substep or '<missing>'}。")
    if agent_name == "graph_path_analyst":
        ensure(substep == "B", f"graph_path_analyst 只能在 Step 5B 调度，当前 substep={substep or '<missing>'}。")


def ensure_step4a_bootstrap_result_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    raw_path = str(state.get("artifacts", {}).get("step4_bootstrap_result_path", "")).strip()
    ensure(raw_path, "Step 4B 缺少 step4_bootstrap_result_path，说明 Step 4A 尚未 finalize。")
    result_path = Path(raw_path)
    ensure(result_path.exists() and result_path.is_file(), f"Step 4B 缺少 step4_bootstrap_result.json: {result_path}")
    payload = load_json(result_path)
    ensure(str(payload.get("status", "")).strip() == "passed", "Step 4B 要求 Step 4A step4_bootstrap_result.status=passed。")
    ensure(str(payload.get("bootstrap_target", "")).strip() == "step4_stack_mapper", "Step 4A bootstrap_target 非 step4_stack_mapper。")
    ensure(bool(payload.get("required_artifacts_ready")), "Step 4A step4_bootstrap_result.required_artifacts_ready 必须为 true。")
    ensure(bool(payload.get("required_flags_ready")), "Step 4A step4_bootstrap_result.required_flags_ready 必须为 true。")
    ready_summary = payload.get("ready_summary", {})
    ensure(isinstance(ready_summary, dict), "Step 4A step4_bootstrap_result.ready_summary 必须是对象。")
    ensure(bool(ready_summary.get("ready")), "Step 4A step4_bootstrap_result.ready_summary.ready 必须为 true。")


def maybe_bootstrap_step4_support(workspace_dir: Path, agent_name: str, current_step: int) -> None:
    if current_step != 4:
        return
    state = load_state(workspace_dir)
    substep = current_substep(state)
    if agent_name == "step4_bootstrap_runner":
        ensure(substep == "A", f"step4_bootstrap_runner 只能在 Step 4A 调度，当前 substep={substep or '<missing>'}。")
        return
    if agent_name != "stack_mapper":
        return
    ensure(substep == "B", f"stack_mapper 只能在 Step 4B 调度，当前 substep={substep or '<missing>'}。")
    ensure_step4b_stack_mapper_dispatch_ready(workspace_dir)


def ensure_step5a_graph_bootstrap_result_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    raw_path = str(state.get("artifacts", {}).get("graph_bootstrap_result_path", "")).strip()
    ensure(raw_path, "Step 5B 缺少 graph_bootstrap_result_path，说明 Step 5A 尚未 finalize。")
    result_path = Path(raw_path)
    ensure(result_path.exists() and result_path.is_file(), f"Step 5B 缺少 graph_bootstrap_result.json: {result_path}")
    payload = load_json(result_path)
    ensure(str(payload.get("status", "")).strip() == "passed", "Step 5B 要求 Step 5A graph_bootstrap_result.status=passed。")
    ensure(
        str(payload.get("bootstrap_target", "")).strip() == "step5_graph_path_analyst",
        "Step 5A bootstrap_target 非 step5_graph_path_analyst。",
    )
    ensure(bool(payload.get("required_artifacts_ready")), "Step 5A graph_bootstrap_result.required_artifacts_ready 必须为 true。")
    ensure(bool(payload.get("required_flags_ready")), "Step 5A graph_bootstrap_result.required_flags_ready 必须为 true。")
    ready_summary = payload.get("ready_summary", {})
    ensure(isinstance(ready_summary, dict), "Step 5A graph_bootstrap_result.ready_summary 必须是对象。")
    ensure(bool(ready_summary.get("ready")), "Step 5A graph_bootstrap_result.ready_summary.ready 必须为 true。")


def ensure_step4b_stack_mapper_dispatch_ready(workspace_dir: Path) -> None:
    ensure_step4a_bootstrap_result_ready(workspace_dir)
    readiness = build_step4_readiness_snapshot(load_state(workspace_dir), "step4_stack_mapper")
    ensure(
        bool(readiness.get("ready")),
        "Step 4B 要求 Step 4A bootstrap ready set 完整。"
        f" missing_artifacts={readiness.get('missing_artifacts', [])},"
        f" missing_flags={readiness.get('missing_flags', [])}",
    )


def ensure_step5a_graph_bootstrap_dispatch_ready(workspace_dir: Path) -> None:
    ensure_runtime_constraints_ready(workspace_dir)
    ensure_step4_stack_evidence_ready(workspace_dir)
    ensure_step4_graph_phase_ready(workspace_dir)
    ensure_step4_graph_plan_ready(workspace_dir)
    ensure_step4_graph_mapping_targets_ready(workspace_dir)
    ensure_step4_external_targets_ready(workspace_dir)


def ensure_step5b_graph_path_dispatch_ready(workspace_dir: Path, state: dict[str, Any] | None = None) -> None:
    current_state = state if state is not None else load_state(workspace_dir)
    inputs = current_state.get("inputs", {})
    artifacts = current_state.get("artifacts", {})
    flags = current_state.get("flags", {})
    ensure_step5a_graph_bootstrap_result_ready(workspace_dir)
    readiness_snapshot = build_step5_readiness_snapshot(load_state(workspace_dir), "step5_graph_path_analyst")
    ensure(
        bool(readiness_snapshot.get("ready")),
        "Step 5B 要求 Step 5A graph bootstrap ready set 完整。"
        f" missing_artifacts={readiness_snapshot.get('missing_artifacts', [])},"
        f" missing_flags={readiness_snapshot.get('missing_flags', [])}",
    )
    model_root_path = str(inputs.get("model_root_path", "")).strip()
    code_repo_path = str(inputs.get("code_repo_path", "")).strip()
    ensure(model_root_path, "Step 5 缺少 model_root_path，禁止调度 graph_path_analyst。")
    model_root = Path(model_root_path)
    code_repo = Path(code_repo_path) if code_repo_path else None
    ensure(model_root.exists() and model_root.is_dir(), f"Step 5 的 model_root_path 不存在或不是目录: {model_root}")
    if code_repo and code_repo.exists():
        try:
            ensure(
                model_root.resolve() != code_repo.resolve(),
                "Step 5 的 model_root_path 指向代码仓根目录而不是实际模型目录。",
            )
        except OSError:
            pass

    required_paths = {
        "runtime_constraints_path": "runtime_constraints.json",
        "graph_execution_plan_path": "graph_execution_plan.json",
        "graph_mapping_targets_path": "graph_mapping_targets.json",
        "graph_bootstrap_result_path": "graph_bootstrap_result.json",
        "graph_forward_context_path": "graph_forward_context.json",
        "graph_seed_context_path": "graph_seed_context.json",
        "graph_operator_spans_path": "graph_operator_spans.json",
    }
    resolved_paths: dict[str, Path] = {}
    for key, label in required_paths.items():
        raw_path = str(artifacts.get(key, "")).strip()
        ensure(raw_path, f"Step 5 缺少 state.artifacts.{key}，说明 {label} 未完成。")
        path = Path(raw_path)
        ensure(path.exists() and path.is_file(), f"Step 5 的 {label} 不存在或不是文件: {path}")
        resolved_paths[key] = path

    required_flags = {
        "runtime_constraints_built": "Step 5 需要 runtime_constraints_built=true。",
        "graph_mapping_targets_built": "Step 5 需要 graph_mapping_targets_built=true。",
        "graph_forward_context_built": "Step 5 需要 graph_forward_context_built=true。",
        "graph_seed_context_built": "Step 5 需要 graph_seed_context_built=true。",
        "graph_operator_spans_built": "Step 5 需要 graph_operator_spans_built=true。",
    }
    for flag_key, message in required_flags.items():
        ensure(bool(flags.get(flag_key)), message)

    runtime_constraints = load_json(resolved_paths["runtime_constraints_path"])
    graph_plan = load_json(resolved_paths["graph_execution_plan_path"])
    graph_mapping_targets = load_json(resolved_paths["graph_mapping_targets_path"])
    graph_forward_context = load_json(resolved_paths["graph_forward_context_path"])
    graph_operator_spans = load_json(resolved_paths["graph_operator_spans_path"])
    runtime_preconditions = runtime_constraints.get("step5_preconditions", {})
    primary_model_context = runtime_constraints.get("primary_model_context", {})
    primary_config_files = primary_model_context.get("config_files", [])
    ensure(
        isinstance(primary_config_files, list) and bool(primary_config_files),
        "Step 5 runtime_constraints 未解析出主模型 config/generation/tokenizer/quant 描述文件，禁止调度 graph_path_analyst。",
    )
    ensure(
        bool(str(primary_model_context.get("resolved_model_family", "")).strip()),
        "Step 5 runtime_constraints 未解析到主模型 resolved_model_family，禁止继续 graph 路径缩圈。",
    )
    quant_modes = {
        str(item).strip().lower()
        for item in runtime_constraints.get("quant_mode_candidates", [])
        if str(item).strip()
    }
    if "modelslim" in quant_modes:
        ensure(
            _select_existing_config_file(model_root, "quant_model_description.json") is not None,
            "Step 5 主模型量化模式包含 modelslim，但 model_root_path 下缺少 quant_model_description.json。",
        )
    if bool(runtime_preconditions.get("requires_draft_model")):
        draft_model_root_text = str(runtime_constraints.get("draft_model_root", "")).strip()
        draft_model_context = runtime_constraints.get("draft_model_context", {})
        draft_config_files = draft_model_context.get("config_files", [])
        ensure(
            bool(draft_model_root_text),
            "Step 5 当前 speculative/runtime 约束要求 draft 模型目录，但 runtime_constraints 未解析出 draft_model_root。",
        )
        draft_model_root = Path(draft_model_root_text)
        ensure(draft_model_root.exists() and draft_model_root.is_dir(), f"Step 5 draft_model_root 不存在或不是目录: {draft_model_root}")
        ensure(
            isinstance(draft_config_files, list) and bool(draft_config_files),
            "Step 5 当前 speculative/runtime 约束要求 draft 模型，但 runtime_constraints 未解析出 draft config 文件。",
        )
        ensure(
            bool(str(draft_model_context.get("resolved_model_family", "")).strip()),
            "Step 5 当前 speculative/runtime 约束要求 draft 模型，但 runtime_constraints 未解析到 draft resolved_model_family。",
        )
    ensure(
        bool(runtime_preconditions.get("ready")),
        "Step 5 运行前提未满足，禁止调度 graph_path_analyst: "
        + "; ".join([str(item) for item in runtime_preconditions.get("blockers", []) if str(item).strip()][:10]),
    )
    ensure(
        str(graph_plan.get("graph_mode", "")).strip() in {"speculative", "decode_only"},
        "Step 5 graph_execution_plan 未形成 speculative/decode_only 的正式 graph mode。",
    )
    ensure(bool(graph_plan.get("graph_groups")), "Step 5 graph_execution_plan 未识别到可下钻的 graph groups。")
    graph_mapping_target_rows = graph_mapping_targets.get("rows", [])
    ensure(
        isinstance(graph_mapping_target_rows, list) and bool(graph_mapping_target_rows),
        "Step 5 graph_mapping_targets 未识别到可下钻的正式 graph mapping targets。",
    )
    graph_operator_span_rows = graph_operator_spans.get("rows", [])
    ensure(
        isinstance(graph_operator_span_rows, list) and bool(graph_operator_span_rows),
        "Step 5 graph_operator_spans 未能围绕 formal graph target 生成正式 operator skeleton。",
    )
    reconstruction_readiness = graph_forward_context.get("path_reconstruction_readiness", {})
    ensure(
        bool(graph_forward_context.get("path_reconstruction_ready")),
        "Step 5 graph_forward_context 判定当前样例尚不具备真实路径下钻前提: "
        + "; ".join([str(item) for item in reconstruction_readiness.get("blockers", []) if str(item).strip()][:10]),
    )


def fail_dispatch(workspace_dir: Path, state: dict, agent_name: str, message: str) -> None:
    write_error_context(
        workspace_dir,
        {
            "task_type": "debug_failure",
            "failed_step": int(state.get("current_step", 0)),
            "failed_component": "prepare_agent_dispatch.py",
            "error_type": "dispatch_precondition_failed",
            "error_message": message,
            "related_files": [],
            "previous_fixes": [],
        },
    )
    state["status"] = "blocked"
    state["next_action"] = "call_profiling_debugger"
    state.setdefault("flags", {})["debug_fix_pending"] = True
    orchestration = state.setdefault("orchestration", {})
    orchestration["active_agent"] = ""
    orchestration["active_dispatch_path"] = ""
    orchestration["active_dispatch_id"] = ""
    orchestration["active_completion_marker_path"] = ""
    orchestration["dispatch_temp_script_baseline"] = []
    orchestration["last_provenance_error"] = message
    save_state(workspace_dir, state)
    raise ValueError(message)


def maybe_bootstrap_step1_inputs(workspace_dir: Path, agent_name: str, current_step: int) -> None:
    if agent_name != "profiling_preprocessor" or current_step != 1:
        return
    resolve_inputs_for_workspace(workspace_dir)
    required_inputs = [
        workspace_dir / "input" / "input_resolution.json",
        workspace_dir / "input" / "input_contract.json",
        workspace_dir / "input" / "source_inventory.json",
    ]
    if all(path.exists() for path in required_inputs):
        return
    discover_inputs_for_workspace(workspace_dir)


def maybe_bootstrap_agent_task_input(workspace_dir: Path, agent_name: str, current_step: int) -> None:
    if agent_name not in TASK_FILENAME_BY_AGENT:
        return
    task_filename = TASK_FILENAME_BY_AGENT[agent_name]
    task_path = workspace_dir / "input" / task_filename
    _, payload = build_task_input_payload(workspace_dir, agent_name)
    dump_json(task_path, payload)


def maybe_bootstrap_graph_step5_support(workspace_dir: Path, agent_name: str, current_step: int) -> None:
    if current_step != 5:
        return
    state = load_state(workspace_dir)
    substep = current_substep(state)
    if agent_name == "graph_bootstrap_runner":
        ensure(substep == "A", f"graph_bootstrap_runner 只能在 Step 5A 调度，当前 substep={substep or '<missing>'}。")
        ensure_step5a_graph_bootstrap_dispatch_ready(workspace_dir)
        return
    if agent_name != "graph_path_analyst":
        return
    ensure(substep == "B", f"graph_path_analyst 只能在 Step 5B 调度，当前 substep={substep or '<missing>'}。")
    ensure_step5b_graph_path_dispatch_ready(workspace_dir, state)


def validate_previous_finalize_closure(workspace_dir: Path, state: dict, agent_name: str) -> None:
    if str(state.get("status", "")).strip() == "blocked":
        return
    orchestration = state.get("orchestration", {})
    active_agent = str(orchestration.get("active_agent", "")).strip()
    ensure(not active_agent, f"当前仍有未收口的 active_agent={active_agent}，禁止继续 prepare {agent_name}。")
    if int(state.get("current_step", 0) or 0) <= 1:
        return
    last_finalize_agent = str(orchestration.get("last_finalize_agent", "")).strip()
    ensure(last_finalize_agent, f"step {state.get('current_step')} 调度前缺少 last_finalize_agent，说明上一步 finalize 未闭环。")
    last_finalize_path = str(orchestration.get("last_finalize_record_path", "")).strip()
    ensure(last_finalize_path, f"step {state.get('current_step')} 调度前缺少 last_finalize_record_path。")
    ensure(Path(last_finalize_path).exists(), f"上一步 finalize 审计文件不存在: {last_finalize_path}")


def validate_dispatch_preconditions(workspace_dir: Path, state: dict, agent_name: str, current_step: int) -> None:
    if agent_name == "step4_bootstrap_runner" and current_step == 4:
        if current_substep(state) != "A":
            fail_dispatch(
                workspace_dir,
                state,
                agent_name,
                f"step4_bootstrap_runner 只能在 Step 4A 调度，当前 substep={current_substep(state) or '<missing>'}。",
            )
        required_paths = {
            "classified_spans_path": "classified_spans.json",
            "timeline_analysis_path": str(workspace_dir / "output" / "timeline_analysis.json"),
        }
        classified_spans_path = str(state.get("artifacts", {}).get("classified_spans_path", "")).strip()
        if not classified_spans_path:
            fail_dispatch(workspace_dir, state, agent_name, "Step 4A 缺少 classified_spans_path。")
        if not Path(classified_spans_path).exists():
            fail_dispatch(workspace_dir, state, agent_name, f"Step 4A 的 classified_spans.json 不存在: {classified_spans_path}")
        timeline_analysis_path = Path(required_paths["timeline_analysis_path"])
        if not timeline_analysis_path.exists():
            fail_dispatch(workspace_dir, state, agent_name, f"Step 4A 缺少 timeline_analysis.json: {timeline_analysis_path}")
        return
    if agent_name == "stack_mapper" and current_step == 4:
        if current_substep(state) != "B":
            fail_dispatch(
                workspace_dir,
                state,
                agent_name,
                f"stack_mapper 只能在 Step 4B 调度，当前 substep={current_substep(state) or '<missing>'}。",
            )
        try:
            ensure_step4b_stack_mapper_dispatch_ready(workspace_dir)
        except Exception as exc:
            fail_dispatch(workspace_dir, state, agent_name, str(exc))
        return
    if current_step != 5 or agent_name not in {"graph_bootstrap_runner", "graph_path_analyst"}:
        return
    if agent_name == "graph_bootstrap_runner" and current_substep(state) != "A":
        fail_dispatch(
            workspace_dir,
            state,
            agent_name,
            f"graph_bootstrap_runner 只能在 Step 5A 调度，当前 substep={current_substep(state) or '<missing>'}。",
        )
    if agent_name == "graph_path_analyst" and current_substep(state) != "B":
        fail_dispatch(
            workspace_dir,
            state,
            agent_name,
            f"graph_path_analyst 只能在 Step 5B 调度，当前 substep={current_substep(state) or '<missing>'}。",
        )
    try:
        if agent_name == "graph_bootstrap_runner":
            ensure_step5a_graph_bootstrap_dispatch_ready(workspace_dir)
        else:
            ensure_step5b_graph_path_dispatch_ready(workspace_dir, state)
    except Exception as exc:
        fail_dispatch(workspace_dir, state, agent_name, str(exc))


def dispatch_main_agent_actions(agent_name: str, current_step: int) -> list[str]:
    actions = [
        "读取 audit/dispatch_<agent>.json 中的 subagent_type / description / query_text。",
        "立刻发起一次正式 Task(...) 调度，不得在 Task 前运行任何 subagent-only 脚本。",
        "Task 返回后运行 scripts/record_subagent_completion.py，并记录 task_call_id / subagent_id。",
    ]
    if agent_name == "step4_bootstrap_runner" and current_step == 4:
        actions.insert(1, "Step 4A 由 step4_bootstrap_runner 负责托管 run_step4_bootstrap_runner.py；这是 Step 4 的重子阶段，不再允许把 Step4 bootstrap 等待塞进主 agent prepare。")
        actions.insert(2, "step4_bootstrap_runner 完成判定必须以 output/step4_bootstrap_result.json 与 logs/wrapper_runs/step4_bootstrap.lock.json 的收口状态为准，不能只看顶层 exit code。")
    if agent_name == "stack_mapper" and current_step == 4:
        actions.insert(1, "Step 4B 的 prepare 现在是轻校验：它只消费 Step 4A 已冻结的 step4_bootstrap_result.json 与 ready set，不再托管 Step4 bootstrap wrapper。")
        actions.insert(2, "若 Step 4A 尚未 finalize、step4_bootstrap_result.json 不存在或 ready set 不完整，必须先回到 Step 4A，而不是手工补跑 bootstrap 脚本。")
    if agent_name == "graph_bootstrap_runner" and current_step == 5:
        actions.insert(1, "Step 5A 由 graph_bootstrap_runner 负责托管 run_graph_bootstrap_runner.py；这是 Step 5 的重 bootstrap 子阶段。")
        actions.insert(2, "graph_bootstrap_runner 完成判定必须以 output/graph_bootstrap_result.json 与 logs/wrapper_runs/step5_graph_bootstrap.lock.json 的收口状态为准，不能只看顶层 exit code。")
    if agent_name == "graph_path_analyst" and current_step == 5:
        actions.insert(1, "Step 5B 的 prepare 现在是轻校验：它只消费 Step 5A 已冻结的 graph_bootstrap_result.json 与 ready set，不再托管 graph bootstrap wrapper。")
        actions.insert(2, "若 Step 5A 尚未 finalize、graph_bootstrap_result.json 不存在或 ready set 不完整，必须先回到 Step 5A，而不是手工补跑 graph bootstrap 脚本。")
        actions.append("在 completion 之后、finalize 之前允许运行 scripts/normalize_graph_review_result.py。")
    actions.extend(
        [
            "运行 scripts/finalize_agent_dispatch.py 完成正式校验与 promotion。",
            "在运行 scripts/mark_step_complete.py 之前，必须至少更新 findings.md 或 progress.md；若 state.flags.task_plan_refresh_required=true，还必须同步更新 task_plan.md。",
            (
                "在 finalize 成功后运行 scripts/mark_step_complete.py --step 4 --substep A。"
                if agent_name == "step4_bootstrap_runner" and current_step == 4
                else (
                    "在 finalize 成功后运行 scripts/mark_step_complete.py --step 4 --substep B。"
                    if agent_name == "stack_mapper" and current_step == 4
                    else (
                        "在 finalize 成功后运行 scripts/mark_step_complete.py --step 5 --substep A。"
                        if agent_name == "graph_bootstrap_runner" and current_step == 5
                        else (
                            "在 finalize 成功后运行 scripts/mark_step_complete.py --step 5 --substep B。"
                            if agent_name == "graph_path_analyst" and current_step == 5
                            else f"在 finalize 成功后运行 scripts/mark_step_complete.py --step {current_step}。"
                        )
                    )
                )
            ),
        ]
    )
    return actions


def dispatch_main_agent_forbidden_behaviors(agent_name: str) -> list[str]:
    forbidden = [
        "不得跳过真实 Task(...) 直接运行 record_subagent_completion.py 或 finalize_agent_dispatch.py。",
        "不得自行生成或改写子 agent 正式输出 JSON/Markdown 来冒充真实返回。",
        "不得运行任何 subagent-only wrapper、切片、渲染、验证或数据处理脚本。",
        "不得修改 state.json、audit/*.json 或 provenance 工件来伪造闭环。",
    ]
    if agent_name == "profiling_preprocessor":
        forbidden.append("不得替 profiling_preprocessor 手工拆跑 Step 1/2 内部脚本。")
    if agent_name == "step4_bootstrap_runner":
        forbidden.append("不得绕过 step4_bootstrap_runner，手工直接运行 scripts/run_step4_bootstrap_pipeline.py 或拆跑任何 Step 4 bootstrap 子脚本。")
    if agent_name == "graph_bootstrap_runner":
        forbidden.append("不得绕过 graph_bootstrap_runner，手工直接运行 scripts/run_step5_graph_bootstrap_pipeline.py 或拆跑任何 Step 5A graph bootstrap 子脚本。")
    if agent_name in {"stack_mapper", "graph_path_analyst"}:
        forbidden.append(
            "不得在 prepare 之后手工重复运行 Step 4 deterministic bootstrap 脚本："
            + ", ".join(STEP4_BOOTSTRAP_SCRIPTS)
        )
        forbidden.append("不得因为 dispatch 尚未生成、prepare 短暂无输出、主进程短暂返回或顶层 exit code 看起来成功/失败，就怀疑卡住并另起一次 prepare_agent_dispatch.py。")
    if agent_name == "graph_path_analyst":
        forbidden.append(
            "不得手工补跑 Step 5A graph bootstrap 脚本：" + ", ".join(STEP5_GRAPH_BOOTSTRAP_SCRIPTS)
        )
    return forbidden


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    current_step = int(state["current_step"])
    validate_previous_finalize_closure(workspace_dir, state, args.agent_name)
    maybe_bootstrap_step1_inputs(workspace_dir, args.agent_name, current_step)
    maybe_bootstrap_step4_support(workspace_dir, args.agent_name, current_step)
    maybe_bootstrap_graph_step5_support(workspace_dir, args.agent_name, current_step)
    maybe_bootstrap_agent_task_input(workspace_dir, args.agent_name, current_step)
    state = load_state(workspace_dir)
    ensure_step4_agent_matches_substep(state, args.agent_name)
    ensure_step5_agent_matches_substep(state, args.agent_name)
    validate_dispatch_preconditions(workspace_dir, state, args.agent_name, int(state["current_step"]))
    config = effective_agent_config(args.agent_name, int(state["current_step"]))
    substep = current_substep(state) if int(config["step"]) in {4, 5} else ""

    if config["step"] > 0:
        ensure(state["current_step"] == config["step"], f"{args.agent_name} 只能在 step {config['step']} 调度，当前是 step {state['current_step']}。")
    input_files = resolve_workspace_paths(workspace_dir, config["input_files"])
    for path in input_files:
        ensure(path.exists(), f"调度 {args.agent_name} 前缺少输入文件: {path}")

    task_input_path, task_input_payload = build_task_input_payload(workspace_dir, args.agent_name)
    bundle = build_query_bundle(workspace_dir, args.agent_name)
    query_meta = write_query_artifacts(workspace_dir, args.agent_name, bundle["query_text"], bundle["config"])
    substep_suffix = f"-substep{substep}" if substep else ""
    dispatch_id = f"{args.agent_name}-step{config['step']}{substep_suffix}-{now_iso()}"
    completion_marker_path = dispatch_completion_marker_path(workspace_dir, args.agent_name)
    if completion_marker_path.exists():
        completion_marker_path.unlink()
    allowed_official_scripts = [
        str(item).strip()
        for item in task_input_payload.get("allowed_official_scripts", [])
        if str(item).strip()
    ]
    required_wrapper_script = str(task_input_payload.get("required_wrapper_script", "")).strip()
    subagent_must_not_do = [
        str(item).strip()
        for item in task_input_payload.get("must_not_do", [])
        if str(item).strip()
    ]

    dispatch_payload = {
        "agent_name": args.agent_name,
        "dispatch_id": dispatch_id,
        "description": config["description"],
        "subagent_type": config["subagent_type"],
        "step": config["step"],
        "substep": substep,
        "guide_file": str(bundle["guide_path"]),
        "prompt_file": str(bundle["prompt_path"]),
        "input_files": [str(path) for path in bundle["input_files"]],
        "output_files": [str(path) for path in bundle["output_files"]],
        "allowed_status": sorted(config["allowed_status"]),
        "query_path": query_meta["query_path"],
        "query_snapshot_path": query_meta["snapshot_path"],
        "query_snapshot_sha256": query_meta["snapshot_sha256"],
        "query_text": bundle["query_text"],
        "task_required": True,
        "task_receipt_required": True,
        "completion_required": True,
        "completion_marker_path": str(completion_marker_path),
        "completion_record_script": str(Path(state["skill_dir"]) / "scripts" / "record_subagent_completion.py"),
        "main_agent_role": "dispatcher_only",
        "subagent_role": "contract_executor",
        "main_agent_next_actions": dispatch_main_agent_actions(args.agent_name, int(config["step"])),
        "main_agent_forbidden_behaviors": dispatch_main_agent_forbidden_behaviors(args.agent_name),
        "task_input_path": str(task_input_path),
        "required_wrapper_script": required_wrapper_script,
        "allowed_official_scripts": allowed_official_scripts,
        "subagent_must_not_do": subagent_must_not_do,
        "workspace_temp_script_baseline": list_workspace_temp_scripts(workspace_dir),
    }
    dispatch_path = workspace_dir / "audit" / f"dispatch_{args.agent_name}.json"
    dump_json(dispatch_path, dispatch_payload)

    state["next_action"] = f"dispatch_{args.agent_name}"
    agents = state.setdefault("agents", {})
    agent_slot = agents.setdefault(args.agent_name, {})
    agent_slot["dispatch_ready_path"] = str(dispatch_path)
    agent_slot["active_dispatch_id"] = dispatch_id
    agent_slot["expected_completion_marker_path"] = str(completion_marker_path)
    agent_slot["last_completion_marker_path"] = ""
    agent_slot["last_completion_recorded_at"] = ""
    agent_slot["last_query_snapshot"] = query_meta["snapshot_path"]
    orchestration = state.setdefault("orchestration", {})
    orchestration["mode"] = "main_agent_with_subagents"
    orchestration["active_agent"] = args.agent_name
    orchestration["active_dispatch_path"] = str(dispatch_path)
    orchestration["active_dispatch_id"] = dispatch_id
    orchestration["active_completion_marker_path"] = str(completion_marker_path)
    orchestration["dispatch_temp_script_baseline"] = dispatch_payload["workspace_temp_script_baseline"]
    save_state(workspace_dir, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
