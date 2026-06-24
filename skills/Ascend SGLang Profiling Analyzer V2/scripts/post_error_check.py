from __future__ import annotations

import argparse
from pathlib import Path

from workflow_common import load_json, load_state, now_iso, save_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查 debug 产物并决定是否允许重试。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    error_context_path = workspace_dir / "input" / "error_context.json"
    ensure(error_context_path.exists(), "缺少 input/error_context.json。")

    fix_path_str = state["artifacts"].get("fix_instructions_path", "")
    if not fix_path_str:
        fix_path_str = str(workspace_dir / "output" / "fix_instructions.json")
    fix_path = Path(fix_path_str)
    ensure(fix_path.exists(), f"缺少 fix_instructions.json: {fix_path}")

    error_context = load_json(error_context_path)
    fix_instructions = load_json(fix_path)
    ensure(bool(fix_instructions.get("actions")), "fix_instructions.json 需要至少一条 actions。")

    debug_state = state.setdefault("debug", {})
    failed_key = str(error_context.get("failed_step", "unknown"))
    retry_by_step = debug_state.setdefault("retry_by_step", {})
    retry_count = int(retry_by_step.get(failed_key, 0)) + 1
    retry_by_step[failed_key] = retry_count
    ensure(retry_count <= 3, f"step {failed_key} 的自动 debug 重试次数已超过 3 次。")

    state["artifacts"]["fix_instructions_path"] = str(fix_path)
    state["flags"]["debug_fix_pending"] = False
    orchestration = state.setdefault("orchestration", {})
    orchestration["active_agent"] = ""
    orchestration["active_dispatch_path"] = ""
    orchestration["active_dispatch_id"] = ""
    orchestration["active_completion_marker_path"] = ""
    orchestration["dispatch_temp_script_baseline"] = []
    if failed_key == "final_gate":
        state["status"] = "awaiting_final_gate"
        state["next_action"] = "run_final_gate"
    else:
        state["status"] = "ready_to_retry"
        state["next_action"] = f"retry_{failed_key}"
    state.setdefault("step_history", []).append(
        {
            "step": "post_error_check",
            "name": "POST_ERROR_CHECK",
            "status": "completed",
            "at": now_iso(),
        }
    )
    save_state(workspace_dir, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
