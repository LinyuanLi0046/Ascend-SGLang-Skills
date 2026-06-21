from __future__ import annotations

import argparse
from pathlib import Path

from agent_contracts import AGENT_CONFIG, effective_agent_config, resolve_workspace_paths
from build_agent_query import build_query_bundle, write_query_artifacts
from build_external_mapping_targets import build_external_mapping_targets_for_workspace
from build_graph_forward_context import build_graph_forward_context_for_workspace
from build_graph_mapping_targets import build_graph_mapping_targets_for_workspace
from build_graph_operator_spans import build_graph_operator_spans_for_workspace
from build_graph_phase_stack_evidence import build_graph_phase_stack_evidence_for_workspace
from build_runtime_constraints import _select_existing_config_file, build_runtime_constraints_for_workspace
from build_graph_seed_context import build_graph_seed_context_for_workspace
from build_stack_call_paths import build_stack_call_paths_for_workspace
from build_stack_evidence import build_stack_evidence_for_workspace
from check_repo_divergence import check_repo_divergence_for_workspace
from check_scope_gate import check_scope_gate_for_workspace
from classify_graph_groups import build_graph_execution_plan_for_workspace
from discover_inputs import discover_inputs_for_workspace
from resolve_step1_inputs import resolve_inputs_for_workspace
from write_agent_task_input import TASK_FILENAME_BY_AGENT, build_payload as build_task_input_payload
from workflow_common import (
    dispatch_completion_marker_path,
    dump_json,
    list_workspace_temp_scripts,
    load_json,
    load_state,
    now_iso,
    save_state,
    write_error_context,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="为主 agent 准备一次正式子 agent 调度。")
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument("--agent-name", required=True, choices=sorted(AGENT_CONFIG))
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


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
    orchestration["active_agent"] = agent_name
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


def maybe_bootstrap_scope_gate_result(workspace_dir: Path, agent_name: str, current_step: int) -> None:
    if agent_name != "timeline_analyst" or current_step != 3:
        return
    check_scope_gate_for_workspace(workspace_dir)


def maybe_bootstrap_shared_graph_scope(workspace_dir: Path, current_step: int) -> None:
    if current_step not in {4, 5}:
        return
    check_repo_divergence_for_workspace(workspace_dir)
    build_runtime_constraints_for_workspace(workspace_dir)
    build_stack_evidence_for_workspace(workspace_dir)
    build_graph_phase_stack_evidence_for_workspace(workspace_dir)
    build_graph_execution_plan_for_workspace(workspace_dir)
    build_graph_mapping_targets_for_workspace(workspace_dir)
    build_external_mapping_targets_for_workspace(workspace_dir)


def maybe_bootstrap_step4_support(workspace_dir: Path, agent_name: str, current_step: int) -> None:
    if agent_name != "stack_mapper" or current_step != 4:
        return
    maybe_bootstrap_shared_graph_scope(workspace_dir, current_step)
    build_stack_call_paths_for_workspace(workspace_dir)


