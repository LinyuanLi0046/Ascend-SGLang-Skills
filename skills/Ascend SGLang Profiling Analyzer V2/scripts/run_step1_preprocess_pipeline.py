from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from workflow_common import load_json, load_state


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按固定顺序执行 Step 1 预处理流水线并做后验检查。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def log(message: str) -> None:
    print(f"[step1-wrapper] {message}", flush=True)


def run_script(script_name: str, workspace_dir: Path, stage_label: str) -> None:
    script_path = SCRIPT_DIR / script_name
    ensure(script_path.exists(), f"缺少子脚本: {script_path}")
    log(f"开始 {stage_label}: {script_name}")
    subprocess.run(
        [sys.executable, str(script_path), "--workspace-dir", str(workspace_dir)],
        cwd=REPO_ROOT,
        check=True,
    )
    log(f"完成 {stage_label}: {script_name}")


def ensure_state_artifact(workspace_dir: Path, artifact_key: str, expected_path: Path) -> None:
    state = load_state(workspace_dir)
    actual_path = str(state.get("artifacts", {}).get(artifact_key, "")).strip()
    ensure(actual_path == str(expected_path), f"{artifact_key} 未正确回写到 state: {actual_path}")
    ensure(expected_path.exists() and expected_path.is_file(), f"{artifact_key} 对应文件不存在: {expected_path}")


def ensure_step1_outputs(workspace_dir: Path) -> None:
    result_path = workspace_dir / "output" / "preprocess_step1_result.json"
    report_path = workspace_dir / "output" / "preprocess_step1_report.md"
    tracer_index_path = workspace_dir / "artifacts" / "stacks" / "python_tracer_index.json"

    ensure(result_path.exists(), f"缺少 Step 1 正式 JSON: {result_path}")
    ensure(report_path.exists(), f"缺少 Step 1 正式报告: {report_path}")
    payload = load_json(result_path)
    status = str(payload.get("status", "")).strip()
    ensure(status == "passed", f"preprocess_step1_result.json.status 必须为 passed，当前为 {status!r}")
    ensure_state_artifact(workspace_dir, "python_tracer_index_path", tracer_index_path)

    state = load_state(workspace_dir)
    ensure(bool(state.get("flags", {}).get("slicing_done")), "slicing_done 未置为 true。")
    ensure(bool(state.get("flags", {}).get("python_tracer_index_built")), "python_tracer_index_built 未置为 true。")


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    log(f"Step 1 wrapper 启动，workspace={workspace_dir}")

    trace_slice_path = workspace_dir / "artifacts" / "slices" / "trace_slice.json"
    kernel_slice_path = workspace_dir / "artifacts" / "slices" / "kernel_details_slice.csv"
    operator_slice_path = workspace_dir / "artifacts" / "slices" / "operator_details_slice.csv"
    task_time_slice_path = workspace_dir / "artifacts" / "slices" / "task_time_slice.csv"
    op_summary_slice_path = workspace_dir / "artifacts" / "slices" / "op_summary_slice.csv"

    run_script("slice_trace_workspace.py", workspace_dir, "trace 切片")
    ensure_state_artifact(workspace_dir, "trace_slice_path", trace_slice_path)

    run_script("slice_kernel_workspace.py", workspace_dir, "kernel 切片")
    ensure_state_artifact(workspace_dir, "kernel_slice_path", kernel_slice_path)

    run_script("slice_operator_details.py", workspace_dir, "operator 详情切片")
    ensure_state_artifact(workspace_dir, "operator_slice_path", operator_slice_path)

    run_script("slice_task_time_csv.py", workspace_dir, "task_time 切片")
    ensure_state_artifact(workspace_dir, "task_time_slice_path", task_time_slice_path)

    run_script("slice_op_summary_csv.py", workspace_dir, "op_summary 切片")
    ensure_state_artifact(workspace_dir, "op_summary_slice_path", op_summary_slice_path)

    run_script("write_preprocess_step1_outputs.py", workspace_dir, "Step 1 正式结果写出")
    ensure_step1_outputs(workspace_dir)
    log("Step 1 wrapper 全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
