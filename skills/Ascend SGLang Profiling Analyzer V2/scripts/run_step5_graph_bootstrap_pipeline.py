from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from step5_graph_bootstrap_plan import (
    BOOTSTRAP_TARGET,
    TARGET_SCRIPT_SEQUENCE,
    step5_graph_bootstrap_lock_path,
    step5_graph_bootstrap_status_path,
)
from workflow_common import dump_json, load_json, now_iso, run_child_script_with_logs


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
LOCK_SCHEMA_VERSION = "step5_graph_bootstrap_lock_v1"
STATUS_SCHEMA_VERSION = "step5_graph_bootstrap_status_v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行 Step5 graph bootstrap wrapper。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def log(message: str) -> None:
    print(f"[step5a-wrapper] {message}", flush=True)


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


def write_bootstrap_status(
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
) -> None:
    dump_json(
        step5_graph_bootstrap_status_path(workspace_dir),
        {
            "schema_version": STATUS_SCHEMA_VERSION,
            "status": status,
            "pid": os.getpid(),
            "workspace_dir": str(workspace_dir),
            "bootstrap_target": BOOTSTRAP_TARGET,
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
        },
    )


def acquire_wrapper_lock(workspace_dir: Path) -> Path:
    lock_path = step5_graph_bootstrap_lock_path(workspace_dir)
    existing = read_lock_payload(lock_path)
    if existing.get("status") == "running":
        existing_pid = int(existing.get("pid", 0) or 0)
        if process_is_alive(existing_pid) and existing_pid != os.getpid():
            raise RuntimeError(
                "检测到已有活跃的 Step5 graph bootstrap wrapper 正在运行；禁止重跑。"
                f" active_pid={existing_pid}, lock={lock_path}"
            )
    total_scripts = len(TARGET_SCRIPT_SEQUENCE[BOOTSTRAP_TARGET])
    write_lock_payload(
        lock_path,
        {
            "schema_version": LOCK_SCHEMA_VERSION,
            "status": "running",
            "pid": os.getpid(),
            "workspace_dir": str(workspace_dir),
            "bootstrap_target": BOOTSTRAP_TARGET,
            "started_at": now_iso(),
            "last_heartbeat_at": now_iso(),
            "active_stage": "initializing",
            "stage_phase": "initializing",
            "script_index": 0,
            "total_scripts": total_scripts,
            "completed_stages": [],
            "current_child_script": "",
            "current_child_log_path": "",
            "last_child_meta_path": "",
            "heartbeat_count": 0,
            "output_line_count": 0,
            "idle_seconds": 0.0,
        },
    )
    write_bootstrap_status(
        workspace_dir,
        status="running",
        active_stage="initializing",
        stage_phase="initializing",
        script_index=0,
        total_scripts=total_scripts,
        current_child_script="",
        current_child_log_path="",
        last_child_meta_path="",
    )
    return lock_path


