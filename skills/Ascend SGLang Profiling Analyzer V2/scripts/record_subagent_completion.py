from __future__ import annotations

import argparse
import os
from pathlib import Path

from agent_contracts import AGENT_CONFIG
from workflow_common import (
    child_run_logs_dir,
    collect_existing_file_hashes,
    compute_sha256,
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
    parser.add_argument("--task-call-id", default="", help="建议填写；记录本次 Task(...) 返回的调用/agent id。")
    parser.add_argument("--subagent-id", default="", help="可选；记录外部 Task 工具返回的 agent id。")
    parser.add_argument("--completion-note", default="", help="可选；记录这次子 agent 返回的补充说明。")
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


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


def _collect_wrapper_completion_guard(workspace_dir: Path, agent_name: str, step: int) -> dict:
    spec = _wrapper_lock_path(workspace_dir, agent_name, step)
    if spec is None:
        return {}
    lock_path, step_label = spec
    ensure(lock_path.exists(), f"{agent_name} completion 前缺少 {step_label} wrapper lock: {lock_path}")
    lock_payload = load_json(lock_path)
    lock_status = str(lock_payload.get("status", "")).strip()
    pid = int(lock_payload.get("pid", 0) or 0)
    pid_alive = _process_is_alive(pid)
    ensure(
        lock_status in {"passed", "failed"},
        f"{agent_name} completion 前 {step_label} wrapper lock 必须已收口为 passed/failed，当前为 {lock_status!r}。"
        f" pid={pid}, pid_alive={pid_alive}, lock={lock_path}",
    )
    return {
        "wrapper_lock_path": str(lock_path),
        "wrapper_lock_status": lock_status,
        "wrapper_lock_pid": pid,
        "wrapper_lock_pid_alive": pid_alive,
        "wrapper_lock_ended_at": str(lock_payload.get("ended_at", "")).strip(),
    }


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
    step = int(dispatch_payload.get("step", 0) or 0)
    substep = str(dispatch_payload.get("substep", "")).strip().upper()
    query_snapshot_path_raw = str(dispatch_payload.get("query_snapshot_path", "")).strip()
    ensure(query_snapshot_path_raw, f"{args.agent_name} dispatch 缺少 query_snapshot_path，需重新 prepare。")
    query_snapshot_path = Path(query_snapshot_path_raw)
    ensure(query_snapshot_path.exists(), f"{args.agent_name} query snapshot 不存在: {query_snapshot_path}")
    query_snapshot_sha256 = compute_sha256(query_snapshot_path)
    dispatch_query_snapshot_sha256 = str(dispatch_payload.get("query_snapshot_sha256", "")).strip()
    if dispatch_query_snapshot_sha256:
        ensure(
            dispatch_query_snapshot_sha256 == query_snapshot_sha256,
            f"{args.agent_name} query snapshot 哈希与 dispatch 不一致，需重新 prepare。",
        )
    completion_marker_raw = str(dispatch_payload.get("completion_marker_path", "")).strip()
    completion_marker = Path(completion_marker_raw) if completion_marker_raw else dispatch_completion_marker_path(workspace_dir, args.agent_name)
    expected_output_files = [Path(str(item)) for item in dispatch_payload.get("output_files", []) if str(item).strip()]
    output_file_hashes = collect_existing_file_hashes(
        {f"output:{path.name}": path for path in expected_output_files if path.exists() and path.is_file()}
    )
    task_call_id = str(args.task_call_id or "").strip()
    subagent_id = str(args.subagent_id or "").strip()
    task_receipt_required = bool(dispatch_payload.get("task_receipt_required", False))
    if task_receipt_required:
        ensure(task_call_id or subagent_id, f"{args.agent_name} completion 需要记录 task_call_id 或 subagent_id。")
    if not subagent_id and task_call_id:
        subagent_id = task_call_id
    if not task_call_id and subagent_id:
        task_call_id = subagent_id
    allowed_official_scripts = [
        str(item).strip()
        for item in dispatch_payload.get("allowed_official_scripts", [])
        if str(item).strip()
    ]
    completion_guard = _collect_wrapper_completion_guard(workspace_dir, args.agent_name, step)
    default_completion_note = "Task(...) 已返回主 agent，允许进入 finalize_agent_dispatch.py。"
    if completion_guard:
        default_completion_note = (
            "Task(...) 已返回主 agent，且对应 wrapper lock 已收口；允许进入 finalize_agent_dispatch.py。"
        )

    payload = {
        "schema_version": "subagent_completion_marker_v2",
        "agent_name": args.agent_name,
        "dispatch_id": dispatch_id,
        "step": step,
        "substep": substep,
        "subagent_type": str(dispatch_payload.get("subagent_type", "")).strip(),
        "query_snapshot_path": str(query_snapshot_path),
        "query_snapshot_sha256": query_snapshot_sha256,
        "task_required": bool(dispatch_payload.get("task_required", True)),
        "task_receipt_required": task_receipt_required,
        "completion_source": args.completion_source,
        "task_call_id": task_call_id,
        "subagent_id": subagent_id,
        "main_agent_role": str(dispatch_payload.get("main_agent_role", "")).strip(),
        "subagent_role": str(dispatch_payload.get("subagent_role", "")).strip(),
        "allowed_official_scripts": allowed_official_scripts,
        "completed_at": now_iso(),
        "status_claimed_by_subagent": "",
        "output_file_hashes": output_file_hashes,
        "completion_note": str(args.completion_note or "").strip() or default_completion_note,
    }
    payload.update(completion_guard)
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
