from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from workflow_common import child_run_logs_dir, dump_json, load_json, now_iso, run_child_script_with_logs


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
LOCK_SCHEMA_VERSION = "step3_wrapper_lock_v2"
STATUS_SCHEMA_VERSION = "step3_wrapper_status_v1"
TOTAL_SCRIPTS = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按固定顺序执行 Step 3 base 分析流水线。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def log(message: str) -> None:
    print(f"[step3-wrapper] {message}", flush=True)


def lock_path_for_workspace(workspace_dir: Path) -> Path:
    return child_run_logs_dir(workspace_dir) / "step3_wrapper.lock.json"


def status_path_for_workspace(workspace_dir: Path) -> Path:
    return workspace_dir / "audit" / "step3_in_progress.json"


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_lock_payload(lock_path: Path) -> dict:
    if not lock_path.exists():
        return {}
    try:
        return load_json(lock_path)
    except Exception:
        return {}


def write_lock_payload(lock_path: Path, payload: dict) -> None:
    dump_json(lock_path, payload)


def write_wrapper_status(
    workspace_dir: Path,
    *,
    status: str,
    active_stage: str,
    stage_phase: str,
    script_index: int,
    total_scripts: int,
    current_child_script: str,
    current_child_log_path: str,
    last_child_meta_path: str,
    heartbeat_count: int = 0,
    output_line_count: int = 0,
    idle_seconds: float = 0.0,
    completed_stages: list[str] | None = None,
) -> None:
    dump_json(
        status_path_for_workspace(workspace_dir),
        {
            "schema_version": STATUS_SCHEMA_VERSION,
            "status": status,
            "pid": os.getpid(),
            "workspace_dir": str(workspace_dir),
            "active_stage": active_stage,
            "stage_phase": stage_phase,
            "script_index": script_index,
            "total_scripts": total_scripts,
            "current_child_script": current_child_script,
            "current_child_log_path": current_child_log_path,
            "last_child_meta_path": last_child_meta_path,
            "last_heartbeat_at": now_iso(),
            "heartbeat_count": heartbeat_count,
            "output_line_count": output_line_count,
            "idle_seconds": round(idle_seconds, 6),
            "completed_stages": list(completed_stages or []),
        },
    )


def acquire_wrapper_lock(workspace_dir: Path) -> Path:
    lock_path = lock_path_for_workspace(workspace_dir)
    existing = read_lock_payload(lock_path)
    if existing.get("status") == "running":
        existing_pid = int(existing.get("pid", 0) or 0)
        if process_is_alive(existing_pid) and existing_pid != os.getpid():
            raise RuntimeError(
                "检测到已有活跃的 Step 3 wrapper 实例正在运行；禁止重跑。"
                f" active_pid={existing_pid}, lock={lock_path}"
            )
    payload = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "status": "running",
        "pid": os.getpid(),
        "workspace_dir": str(workspace_dir),
        "started_at": now_iso(),
        "last_heartbeat_at": now_iso(),
        "active_stage": "initializing",
        "stage_phase": "initializing",
        "script_index": 0,
        "total_scripts": TOTAL_SCRIPTS,
        "completed_stages": [],
        "current_child_script": "",
        "current_child_log_path": "",
        "last_child_meta_path": "",
        "heartbeat_count": 0,
        "output_line_count": 0,
        "idle_seconds": 0.0,
    }
    write_lock_payload(lock_path, payload)
    write_wrapper_status(
        workspace_dir,
        status="running",
        active_stage="initializing",
        stage_phase="initializing",
        script_index=0,
        total_scripts=TOTAL_SCRIPTS,
        current_child_script="",
        current_child_log_path="",
        last_child_meta_path="",
        completed_stages=[],
    )
    return lock_path


