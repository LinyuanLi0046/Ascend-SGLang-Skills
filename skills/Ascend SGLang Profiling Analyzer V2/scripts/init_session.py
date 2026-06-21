from __future__ import annotations

import argparse
from pathlib import Path

from workflow_common import AGENT_NAMES, STEP_DEFINITIONS, document_hashes, dump_json, now_iso


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="初始化 V2 skill workspace。")
    parser.add_argument("--skill-dir", required=True, help="Skill 根目录绝对路径。")
    parser.add_argument("--workspace-dir", required=True, help="Workspace 根目录绝对路径。")
    parser.add_argument("--task-id", required=True, help="任务唯一标识。")
    return parser


def default_state(skill_dir: Path, workspace_dir: Path, task_id: str) -> dict:
    timestamp = now_iso()
    state = {
        "schema": "profiling_stack_skill_state/v1",
        "task_id": task_id,
        "skill_dir": str(skill_dir),
        "workspace_dir": str(workspace_dir),
        "status": "initialized",
        "current_step": 1,
        "last_completed_step": 0,
        "next_action": "run_step_1",
        "created_at": timestamp,
        "updated_at": timestamp,
        "inputs": {
            "profiling_root_path": "",
            "window_start_ns": 0,
            "window_end_ns": 0,
            "code_repo_path": "d:/Agent/sglang-prof/sglang-main",
            "model_root_path": "",
            "draft_model_root_path": "",
            "launch_command_file": "",
            "launch_command_text": "",
            "benchmark_result_path": "",
            "supplemental_input_paths": [],
        },
        "flags": {
            "input_resolution_done": False,
            "input_contract_valid": False,
            "raw_profiling_discovered": False,
            "slicing_done": False,
            "python_tracer_index_built": False,
            "timeline_index_built": False,
            "classification_done": False,
            "hardware_scope_classified": False,
            "scope_gate_passed": False,
            "stack_evidence_built": False,
            "stack_call_paths_built": False,
            "external_mapping_targets_built": False,
            "external_span_mapping_built": False,
            "graph_phase_stack_evidence_built": False,
            "graph_path_built": False,
            "repo_divergence_checked": False,
            "runtime_constraints_built": False,
            "graph_seed_context_built": False,
            "graph_forward_context_built": False,
            "graph_span_identified": False,
            "forward_segment_template_built": False,
            "graph_span_alignment_built": False,
            "span_mapping_done": False,
            "annotated_trace_generated": False,
            "timeline_generated": False,
            "validation_passed": False,
            "debug_fix_pending": False,
            "has_low_confidence_mappings": False,
            "has_unresolved_semantic_spans": False,
            "task_plan_refresh_required": False,
        },
        "artifacts": {
            "input_resolution_path": "",
            "normalized_launch_command_path": "",
            "raw_trace_path": "",
            "raw_kernel_csv_path": "",
            "raw_operator_details_path": "",
            "raw_task_time_paths": [],
            "raw_op_summary_paths": [],
            "framework_python_tracer_hash_path": "",
            "framework_python_tracer_func_path": "",
            "trace_slice_path": "",
            "kernel_slice_path": "",
            "operator_slice_path": "",
            "task_time_slice_path": "",
            "op_summary_slice_path": "",
            "python_tracer_index_path": "",
            "timeline_index_path": "",
            "classified_spans_path": "",
            "scope_gate_result_path": "",
            "stack_evidence_path": "",
            "stack_evidence_lite_path": "",
            "stack_call_paths_path": "",
            "external_mapping_targets_path": "",
            "external_span_mapping_path": "",
            "graph_phase_stack_evidence_path": "",
            "repo_divergence_report_path": "",
            "runtime_constraints_path": "",
            "graph_seed_context_path": "",
            "graph_execution_plan_path": "",
            "graph_mapping_targets_path": "",
            "graph_forward_context_path": "",
            "graph_operator_spans_path": "",
            "graph_span_candidates_path": "",
            "forward_segment_template_path": "",
            "graph_span_alignment_path": "",
            "span_code_mapping_path": "",
            "annotated_trace_path": "",
            "stream_span_timeline_path": "",
            "validation_result_path": "",
            "fix_instructions_path": "",
        },
        "agents": {
            agent_name: {
                "last_status": "",
                "last_called_at": "",
                "last_query_snapshot": "",
                "last_output_path": "",
                "dispatch_ready_path": "",
                "active_dispatch_id": "",
                "expected_completion_marker_path": "",
                "last_completion_marker_path": "",
                "last_completion_recorded_at": "",
            }
            for agent_name in sorted(AGENT_NAMES)
        },
        "orchestration": {
            "mode": "main_agent_with_subagents",
            "active_agent": "",
            "active_dispatch_path": "",
            "active_dispatch_id": "",
            "active_completion_marker_path": "",
            "dispatch_temp_script_baseline": [],
            "last_finalize_agent": "",
            "last_finalize_at": "",
            "last_finalize_hashes": {},
            "last_allowed_scripts": [],
            "provenance_verified": False,
            "illegal_temp_script_detected": False,
            "last_provenance_error": "",
            "last_subagent_completion_agent": "",
            "last_subagent_completion_at": "",
        },
        "document_hashes_baseline": {},
        "step_history": [],
    }
    return state


def write_workspace_templates(workspace_dir: Path) -> None:
    for relative in [
        "input",
        "artifacts/slices",
        "artifacts/index",
        "artifacts/classification",
        "artifacts/stacks",
        "artifacts/mapping",
        "artifacts/graph",
        "artifacts/repo",
        "artifacts/timeline",
        "output",
        "logs/agent_calls",
        "audit",
    ]:
        (workspace_dir / relative).mkdir(parents=True, exist_ok=True)

    (workspace_dir / "task_plan.md").write_text(
        "\n".join(
            [
                "# Task Plan",
                "",
                "- 当前阶段: 初始化完成",
                "- 当前 step: 1 INPUT_DISCOVERY_AND_SLICING",
                "- 下一动作: 运行 Step 1 输入发现与切片",
                "",
                "## Step Definitions",
                "",
            ]
            + [f"- {step}: {name}" for step, name in STEP_DEFINITIONS.items()]
            + [""]
        ),
        encoding="utf-8",
        newline="\n",
    )
    (workspace_dir / "findings.md").write_text(
        "# Findings\n\n## Confirmed Facts\n\n- 初始化 findings 文件\n",
        encoding="utf-8",
        newline="\n",
    )
    (workspace_dir / "progress.md").write_text(
        "# Progress\n\n- 初始化 workspace 与状态文件。\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    args = build_parser().parse_args()
    skill_dir = Path(args.skill_dir).resolve()
    workspace_dir = Path(args.workspace_dir).resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    write_workspace_templates(workspace_dir)
    state = default_state(skill_dir, workspace_dir, args.task_id)
    state["document_hashes_baseline"] = document_hashes(workspace_dir)
    dump_json(workspace_dir / "state.json", state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
