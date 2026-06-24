from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from workflow_common import load_state, save_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="基于 workspace 状态切片 kernel_details.csv。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    input_path = Path(state["artifacts"]["raw_kernel_csv_path"])
    output_path = workspace_dir / "artifacts" / "slices" / "kernel_details_slice.csv"
    script_path = Path(__file__).with_name("slice_kernel_csv.py")

    cmd = [
        sys.executable,
        str(script_path),
        str(input_path),
        str(output_path),
        "--start-ns",
        str(int(state["inputs"]["window_start_ns"])),
        "--end-ns",
        str(int(state["inputs"]["window_end_ns"])),
        "--add-effective-columns",
    ]
    subprocess.run(cmd, check=True)
    state["artifacts"]["kernel_slice_path"] = str(output_path)
    save_state(workspace_dir, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