def update_wrapper_state(
    workspace_dir: Path,
    lock_path: Path,
    *,
    status: str = "running",
    active_stage: str | None = None,
    completed_stage: str | None = None,
    current_child_script: str | None = None,
    current_child_log_path: str | None = None,
    last_child_meta_path: str | None = None,
    stage_phase: str | None = None,
    script_index: int | None = None,
    total_scripts: int | None = None,
    heartbeat_count: int | None = None,
    output_line_count: int | None = None,
    idle_seconds: float | None = None,
) -> dict:
    payload = read_lock_payload(lock_path)
    completed = payload.get("completed_stages", [])
    if not isinstance(completed, list):
        completed = []
    if completed_stage and completed_stage not in completed:
        completed.append(completed_stage)
    payload.update(
        {
            "schema_version": LOCK_SCHEMA_VERSION,
            "status": status,
            "pid": os.getpid(),
            "workspace_dir": str(workspace_dir),
            "last_heartbeat_at": now_iso(),
            "completed_stages": completed,
        }
    )
    if active_stage is not None:
        payload["active_stage"] = active_stage
    if current_child_script is not None:
        payload["current_child_script"] = current_child_script
    if current_child_log_path is not None:
        payload["current_child_log_path"] = current_child_log_path
    if last_child_meta_path is not None:
        payload["last_child_meta_path"] = last_child_meta_path
    if stage_phase is not None:
        payload["stage_phase"] = stage_phase
    if script_index is not None:
        payload["script_index"] = script_index
    if total_scripts is not None:
        payload["total_scripts"] = total_scripts
    if heartbeat_count is not None:
        payload["heartbeat_count"] = heartbeat_count
    if output_line_count is not None:
        payload["output_line_count"] = output_line_count
    if idle_seconds is not None:
        payload["idle_seconds"] = round(idle_seconds, 6)
    if status != "running":
        payload["ended_at"] = now_iso()
    write_lock_payload(lock_path, payload)
    write_wrapper_status(
        workspace_dir,
        status=str(payload.get("status", status)).strip(),
        active_stage=str(payload.get("active_stage", "")).strip(),
        stage_phase=str(payload.get("stage_phase", "")).strip(),
        script_index=int(payload.get("script_index", 0) or 0),
        total_scripts=int(payload.get("total_scripts", TOTAL_SCRIPTS) or TOTAL_SCRIPTS),
        current_child_script=str(payload.get("current_child_script", "")).strip(),
        current_child_log_path=str(payload.get("current_child_log_path", "")).strip(),
        last_child_meta_path=str(payload.get("last_child_meta_path", "")).strip(),
        heartbeat_count=int(payload.get("heartbeat_count", 0) or 0),
        output_line_count=int(payload.get("output_line_count", 0) or 0),
        idle_seconds=float(payload.get("idle_seconds", 0.0) or 0.0),
        completed_stages=completed,
    )
    return payload


def run_script(
    script_name: str,
    workspace_dir: Path,
    lock_path: Path,
    extra_args: list[str],
    *,
    stage_token: str,
    script_index: int,
    total_scripts: int,
) -> dict:
    script_path = SCRIPT_DIR / script_name
    ensure(script_path.exists(), f"缺少子脚本: {script_path}")
    log(f"开始执行: {script_name}")
    update_wrapper_state(
        workspace_dir,
        lock_path,
        active_stage=f"{stage_token}_running",
        stage_phase="launching_child",
        script_index=script_index,
        total_scripts=total_scripts,
        current_child_script=script_name,
        current_child_log_path="",
        last_child_meta_path="",
        heartbeat_count=0,
        output_line_count=0,
        idle_seconds=0.0,
    )

    def _on_heartbeat(payload: dict) -> None:
        update_wrapper_state(
            workspace_dir,
            lock_path,
            active_stage=f"{stage_token}_running",
            stage_phase="child_running",
            script_index=script_index,
            total_scripts=total_scripts,
            current_child_script=script_name,
            current_child_log_path=str(payload.get("combined_log_path", "")),
            last_child_meta_path=str(payload.get("metadata_path", "")),
            heartbeat_count=int(payload.get("heartbeat_count", 0) or 0),
            output_line_count=int(payload.get("output_line_count", 0) or 0),
            idle_seconds=float(payload.get("idle_seconds", 0.0) or 0.0),
        )

    metadata = run_child_script_with_logs(
        script_path=script_path,
        workspace_dir=workspace_dir,
        repo_root=REPO_ROOT,
        log_prefix=f"step3_{Path(script_name).stem}",
        heartbeat_seconds=30,
        extra_args=extra_args,
        on_heartbeat=_on_heartbeat,
    )
    update_wrapper_state(
        workspace_dir,
        lock_path,
        active_stage=f"{stage_token}_post_checking",
        stage_phase="post_checking",
        script_index=script_index,
        total_scripts=total_scripts,
        current_child_script=script_name,
        current_child_log_path=str(metadata["combined_log_path"]),
        last_child_meta_path=str(metadata["metadata_path"]),
        heartbeat_count=int(metadata.get("heartbeat_count", 0) or 0),
        output_line_count=int(metadata.get("output_line_count", 0) or 0),
        idle_seconds=0.0,
    )
    log(
        f"完成执行: {script_name} "
        f"(耗时 {metadata['duration_seconds']:.2f}s, 输出 {metadata['output_line_count']} 行, "
        f"log={metadata['combined_log_path']})"
    )
    return metadata


