from __future__ import annotations

import argparse
from pathlib import Path

from step5_graph_bootstrap_plan import (
    BOOTSTRAP_TARGET,
    TARGET_SCRIPT_SEQUENCE,
    build_readiness_snapshot,
    step5_graph_bootstrap_lock_path,
    step5_graph_bootstrap_status_path,
)
from workflow_common import dump_json, load_json, load_state, run_child_script_with_logs, write_text


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行 Step 5A graph bootstrap runner。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    wrapper_script = SCRIPT_DIR / "run_step5_graph_bootstrap_pipeline.py"
    wrapper_metadata = run_child_script_with_logs(
        script_path=wrapper_script,
        workspace_dir=workspace_dir,
        repo_root=REPO_ROOT,
        log_prefix="step5a_graph_bootstrap_runner",
        heartbeat_seconds=30,
    )
    lock_path = step5_graph_bootstrap_lock_path(workspace_dir)
    status_path = step5_graph_bootstrap_status_path(workspace_dir)
    ensure(lock_path.exists(), f"graph bootstrap 完成后缺少 wrapper lock: {lock_path}")
    lock_payload = load_json(lock_path)
    ensure(
        str(lock_payload.get("status", "")).strip() == "passed",
        f"graph bootstrap wrapper 未以 passed 收口: {lock_payload.get('status')!r}",
    )
    state = load_state(workspace_dir)
    readiness = build_readiness_snapshot(state, BOOTSTRAP_TARGET)
    ensure(
        bool(readiness.get("ready")),
        "graph bootstrap wrapper 已结束，但 Step5A ready set 仍不完整："
        f" missing_artifacts={readiness.get('missing_artifacts', [])},"
        f" missing_flags={readiness.get('missing_flags', [])}",
    )
    result_path = workspace_dir / "output" / "graph_bootstrap_result.json"
    report_path = workspace_dir / "output" / "graph_bootstrap_report.md"
    result = {
        "status": "passed",
        "step": 5,
        "substep": "A",
        "bootstrap_target": BOOTSTRAP_TARGET,
        "expected_script_sequence": TARGET_SCRIPT_SEQUENCE[BOOTSTRAP_TARGET],
        "wrapper_lock_path": str(lock_path),
        "wrapper_lock_status": str(lock_payload.get("status", "")).strip(),
        "wrapper_status_path": str(status_path),
        "wrapper_status_exists": status_path.exists(),
        "wrapper_log_path": str(wrapper_metadata.get("combined_log_path", "")),
        "wrapper_meta_path": str(wrapper_metadata.get("metadata_path", "")),
        "current_or_final_stage": str(lock_payload.get("active_stage", "")).strip(),
        "required_artifacts_ready": bool(readiness.get("required_artifacts_ready")),
        "required_flags_ready": bool(readiness.get("required_flags_ready")),
        "ready_summary": readiness,
        "blocking_issues": [],
    }
    dump_json(result_path, result)
    report_lines = [
        "# Step 5A Graph Bootstrap Report",
        "",
        "- Status: passed",
        f"- Bootstrap target: {BOOTSTRAP_TARGET}",
        f"- Wrapper lock path: {lock_path}",
        f"- Wrapper log path: {wrapper_metadata.get('combined_log_path', '')}",
        f"- Wrapper meta path: {wrapper_metadata.get('metadata_path', '')}",
        f"- Final stage: {lock_payload.get('active_stage', '')}",
        f"- Required artifacts ready: {readiness.get('required_artifacts_ready')}",
        f"- Required flags ready: {readiness.get('required_flags_ready')}",
        f"- Missing artifacts: {readiness.get('missing_artifacts', [])}",
        f"- Missing flags: {readiness.get('missing_flags', [])}",
    ]
    write_text(report_path, "\n".join(report_lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
