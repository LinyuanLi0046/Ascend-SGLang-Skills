from __future__ import annotations

import argparse
from pathlib import Path

from resolve_step1_inputs import resolve_inputs_for_workspace
from workflow_common import dump_json, load_state, save_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="发现原始 profiling 输入文件。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def find_mindstudio_root(profiling_root: Path) -> Path:
    for candidate in profiling_root.glob("PROF_*/mindstudio_profiler_output"):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"未找到 mindstudio_profiler_output: {profiling_root}")


def discover_inputs_for_workspace(workspace_dir: Path) -> dict:
    resolve_inputs_for_workspace(workspace_dir)
    state = load_state(workspace_dir)
    profiling_root = Path(state["inputs"]["profiling_root_path"])
    ascend_root = profiling_root / "ASCEND_PROFILER_OUTPUT"
    mindstudio_root = find_mindstudio_root(profiling_root)
    framework_root = profiling_root / "FRAMEWORK"

    source_inventory = {
        "raw_trace_path": str(ascend_root / "trace_view.json"),
        "raw_kernel_csv_path": str(ascend_root / "kernel_details.csv"),
        "raw_operator_details_path": str(ascend_root / "operator_details.csv"),
        "raw_task_time_paths": [str(path) for path in sorted(mindstudio_root.glob("task_time_*.csv"))],
        "raw_op_summary_paths": [str(path) for path in sorted(mindstudio_root.glob("op_summary_*.csv"))],
        "raw_msprof_json_paths": [str(path) for path in sorted(mindstudio_root.glob("msprof_*.json"))],
        "framework_python_tracer_hash_path": str(framework_root / "torch.python_tracer_hash"),
        "framework_python_tracer_func_path": str(framework_root / "torch.python_tracer_func"),
    }
    if not source_inventory["raw_task_time_paths"]:
        raise FileNotFoundError("至少需要一份 task_time_*.csv")
    if not source_inventory["raw_op_summary_paths"]:
        raise FileNotFoundError("至少需要一份 op_summary_*.csv")

    input_contract = {
        "profiling_root_path": str(profiling_root),
        "window_start_ns": int(state["inputs"]["window_start_ns"]),
        "window_end_ns": int(state["inputs"]["window_end_ns"]),
        "code_repo_path": state["inputs"]["code_repo_path"],
        "model_root_path": state["inputs"]["model_root_path"],
        "draft_model_root_path": state["inputs"].get("draft_model_root_path", ""),
        "launch_command_file": state["inputs"]["launch_command_file"],
        "input_resolution_path": str(workspace_dir / "input" / "input_resolution.json"),
        "p0_mode": "graph_decode_spec_v2",
        "strict_code_mapping_allowed": True,
    }

    dump_json(workspace_dir / "input" / "input_contract.json", input_contract)
    dump_json(workspace_dir / "input" / "source_inventory.json", source_inventory)

    artifacts = state["artifacts"]
    artifacts["raw_trace_path"] = source_inventory["raw_trace_path"]
    artifacts["raw_kernel_csv_path"] = source_inventory["raw_kernel_csv_path"]
    artifacts["raw_operator_details_path"] = source_inventory["raw_operator_details_path"]
    artifacts["raw_task_time_paths"] = source_inventory["raw_task_time_paths"]
    artifacts["raw_op_summary_paths"] = source_inventory["raw_op_summary_paths"]
    artifacts["framework_python_tracer_hash_path"] = source_inventory["framework_python_tracer_hash_path"]
    artifacts["framework_python_tracer_func_path"] = source_inventory["framework_python_tracer_func_path"]

    state["flags"]["input_contract_valid"] = True
    state["flags"]["raw_profiling_discovered"] = True
    save_state(workspace_dir, state)
    return {
        "input_contract_path": str(workspace_dir / "input" / "input_contract.json"),
        "source_inventory_path": str(workspace_dir / "input" / "source_inventory.json"),
    }


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    discover_inputs_for_workspace(workspace_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