def ensure_step3_base_outputs(workspace_dir: Path) -> None:
    classified_base_path = workspace_dir / "artifacts" / "classification" / "classified_spans.base.json"
    scope_gate_base_path = workspace_dir / "output" / "scope_gate_result.base.json"
    ensure(classified_base_path.exists(), f"缺少 Step 3 base classified 输出: {classified_base_path}")
    ensure(scope_gate_base_path.exists(), f"缺少 Step 3 base scope gate 输出: {scope_gate_base_path}")
    scope_gate_payload = load_json(scope_gate_base_path)
    ensure(
        str(scope_gate_payload.get("status", "")).strip() in {"passed", "failed"},
        "scope_gate_result.base.json.status 必须为 passed/failed。",
    )


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    lock_path = acquire_wrapper_lock(workspace_dir)
    start_ts = time.perf_counter()
    log(f"Step 3 wrapper 启动，workspace={workspace_dir}")

    classified_base_path = workspace_dir / "artifacts" / "classification" / "classified_spans.base.json"
    scope_gate_base_path = workspace_dir / "output" / "scope_gate_result.base.json"
    try:
        classify_meta = run_script(
            "classify_spans.py",
            workspace_dir,
            lock_path,
            [
                "--output-path",
                str(classified_base_path),
                "--write-state",
                "false",
            ],
            stage_token="classify_spans",
            script_index=1,
            total_scripts=TOTAL_SCRIPTS,
        )
        update_wrapper_state(
            workspace_dir,
            lock_path,
            active_stage="classify_spans_done",
            completed_stage="classify_spans",
            stage_phase="post_check_done",
            script_index=1,
            total_scripts=TOTAL_SCRIPTS,
            current_child_script="classify_spans.py",
            current_child_log_path=str(classify_meta["combined_log_path"]),
            last_child_meta_path=str(classify_meta["metadata_path"]),
            heartbeat_count=int(classify_meta.get("heartbeat_count", 0) or 0),
            output_line_count=int(classify_meta.get("output_line_count", 0) or 0),
            idle_seconds=0.0,
        )
        scope_gate_meta = run_script(
            "check_scope_gate.py",
            workspace_dir,
            lock_path,
            [
                "--classified-path",
                str(classified_base_path),
                "--output-path",
                str(scope_gate_base_path),
                "--write-state",
                "false",
            ],
            stage_token="check_scope_gate",
            script_index=2,
            total_scripts=TOTAL_SCRIPTS,
        )
        ensure_step3_base_outputs(workspace_dir)
        update_wrapper_state(
            workspace_dir,
            lock_path,
            status="passed",
            active_stage="done",
            completed_stage="check_scope_gate",
            stage_phase="completed",
            script_index=TOTAL_SCRIPTS,
            total_scripts=TOTAL_SCRIPTS,
            current_child_script="",
            current_child_log_path=str(scope_gate_meta["combined_log_path"]),
            last_child_meta_path=str(scope_gate_meta["metadata_path"]),
            heartbeat_count=int(scope_gate_meta.get("heartbeat_count", 0) or 0),
            output_line_count=int(scope_gate_meta.get("output_line_count", 0) or 0),
            idle_seconds=0.0,
        )
        log(f"Step 3 wrapper 全部完成，总耗时 {time.perf_counter() - start_ts:.2f}s")
        return 0
    except Exception:
        failure_payload = read_lock_payload(lock_path)
        update_wrapper_state(
            workspace_dir,
            lock_path,
            status="failed",
            active_stage="failed",
            stage_phase="failed",
            script_index=int(failure_payload.get("script_index", 0) or 0),
            total_scripts=int(failure_payload.get("total_scripts", TOTAL_SCRIPTS) or TOTAL_SCRIPTS),
            current_child_script=str(failure_payload.get("current_child_script", "")).strip(),
            current_child_log_path=str(failure_payload.get("current_child_log_path", "")).strip(),
            last_child_meta_path=str(failure_payload.get("last_child_meta_path", "")).strip(),
            heartbeat_count=int(failure_payload.get("heartbeat_count", 0) or 0),
            output_line_count=int(failure_payload.get("output_line_count", 0) or 0),
            idle_seconds=float(failure_payload.get("idle_seconds", 0.0) or 0.0),
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
