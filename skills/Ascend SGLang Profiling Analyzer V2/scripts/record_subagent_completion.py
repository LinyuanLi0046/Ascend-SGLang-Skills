from __future__ import annotations

import argparse
from pathlib import Path

from agent_contracts import AGENT_CONFIG
from workflow_common import (
    collect_existing_file_hashes,
    dispatch_completion_marker_path,
    dump_json,
    load_json,
    load_state,
    now_iso,
    save_state,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="记录一次真实子 agent 调用已经返回，可进入 finalize。")
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument("--agent-name", required=True, choices=sorted(AGENT_CONFIG))
    parser.add_argument(
        "--completion-source",
        default="task_subagent",
        choices=["task_subagent", "task_resume"],
        help="本次 completion 的来源，默认是一次普通 Task(...) 返回。",
    )
    parser.add_argument("--subagent-id", default="", help="可选；记录外部 Task 工具返回的 agent id。")
    parser.add_argument("--completion-note", default="", help="可选；记录这次子 agent 返回的补充说明。")
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    agent_slot = state.get("agents", {}).get(args.agent_name, {})
    dispatch_ready_path = str(agent_slot.get("dispatch_ready_path", "")).strip()
    ensure(dispatch_ready_path, f"{args.agent_name} 缺少 dispatch_ready_path，需先运行 prepare_agent_dispatch.py。")
    dispatch_path = Path(dispatch_ready_path)
    ensure(dispatch_path.exists(), f"{args.agent_name} dispatch 文件不存在: {dispatch_path}")
    dispatch_payload = load_json(dispatch_path)

    dispatch_id = str(dispatch_payload.get("dispatch_id", "")).strip()
    ensure(dispatch_id, f"{args.agent_name} dispatch 缺少 dispatch_id，需重新 prepare。")
    completion_marker_raw = str(dispatch_payload.get("completion_marker_path", "")).strip()
    completion_marker = Path(completion_marker_raw) if completion_marker_raw else dispatch_completion_marker_path(workspace_dir, args.agent_name)
    expected_output_files = [Path(str(item)) for item in dispatch_payload.get("output_files", []) if str(item).strip()]
    output_file_hashes = collect_existing_file_hashes(
        {f"output:{path.name}": path for path in expected_output_files if path.exists() and path.is_file()}
    )

    payload = {
        "schema_version": "subagent_completion_marker_v1",
        "agent_name": args.agent_name,
        "dispatch_id": dispatch_id,
        "step": int(dispatch_payload.get("step", 0) or 0),
        "subagent_type": str(dispatch_payload.get("subagent_type", "")).strip(),
        "query_snapshot_path": str(dispatch_payload.get("query_snapshot_path", "")).strip(),
        "completion_source": args.completion_source,
        "subagent_id": str(args.subagent_id or "").strip(),
        "completed_at": now_iso(),
        "status_claimed_by_subagent": "",
        "output_file_hashes": output_file_hashes,
        "completion_note": str(args.completion_note or "").strip()
        or "Task(...) 已返回主 agent，允许进入 finalize_agent_dispatch.py。",
    }
    dump_json(completion_marker, payload)

    agent_slot["last_completion_marker_path"] = str(completion_marker)
    agent_slot["last_completion_recorded_at"] = payload["completed_at"]
    orchestration = state.setdefault("orchestration", {})
    orchestration["last_subagent_completion_agent"] = args.agent_name
    orchestration["last_subagent_completion_at"] = payload["completed_at"]
    save_state(workspace_dir, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