def update_wrapper_lock(
    lock_path: Path,
    *,
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
) -> None:
    payload = read_lock_payload(lock_path)
    completed = payload.get("completed_stages", [])
    if not isinstance(completed, list):
        completed = []
    if completed_stage and completed_stage not in completed:
        completed.append(completed_stage)
    payload.update(
        {
            "schema_version": LOCK_SCHEMA_VERSION,
            "status": "running",
            "pid": os.getpid(),
            "bootstrap_target": BOOTSTRAP_TARGET,
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
    write_lock_payload(lock_path, payload)


def finalize_wrapper_lock(
    lock_path: Path,
    status: str,
    *,
    active_stage: str,
    stage_phase: str,
    script_index: int,
    total_scripts: int,
) -> None:
    payload = read_lock_payload(lock_path)
    payload.update(
        {
            "schema_version": LOCK_SCHEMA_VERSION,
            "status": status,
            "pid": os.getpid(),
            "bootstrap_target": BOOTSTRAP_TARGET,
            "active_stage": active_stage,
            "stage_phase": stage_phase,
            "last_heartbeat_at": now_iso(),
            "ended_at": now_iso(),
            "script_index": script_index,
            "total_scripts": total_scripts,
        }
    )
    write_lock_payload(lock_path, payload)


def require_artifact(path: Path, label: str) -> None:
    ensure(path.exists() and path.is_file(), f"{label} 不存在或不是文件: {path}")


def post_check(script_name: str, workspace_dir: Path) -> None:
    artifacts_dir = workspace_dir / "artifacts" / "graph"
    input_dir = workspace_dir / "input"
    if script_name == "build_graph_forward_context.py":
        require_artifact(artifacts_dir / "graph_forward_context.json", "graph_forward_context.json")
        return
    if script_name == "build_graph_seed_context.py":
        require_artifact(input_dir / "graph_seed_context.json", "graph_seed_context.json")
        return
    if script_name == "build_graph_operator_spans.py":
        require_artifact(artifacts_dir / "graph_operator_spans.json", "graph_operator_spans.json")


def run_stage(
    workspace_dir: Path,
    lock_path: Path,
    script_name: str,
    *,
    script_index: int,
    total_scripts: int,
) -> None:
    stage_token = Path(script_name).stem
    script_path = SCRIPT_DIR / script_name
    ensure(script_path.exists(), f"缺少子脚本: {script_path}")
    update_wrapper_lock(
        lock_path,
        active_stage=f"{stage_token}_running",
        stage_phase="launching_child",
        script_index=script_index,
        total_scripts=total_scripts,
        current_child_script=script_name,
        current_child_log_path="",
        last_child_meta_path="",
    )
    write_bootstrap_status(
        workspace_dir,
        status="running",
        active_stage=f"{stage_token}_running",
        stage_phase="launching_child",
        script_index=script_index,
        total_scripts=total_scripts,
        current_child_script=script_name,
        current_child_log_path="",
        last_child_meta_path="",
    )
    log(f"[{script_index}/{total_scripts}] 启动 {script_name} (phase=launching_child)")

    def _on_child_heartbeat(payload: dict) -> None:
        update_wrapper_lock(
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
        write_bootstrap_status(
            workspace_dir,
            status="running",
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
        log_prefix=f"step5a_{stage_token}",
        heartbeat_seconds=30,
        on_heartbeat=_on_child_heartbeat,
    )
    update_wrapper_lock(
        lock_path,
        active_stage=f"{stage_token}_post_check",
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
    write_bootstrap_status(
        workspace_dir,
        status="running",
        active_stage=f"{stage_token}_post_check",
        stage_phase="post_checking",
        script_index=script_index,
        total_scripts=total_scripts,
        current_child_script=script_name,
        current_child_log_path=str(metadata["combined_log_path"]),
        last_child_meta_path=str(metadata["metadata_path"]),
        heartbeat_count=int(metadata.get("heartbeat_count", 0) or 0),
        output_line_count=int(metadata.get("output_line_count", 0) or 0),
    )
    post_check(script_name, workspace_dir)
    update_wrapper_lock(
        lock_path,
        active_stage=f"{stage_token}_done",
        stage_phase="post_check_done",
        script_index=script_index,
        total_scripts=total_scripts,
        completed_stage=script_name,
        current_child_script=script_name,
        current_child_log_path=str(metadata["combined_log_path"]),
        last_child_meta_path=str(metadata["metadata_path"]),
        heartbeat_count=int(metadata.get("heartbeat_count", 0) or 0),
        output_line_count=int(metadata.get("output_line_count", 0) or 0),
        idle_seconds=0.0,
    )
    write_bootstrap_status(
        workspace_dir,
        status="running",
        active_stage=f"{stage_token}_done",
        stage_phase="post_check_done",
        script_index=script_index,
        total_scripts=total_scripts,
        current_child_script=script_name,
        current_child_log_path=str(metadata["combined_log_path"]),
        last_child_meta_path=str(metadata["metadata_path"]),
        heartbeat_count=int(metadata.get("heartbeat_count", 0) or 0),
        output_line_count=int(metadata.get("output_line_count", 0) or 0),
    )
    log(
        f"[{script_index}/{total_scripts}] 完成 {script_name} "
        f"(耗时 {metadata['duration_seconds']:.2f}s, 输出 {metadata['output_line_count']} 行, "
        f"log={metadata['combined_log_path']})"
    )


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    start_ts = time.perf_counter()
    total_scripts = len(TARGET_SCRIPT_SEQUENCE[BOOTSTRAP_TARGET])
    lock_path = acquire_wrapper_lock(workspace_dir)
    log(
        "Step5 graph bootstrap wrapper 启动，"
        f"workspace={workspace_dir}, lock={lock_path}, "
        f"status_file={step5_graph_bootstrap_status_path(workspace_dir)}, total_scripts={total_scripts}"
    )
    try:
        for script_index, script_name in enumerate(TARGET_SCRIPT_SEQUENCE[BOOTSTRAP_TARGET], start=1):
            run_stage(
                workspace_dir,
                lock_path,
                script_name,
                script_index=script_index,
                total_scripts=total_scripts,
            )
        finalize_wrapper_lock(
            lock_path,
            "passed",
            active_stage="completed",
            stage_phase="completed",
            script_index=total_scripts,
            total_scripts=total_scripts,
        )
        write_bootstrap_status(
            workspace_dir,
            status="passed",
            active_stage="completed",
            stage_phase="completed",
            script_index=total_scripts,
            total_scripts=total_scripts,
            current_child_script="",
            current_child_log_path="",
            last_child_meta_path="",
        )
        log(f"Step5 graph bootstrap wrapper 完成，总耗时 {time.perf_counter() - start_ts:.2f}s")
        return 0
    except Exception:
        failure_payload = read_lock_payload(lock_path)
        failure_stage = str(failure_payload.get("active_stage", "failed")).strip() or "failed"
        failure_script_index = int(failure_payload.get("script_index", 0) or 0)
        failure_current_child = str(failure_payload.get("current_child_script", "")).strip()
        failure_child_log = str(failure_payload.get("current_child_log_path", "")).strip()
        failure_meta = str(failure_payload.get("last_child_meta_path", "")).strip()
        finalize_wrapper_lock(
            lock_path,
            "failed",
            active_stage=failure_stage,
            stage_phase="failed",
            script_index=failure_script_index,
            total_scripts=total_scripts,
        )
        write_bootstrap_status(
            workspace_dir,
            status="failed",
            active_stage=failure_stage,
            stage_phase="failed",
            script_index=failure_script_index,
            total_scripts=total_scripts,
            current_child_script=failure_current_child,
            current_child_log_path=failure_child_log,
            last_child_meta_path=failure_meta,
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
