from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from workflow_common import load_json, load_state


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按固定顺序执行 Step 2 预处理流水线并做后验检查。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run_script(script_name: str, workspace_dir: Path) -> None:
    script_path = SCRIPT_DIR / script_name
    ensure(script_path.exists(), f"缺少子脚本: {script_path}")
    subprocess.run(
        [sys.executable, str(script_path), "--workspace-dir", str(workspace_dir)],
        cwd=REPO_ROOT,
        check=True,
    )


def ensure_state_artifact(workspace_dir: Path, artifact_key: str, expected_path: Path) -> None:
    state = load_state(workspace_dir)
    actual_path = str(state.get("artifacts", {}).get(artifact_key, "")).strip()
    ensure(actual_path == str(expected_path), f"{artifact_key} 未正确回写到 state: {actual_path}")
    ensure(expected_path.exists() and expected_path.is_file(), f"{artifact_key} 对应文件不存在: {expected_path}")


def ensure_step2_outputs(workspace_dir: Path) -> None:
    result_path = workspace_dir / "output" / "preprocess_step2_result.json"
    report_path = workspace_dir / "output" / "preprocess_step2_report.md"

    ensure(result_path.exists(), f"缺少 Step 2 正式 JSON: {result_path}")
    ensure(report_path.exists(), f"缺少 Step 2 正式报告: {report_path}")
    payload = load_json(result_path)
    status = str(payload.get("status", "")).strip()
    ensure(status == "passed", f"preprocess_step2_result.json.status 必须为 passed，当前为 {status!r}")

    state = load_state(workspace_dir)
    ensure(bool(state.get("flags", {}).get("timeline_index_built")), "timeline_index_built 未置为 true。")


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)

    timeline_index_path = workspace_dir / "artifacts" / "index" / "timeline_index.json"

    run_script("build_timeline_index.py", workspace_dir)
    ensure_state_artifact(workspace_dir, "timeline_index_path", timeline_index_path)

    run_script("write_preprocess_step2_outputs.py", workspace_dir)
    ensure_state_artifact(workspace_dir, "timeline_index_path", timeline_index_path)
    ensure_step2_outputs(workspace_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
