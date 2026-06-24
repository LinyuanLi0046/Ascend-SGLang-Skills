from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from step4_bootstrap_plan import TARGET_SCRIPT_SEQUENCE, step4_bootstrap_lock_path, step4_bootstrap_status_path
from workflow_common import child_run_logs_dir, dump_json, load_json, load_state, now_iso, run_child_script_with_logs


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
LOCK_SCHEMA_VERSION = "step4_bootstrap_lock_v1"
STATUS_SCHEMA_VERSION = "step4_bootstrap_status_v1"
VALID_TARGETS = {"step4_stack_mapper"}
VALID_SPEC_MODES = {"spec_v2", "decode_graph", "disabled"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行 Step4 bootstrap wrapper。")
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument("--target", required=True, choices=sorted(VALID_TARGETS))
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def log(message: str) -> None:
    print(f"[step4-wrapper] {message}", flush=True)


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
    target: str,
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
        step4_bootstrap_status_path(workspace_dir),
        {
            "schema_version": STATUS_SCHEMA_VERSION,
            "status": status,
            "pid": os.getpid(),
            "workspace_dir": str(workspace_dir),
            "bootstrap_target": target,
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


def acquire_wrapper_lock(workspace_dir: Path, target: str) -> Path:
    lock_path = step4_bootstrap_lock_path(workspace_dir)
    existing = read_lock_payload(lock_path)
    if existing.get("status") == "running":
        existing_pid = int(existing.get("pid", 0) or 0)
        existing_target = str(existing.get("bootstrap_target", "")).strip()
        if process_is_alive(existing_pid) and existing_pid != os.getpid():
            raise RuntimeError(
                "检测到已有活跃的 Step4 bootstrap wrapper 正在运行；禁止重跑。"
                f" active_pid={existing_pid}, target={existing_target or '<unknown>'}, lock={lock_path}"
            )
    write_lock_payload(
        lock_path,
        {
            "schema_version": LOCK_SCHEMA_VERSION,
            "status": "running",
            "pid": os.getpid(),
            "workspace_dir": str(workspace_dir),
            "bootstrap_target": target,
            "started_at": now_iso(),
            "last_heartbeat_at": now_iso(),
            "active_stage": "initializing",
            "stage_phase": "initializing",
            "script_index": 0,
            "total_scripts": len(TARGET_SCRIPT_SEQUENCE[target]),
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
        target=target,
        status="running",
        active_stage="initializing",
        stage_phase="initializing",
        script_index=0,
        total_scripts=len(TARGET_SCRIPT_SEQUENCE[target]),
        current_child_script="",
        current_child_log_path="",
        last_child_meta_path="",
    )
    return lock_path


def update_wrapper_lock(
    lock_path: Path,
    *,
    target: str,
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
            "bootstrap_target": target,
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
    target: str,
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
            "bootstrap_target": target,
            "active_stage": active_stage,
            "stage_phase": stage_phase,
            "last_heartbeat_at": now_iso(),
            "ended_at": now_iso(),
            "script_index": script_index,
            "total_scripts": total_scripts,
        }
    )
    write_lock_payload(lock_path, payload)


def require_state_artifact_path(state: dict, artifact_key: str, label: str) -> Path:
    raw_path = str(state.get("artifacts", {}).get(artifact_key, "")).strip()
    ensure(raw_path, f"缺少 state.artifacts.{artifact_key}，说明 {label} 未正确回写。")
    path = Path(raw_path)
    ensure(path.exists() and path.is_file(), f"{label} 不存在或不是文件: {path}")
    return path


def ensure_flag(state: dict, flag_key: str) -> None:
    ensure(bool(state.get("flags", {}).get(flag_key)), f"{flag_key} 未置为 true。")


def ensure_repo_divergence_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    require_state_artifact_path(state, "repo_divergence_report_path", "repo_divergence_report.json")
    ensure_flag(state, "repo_divergence_checked")


def ensure_runtime_constraints_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    runtime_constraints_path = require_state_artifact_path(state, "runtime_constraints_path", "runtime_constraints.json")
    runtime_constraints = load_json(runtime_constraints_path)
    spec_mode = str(runtime_constraints.get("spec_mode", "")).strip()
    ensure(
        spec_mode in VALID_SPEC_MODES,
        f"runtime_constraints.json 缺少有效 spec_mode，当前为 {spec_mode or '<missing>'}。",
    )
    ensure_flag(state, "runtime_constraints_built")


def ensure_stack_evidence_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    require_state_artifact_path(state, "stack_evidence_path", "stack_evidence.json")
    require_state_artifact_path(state, "stack_evidence_lite_path", "stack_evidence_lite.json")
    ensure_flag(state, "stack_evidence_built")


def ensure_graph_phase_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    require_state_artifact_path(state, "graph_phase_stack_evidence_path", "graph_phase_stack_evidence.json")
    ensure_flag(state, "graph_phase_stack_evidence_built")


def ensure_graph_execution_plan_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    require_state_artifact_path(state, "graph_execution_plan_path", "graph_execution_plan.json")


def ensure_graph_mapping_targets_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    require_state_artifact_path(state, "graph_mapping_targets_path", "graph_mapping_targets.json")
    ensure_flag(state, "graph_mapping_targets_built")


def ensure_external_mapping_targets_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    require_state_artifact_path(state, "external_mapping_targets_path", "external_mapping_targets.json")
    ensure_flag(state, "external_mapping_targets_built")


def ensure_stack_call_paths_ready(workspace_dir: Path) -> None:
    state = load_state(workspace_dir)
    require_state_artifact_path(state, "stack_call_paths_path", "stack_call_paths.json")
    ensure_flag(state, "stack_call_paths_built")


POST_STAGE_CHECKS = {
    "check_repo_divergence.py": ensure_repo_divergence_ready,
    "build_runtime_constraints.py": ensure_runtime_constraints_ready,
    "build_stack_evidence.py": ensure_stack_evidence_ready,
    "build_graph_phase_stack_evidence.py": ensure_graph_phase_ready,
    "classify_graph_groups.py": ensure_graph_execution_plan_ready,
    "build_graph_mapping_targets.py": ensure_graph_mapping_targets_ready,
    "build_external_mapping_targets.py": ensure_external_mapping_targets_ready,
    "build_stack_call_paths.py": ensure_stack_call_paths_ready,
}


def run_stage(
    workspace_dir: Path,
    lock_path: Path,
    target: str,
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
        target=target,
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
    write_bootstrap_status(
        workspace_dir,
        target=target,
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
            target=target,
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
            target=target,
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

    log(f"[{script_index}/{total_scripts}] {script_name} 已启动，进入 child_running；后续进度以 child heartbeat/log 为准")
    update_wrapper_lock(
        lock_path,
        target=target,
        active_stage=f"{stage_token}_running",
        stage_phase="child_running",
        script_index=script_index,
        total_scripts=total_scripts,
        current_child_script=script_name,
    )
    write_bootstrap_status(
        workspace_dir,
        target=target,
        status="running",
        active_stage=f"{stage_token}_running",
        stage_phase="child_running",
        script_index=script_index,
        total_scripts=total_scripts,
        current_child_script=script_name,
        current_child_log_path="",
        last_child_meta_path="",
    )
    metadata = run_child_script_with_logs(
        script_path=script_path,
        workspace_dir=workspace_dir,
        repo_root=REPO_ROOT,
        log_prefix=f"step4_{target}_{stage_token}",
        heartbeat_seconds=30,
        on_heartbeat=_on_child_heartbeat,
    )
    log(f"[{script_index}/{total_scripts}] {script_name} 子进程已退出，开始 post-check")
    update_wrapper_lock(
        lock_path,
        target=target,
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
        target=target,
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
    post_check = POST_STAGE_CHECKS.get(script_name)
    if post_check is not None:
        post_check(workspace_dir)
    log(f"[{script_index}/{total_scripts}] {script_name} post-check 通过，阶段完成")
    update_wrapper_lock(
        lock_path,
        target=target,
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
        target=target,
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
    target = str(args.target).strip()
    start_ts = time.perf_counter()
    total_scripts = len(TARGET_SCRIPT_SEQUENCE[target])
    lock_path = acquire_wrapper_lock(workspace_dir, target)
    log(
        "Step4 bootstrap wrapper 启动，"
        f"target={target}, workspace={workspace_dir}, lock={lock_path}, "
        f"status_file={step4_bootstrap_status_path(workspace_dir)}, total_scripts={total_scripts}"
    )
    try:
        for script_index, script_name in enumerate(TARGET_SCRIPT_SEQUENCE[target], start=1):
            run_stage(
                workspace_dir,
                lock_path,
                target,
                script_name,
                script_index=script_index,
                total_scripts=total_scripts,
            )
        finalize_wrapper_lock(
            lock_path,
            target,
            "passed",
            active_stage="completed",
            stage_phase="completed",
            script_index=total_scripts,
            total_scripts=total_scripts,
        )
        write_bootstrap_status(
            workspace_dir,
            target=target,
            status="passed",
            active_stage="completed",
            stage_phase="completed",
            script_index=total_scripts,
            total_scripts=total_scripts,
            current_child_script="",
            current_child_log_path="",
            last_child_meta_path="",
        )
        log(f"Step4 bootstrap wrapper 完成，总耗时 {time.perf_counter() - start_ts:.2f}s")
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
            target,
            "failed",
            active_stage=failure_stage,
            stage_phase="failed",
            script_index=failure_script_index,
            total_scripts=total_scripts,
        )
        write_bootstrap_status(
            workspace_dir,
            target=target,
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