def maybe_bootstrap_graph_step5_support(workspace_dir: Path, agent_name: str, current_step: int) -> None:
    if agent_name != "graph_path_analyst" or current_step != 5:
        return
    maybe_bootstrap_shared_graph_scope(workspace_dir, current_step)
    build_graph_forward_context_for_workspace(workspace_dir)
    build_graph_seed_context_for_workspace(workspace_dir)
    build_graph_operator_spans_for_workspace(workspace_dir)


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
    if agent_name != "graph_path_analyst" or current_step != 5:
        return
    inputs = state.get("inputs", {})
    model_root_path = str(inputs.get("model_root_path", "")).strip()
    code_repo_path = str(inputs.get("code_repo_path", "")).strip()
    if not model_root_path:
        fail_dispatch(workspace_dir, state, agent_name, "Step 5 缺少 model_root_path，禁止调度 graph_path_analyst。")
    model_root = Path(model_root_path)
    code_repo = Path(code_repo_path) if code_repo_path else None
    if not model_root.exists() or not model_root.is_dir():
        fail_dispatch(workspace_dir, state, agent_name, f"Step 5 的 model_root_path 不存在或不是目录: {model_root}")
    if code_repo and code_repo.exists():
        try:
            if model_root.resolve() == code_repo.resolve():
                fail_dispatch(workspace_dir, state, agent_name, "Step 5 的 model_root_path 指向代码仓根目录而不是实际模型目录。")
        except OSError:
            pass
    required_model_markers = [
        _select_existing_config_file(model_root, "config.json"),
        _select_existing_config_file(model_root, "generation_config.json"),
        _select_existing_config_file(model_root, "quant_model_description.json"),
    ]
    if not any(path and path.exists() for path in required_model_markers):
        fail_dispatch(
            workspace_dir,
            state,
            agent_name,
            "Step 5 的 model_root_path 缺少 config.json / generation_config.json / quant_model_description.json 等关键模型文件。",
        )
    runtime_constraints_path = workspace_dir / "input" / "runtime_constraints.json"
    graph_forward_context_path = workspace_dir / "artifacts" / "graph" / "graph_forward_context.json"
    graph_plan_path = workspace_dir / "artifacts" / "graph" / "graph_execution_plan.json"
    graph_mapping_targets_path = workspace_dir / "artifacts" / "graph" / "graph_mapping_targets.json"
    graph_operator_spans_path = workspace_dir / "artifacts" / "graph" / "graph_operator_spans.json"
    if (
        not runtime_constraints_path.exists()
        or not graph_forward_context_path.exists()
        or not graph_plan_path.exists()
        or not graph_mapping_targets_path.exists()
        or not graph_operator_spans_path.exists()
    ):
        fail_dispatch(
            workspace_dir,
            state,
            agent_name,
            "Step 5 缺少 runtime_constraints / graph_forward_context / graph_execution_plan / graph_mapping_targets / graph_operator_spans 正式前置工件。",
        )
    runtime_constraints = load_json(runtime_constraints_path)
    graph_forward_context = load_json(graph_forward_context_path)
    graph_plan = load_json(graph_plan_path)
    graph_mapping_targets = load_json(graph_mapping_targets_path)
    graph_operator_spans = load_json(graph_operator_spans_path)
    runtime_preconditions = runtime_constraints.get("step5_preconditions", {})
    if not bool(runtime_preconditions.get("ready")):
        blockers = [str(item) for item in runtime_preconditions.get("blockers", []) if str(item).strip()]
        fail_dispatch(
            workspace_dir,
            state,
            agent_name,
            "Step 5 运行前提未满足，禁止调度 graph_path_analyst: " + "; ".join(blockers[:10]),
        )
    if str(graph_plan.get("graph_mode", "")).strip() not in {"speculative", "decode_only"}:
        fail_dispatch(workspace_dir, state, agent_name, "Step 5 graph_execution_plan 未形成 speculative/decode_only 的正式 graph mode。")
    if not graph_plan.get("graph_groups"):
        fail_dispatch(workspace_dir, state, agent_name, "Step 5 graph_execution_plan 未识别到可下钻的 graph groups。")
    graph_mapping_target_rows = graph_mapping_targets.get("rows", [])
    if not isinstance(graph_mapping_target_rows, list) or not graph_mapping_target_rows:
        fail_dispatch(workspace_dir, state, agent_name, "Step 5 graph_mapping_targets 未识别到可下钻的正式 graph mapping targets。")
    graph_operator_span_rows = graph_operator_spans.get("rows", [])
    if not isinstance(graph_operator_span_rows, list) or not graph_operator_span_rows:
        fail_dispatch(workspace_dir, state, agent_name, "Step 5 graph_operator_spans 未能围绕 formal graph target 生成正式 operator skeleton。")
    readiness = graph_forward_context.get("path_reconstruction_readiness", {})
    if not bool(graph_forward_context.get("path_reconstruction_ready")):
        blockers = [str(item) for item in readiness.get("blockers", []) if str(item).strip()]
        fail_dispatch(
            workspace_dir,
            state,
            agent_name,
            "Step 5 graph_forward_context 判定当前样例尚不具备真实路径下钻前提: " + "; ".join(blockers[:10]),
        )


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    current_step = int(state["current_step"])
    validate_previous_finalize_closure(workspace_dir, state, args.agent_name)
    maybe_bootstrap_step1_inputs(workspace_dir, args.agent_name, current_step)
    maybe_bootstrap_scope_gate_result(workspace_dir, args.agent_name, current_step)
    maybe_bootstrap_step4_support(workspace_dir, args.agent_name, current_step)
    maybe_bootstrap_graph_step5_support(workspace_dir, args.agent_name, current_step)
    maybe_bootstrap_agent_task_input(workspace_dir, args.agent_name, current_step)
    state = load_state(workspace_dir)
    validate_dispatch_preconditions(workspace_dir, state, args.agent_name, int(state["current_step"]))
    config = effective_agent_config(args.agent_name, int(state["current_step"]))

    if config["step"] > 0:
        ensure(state["current_step"] == config["step"], f"{args.agent_name} 只能在 step {config['step']} 调度，当前是 step {state['current_step']}。")
    input_files = resolve_workspace_paths(workspace_dir, config["input_files"])
    for path in input_files:
        ensure(path.exists(), f"调度 {args.agent_name} 前缺少输入文件: {path}")

    bundle = build_query_bundle(workspace_dir, args.agent_name)
    query_meta = write_query_artifacts(workspace_dir, args.agent_name, bundle["query_text"], bundle["config"])
    dispatch_id = f"{args.agent_name}-step{config['step']}-{now_iso()}"
    completion_marker_path = dispatch_completion_marker_path(workspace_dir, args.agent_name)
    if completion_marker_path.exists():
        completion_marker_path.unlink()

    dispatch_payload = {
        "agent_name": args.agent_name,
        "dispatch_id": dispatch_id,
        "description": config["description"],
        "subagent_type": config["subagent_type"],
        "step": config["step"],
        "guide_file": str(bundle["guide_path"]),
        "prompt_file": str(bundle["prompt_path"]),
        "input_files": [str(path) for path in bundle["input_files"]],
        "output_files": [str(path) for path in bundle["output_files"]],
        "allowed_status": sorted(config["allowed_status"]),
        "query_path": query_meta["query_path"],
        "query_snapshot_path": query_meta["snapshot_path"],
        "query_text": bundle["query_text"],
        "completion_required": True,
        "completion_marker_path": str(completion_marker_path),
        "completion_record_script": str(Path(state["skill_dir"]) / "scripts" / "record_subagent_completion.py"),
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
