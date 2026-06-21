from __future__ import annotations

import argparse
from pathlib import Path

from workflow_common import load_state, required_step


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行 step 前检查。")
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument("--step", required=True, type=int)
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_common(state: dict, step: int) -> None:
    ensure(state["status"] != "completed", "任务已经完成，不能继续执行 step。")
    ensure(
        state["status"] != "awaiting_final_gate",
        "当前已进入 awaiting_final_gate，不能再次执行 step；请运行 check_final_gate.py。",
    )
    ensure(state["current_step"] == step, f"当前允许 step={state['current_step']}，不是 {step}。")
    ensure(state["next_action"] != "call_profiling_debugger", "当前必须先调用 profiling-debugger。")
    required_step(step)


def require_path(path_str: str, label: str) -> None:
    ensure(bool(path_str), f"{label} 为空。")
    ensure(Path(path_str).exists(), f"{label} 不存在: {path_str}")


def step1_checks(state: dict) -> None:
    inputs = state["inputs"]
    require_path(inputs["profiling_root_path"], "profiling_root_path")
    ensure(int(inputs["window_start_ns"]) < int(inputs["window_end_ns"]), "时间窗口不合法。")
    require_path(inputs["code_repo_path"], "code_repo_path")
    require_path(inputs["model_root_path"], "model_root_path")
    ensure(
        bool(inputs["launch_command_file"] or inputs["launch_command_text"]),
        "launch_command_file 与 launch_command_text 至少提供一个。",
    )
    profiling_root = Path(inputs["profiling_root_path"])
    require_path(profiling_root / "ASCEND_PROFILER_OUTPUT" / "trace_view.json", "trace_view.json")
    require_path(profiling_root / "ASCEND_PROFILER_OUTPUT" / "kernel_details.csv", "kernel_details.csv")
    require_path(profiling_root / "ASCEND_PROFILER_OUTPUT" / "operator_details.csv", "operator_details.csv")
    mindstudio_root = next(profiling_root.glob("PROF_*/*"), None)
    ensure(mindstudio_root is not None and mindstudio_root.is_dir(), "缺少 mindstudio_profiler_output 子目录。")


def step2_checks(state: dict) -> None:
    artifacts = state["artifacts"]
    for key in [
        "trace_slice_path",
        "kernel_slice_path",
        "operator_slice_path",
        "task_time_slice_path",
        "op_summary_slice_path",
    ]:
        require_path(artifacts[key], key)


def step3_checks(state: dict) -> None:
    require_path(state["artifacts"]["timeline_index_path"], "timeline_index_path")


def step4_checks(state: dict) -> None:
    require_path(state["artifacts"]["classified_spans_path"], "classified_spans_path")
    require_path(Path(state["workspace_dir"]) / "output" / "timeline_analysis.json", "timeline_analysis.json")


def step5_checks(state: dict) -> None:
    require_path(state["artifacts"]["classified_spans_path"], "classified_spans_path")
    require_path(state["artifacts"]["stack_evidence_path"], "stack_evidence_path")
    require_path(state["artifacts"]["graph_phase_stack_evidence_path"], "graph_phase_stack_evidence_path")


def step6_checks(state: dict) -> None:
    require_path(state["artifacts"]["classified_spans_path"], "classified_spans_path")
    require_path(state["artifacts"]["stack_evidence_path"], "stack_evidence_path")
    require_path(state["artifacts"]["external_span_mapping_path"], "external_span_mapping_path")
    require_path(state["artifacts"]["graph_execution_plan_path"], "graph_execution_plan_path")
    require_path(state["artifacts"]["graph_forward_context_path"], "graph_forward_context_path")
    require_path(state["artifacts"]["graph_mapping_targets_path"], "graph_mapping_targets_path")
    require_path(state["artifacts"]["graph_operator_spans_path"], "graph_operator_spans_path")
    require_path(state["artifacts"]["graph_span_candidates_path"], "graph_span_candidates_path")
    require_path(state["artifacts"]["forward_segment_template_path"], "forward_segment_template_path")
    require_path(state["artifacts"]["graph_span_alignment_path"], "graph_span_alignment_path")


def step7_checks(state: dict) -> None:
    require_path(state["artifacts"]["annotated_trace_path"], "annotated_trace_path")
    require_path(state["artifacts"]["stream_span_timeline_path"], "stream_span_timeline_path")
    require_path(state["artifacts"]["span_code_mapping_path"], "span_code_mapping_path")
    require_path(state["artifacts"]["graph_execution_plan_path"], "graph_execution_plan_path")
    require_path(state["artifacts"]["graph_forward_context_path"], "graph_forward_context_path")
    require_path(state["artifacts"]["graph_mapping_targets_path"], "graph_mapping_targets_path")
    require_path(state["artifacts"]["graph_operator_spans_path"], "graph_operator_spans_path")
    require_path(state["artifacts"]["graph_span_alignment_path"], "graph_span_alignment_path")


def main() -> int:
    args = build_parser().parse_args()
    state = load_state(Path(args.workspace_dir))
    validate_common(state, args.step)
    step_specific = {
        1: step1_checks,
        2: step2_checks,
        3: step3_checks,
        4: step4_checks,
        5: step5_checks,
        6: step6_checks,
        7: step7_checks,
    }
    step_specific[args.step](state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
