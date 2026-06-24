from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any


AGENT_CONFIG: dict[str, dict[str, Any]] = {
    "profiling_preprocessor": {
        "steps": [1, 2],
        "subagent_type": "general_purpose_task",
        "description": "Prepare profiling data",
        "guide_file": "references/agents/profiling_preprocessor.md",
        "prompt_file": "prompts/profiling-preprocessor.md",
        "step_overrides": {
            1: {
                "description": "Prepare profiling slices",
                "input_files": [
                    "input/preprocess_task.json",
                    "input/input_resolution.json",
                    "input/input_contract.json",
                    "input/source_inventory.json",
                ],
                "output_files": [
                    "output/preprocess_step1_result.json",
                    "output/preprocess_step1_report.md",
                ],
                "allowed_status": {"passed"},
            },
            2: {
                "description": "Build timeline index",
                "input_files": [
                    "input/preprocess_task.json",
                    "artifacts/slices/trace_slice.json",
                    "artifacts/slices/kernel_details_slice.csv",
                    "artifacts/slices/operator_details_slice.csv",
                    "artifacts/slices/task_time_slice.csv",
                    "artifacts/slices/op_summary_slice.csv",
                ],
                "output_files": [
                    "output/preprocess_step2_result.json",
                    "output/preprocess_step2_report.md",
                ],
                "allowed_status": {"passed"},
            },
        },
    },
    "timeline_analyst": {
        "step": 3,
        "subagent_type": "general_purpose_task",
        "description": "Analyze timeline semantics",
        "contract_schema": "timeline_review_patch_v1",
        "contract_schema_file": "references/contracts/timeline_review_patch.schema.json",
        "secondary_contract_schema_files": [
            "references/contracts/timeline_analysis_result.schema.json",
        ],
        "contract_example_files": [
            "examples/contracts/timeline_review_patch.passed.sample.json",
            "examples/contracts/timeline_analysis_result.passed.sample.json",
        ],
        "guide_file": "references/agents/timeline_analyst.md",
        "prompt_file": "prompts/timeline-analyst.md",
        "input_files": [
            "input/timeline_task.json",
            "artifacts/index/timeline_index.json",
            "artifacts/slices/trace_slice.json",
        ],
        "output_files": [
            "output/timeline_review_patch.json",
            "artifacts/classification/classified_spans.base.json",
            "output/scope_gate_result.base.json",
            "output/timeline_analysis.json",
            "output/timeline_analysis.md",
        ],
        "allowed_status": {"passed"},
    },
    "step4_bootstrap_runner": {
        "step": 4,
        "subagent_type": "general_purpose_task",
        "description": "Run Step4A bootstrap",
        "contract_schema": "step4_bootstrap_runner_v1",
        "contract_schema_file": "references/contracts/step4_bootstrap_result.schema.json",
        "guide_file": "references/agents/step4_bootstrap_runner.md",
        "prompt_file": "prompts/step4-bootstrap-runner.md",
        "input_files": [
            "input/step4_bootstrap_task.json",
        ],
        "output_files": [
            "output/step4_bootstrap_result.json",
            "output/step4_bootstrap_report.md",
        ],
        "allowed_status": {"passed"},
    },
    "stack_mapper": {
        "step": 4,
        "subagent_type": "general_purpose_task",
        "description": "Map non-graph spans",
        "contract_schema": "stack_mapper_v2",
        "contract_schema_file": "references/contracts/stack_mapping_result.schema.json",
        "contract_example_files": [
            "examples/contracts/stack_mapping_result.passed.sample.json",
            "examples/contracts/stack_mapping_result.partial.sample.json",
        ],
        "guide_file": "references/agents/stack_mapper.md",
        "prompt_file": "prompts/stack-mapper.md",
        "input_files": [
            "input/stack_mapping_task.json",
            "output/step4_bootstrap_result.json",
            "artifacts/mapping/stack_evidence.json",
            "artifacts/mapping/stack_call_paths.json",
            "artifacts/mapping/external_mapping_targets.json",
            "artifacts/stacks/python_tracer_index.json",
            "artifacts/classification/classified_spans.json",
            "artifacts/index/timeline_index.json",
        ],
        "output_files": [
            "output/stack_mapping_result.json",
            "output/stack_mapping_report.md",
        ],
        "allowed_status": {"passed", "partial"},
    },
    "graph_bootstrap_runner": {
        "step": 5,
        "subagent_type": "general_purpose_task",
        "description": "Run Step5A bootstrap",
        "contract_schema": "graph_bootstrap_runner_v1",
        "contract_schema_file": "references/contracts/graph_bootstrap_result.schema.json",
        "contract_example_files": [
            "examples/contracts/graph_bootstrap_result.passed.sample.json",
        ],
        "guide_file": "references/agents/graph_bootstrap_runner.md",
        "prompt_file": "prompts/graph-bootstrap-runner.md",
        "input_files": [
            "input/graph_bootstrap_task.json",
        ],
        "output_files": [
            "output/graph_bootstrap_result.json",
            "output/graph_bootstrap_report.md",
        ],
        "allowed_status": {"passed"},
    },
    "graph_path_analyst": {
        "step": 5,
        "subagent_type": "general_purpose_task",
        "description": "Reconstruct graph path",
        "contract_schema": "graph_path_analyst_v2",
        "contract_schema_file": "references/contracts/graph_review_result.schema.json",
        "contract_example_files": [
            "examples/contracts/graph_review_result.partial.sample.json",
            "examples/contracts/graph_review_result.passed.sample.json",
        ],
        "normalizer_script": "scripts/normalize_graph_review_result.py",
        "guide_file": "references/agents/graph_path_analyst.md",
        "prompt_file": "prompts/graph-path-analyst.md",
        "input_files": [
            "input/graph_path_task.json",
            "output/graph_bootstrap_result.json",
            "artifacts/classification/classified_spans.json",
            "artifacts/index/timeline_index.json",
            "artifacts/mapping/stack_evidence.json",
            "artifacts/graph/graph_phase_stack_evidence.json",
            "artifacts/graph/graph_mapping_targets.json",
            "artifacts/repo/repo_divergence_report.json",
            "input/launch_command.json",
            "input/model_context.json",
            "input/runtime_constraints.json",
            "input/graph_seed_context.json",
            "artifacts/graph/graph_execution_plan.json",
            "artifacts/graph/graph_forward_context.json",
            "artifacts/graph/graph_operator_spans.json",
        ],
        "output_files": [
            "output/graph_review_result.json",
            "output/graph_path_report.md",
        ],
        "allowed_status": {"passed", "partial"},
    },
    "artifact_validator": {
        "step": 7,
        "subagent_type": "general_purpose_task",
        "description": "Validate final artifacts",
        "contract_schema": "artifact_validator_v2",
        "guide_file": "references/agents/artifact_validator.md",
        "prompt_file": "prompts/artifact-validator.md",
        "input_files": [
            "input/validation_task.json",
            "artifacts/mapping/span_code_mapping.json",
            "artifacts/classification/classified_spans.json",
            "output/trace_view.annotated.json",
            "artifacts/timeline/stream_span_timeline.json",
            "artifacts/graph/graph_execution_plan.json",
            "artifacts/graph/graph_forward_context.json",
            "artifacts/graph/graph_mapping_targets.json",
            "artifacts/graph/graph_span_candidates.json",
            "artifacts/graph/forward_segment_template.json",
            "artifacts/graph/graph_span_alignment.json",
            "artifacts/graph/graph_operator_spans.json",
        ],
        "output_files": [
            "output/validation_result.json",
            "output/validation_report.md",
        ],
        "allowed_status": {"passed", "failed"},
    },
    "profiling_debugger": {
        "step": 0,
        "subagent_type": "general_purpose_task",
        "description": "Diagnose pipeline failure",
        "guide_file": "references/agents/profiling_debugger.md",
        "prompt_file": "prompts/profiling-debugger.md",
        "input_files": [
            "input/error_context.json",
        ],
        "output_files": [
            "output/fix_instructions.json",
            "output/debug_report.md",
        ],
        "allowed_status": {"fix_available", "fix_verified", "passed"},
    },
    "artifact_renderer": {
        "step": 6,
        "subagent_type": "general_purpose_task",
        "description": "Render final artifacts",
        "contract_schema": "artifact_renderer_v2",
        "guide_file": "references/agents/artifact_renderer.md",
        "prompt_file": "prompts/artifact-renderer.md",
        "input_files": [
            "input/render_task.json",
            "artifacts/classification/classified_spans.json",
            "artifacts/mapping/stack_evidence.json",
            "artifacts/mapping/external_span_mapping.json",
            "artifacts/graph/graph_execution_plan.json",
            "artifacts/graph/graph_forward_context.json",
            "artifacts/graph/graph_mapping_targets.json",
            "artifacts/graph/graph_span_candidates.json",
            "artifacts/graph/forward_segment_template.json",
            "artifacts/graph/graph_span_alignment.json",
            "artifacts/graph/graph_operator_spans.json",
        ],
        "output_files": [
            "output/render_result.json",
            "output/render_report.md",
        ],
        "allowed_status": {"passed"},
    },
}


def resolve_workspace_paths(base_dir: Path, items: list[str]) -> list[Path]:
    resolved = []
    for item in items:
        path = Path(item)
        if not path.is_absolute():
            path = base_dir / item
        resolved.append(path)
    return resolved


def effective_agent_config(agent_name: str, current_step: int) -> dict[str, Any]:
    config = deepcopy(AGENT_CONFIG[agent_name])
    steps = config.get("steps")
    step = config.get("step", 0)
    if steps is not None:
        if current_step not in steps:
            raise ValueError(f"{agent_name} 只能在 steps={steps} 调度，当前是 step {current_step}。")
        override = config.get("step_overrides", {}).get(current_step, {})
        config.update(override)
        config["step"] = current_step
    elif step > 0 and current_step != step:
        raise ValueError(f"{agent_name} 只能在 step {step} 调度，当前是 step {current_step}。")
    else:
        config["step"] = current_step if step == 0 else step
    return config
