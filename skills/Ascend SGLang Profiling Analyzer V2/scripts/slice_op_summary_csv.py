from __future__ import annotations

import argparse
import csv
from pathlib import Path

from workflow_common import load_json, load_state, save_state


START_KEYS = ["Task Start Time(us)", "Start Time(us)"]
DURATION_KEYS = ["Task Duration(us)", "Duration(us)"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="合并并切片 op_summary_*.csv。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def parse_us_to_ns(value: str) -> int:
    return int(float(value) * 1000)


def infer_times(row: dict[str, str]) -> tuple[int | None, int | None]:
    start_raw = next((row.get(key, "").strip() for key in START_KEYS if row.get(key, "").strip()), "")
    dur_raw = next((row.get(key, "").strip() for key in DURATION_KEYS if row.get(key, "").strip()), "")
    if not start_raw or not dur_raw:
        return None, None
    start_ns = parse_us_to_ns(start_raw)
    dur_ns = parse_us_to_ns(dur_raw)
    return start_ns, start_ns + dur_ns


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    inventory = load_json(workspace_dir / "input" / "source_inventory.json")
    source_paths = [Path(item) for item in inventory["raw_op_summary_paths"]]
    output_path = workspace_dir / "artifacts" / "slices" / "op_summary_slice.csv"
    window_start_ns = int(state["inputs"]["window_start_ns"])
    window_end_ns = int(state["inputs"]["window_end_ns"])

    union_fieldnames: list[str] = []
    rows: list[dict[str, str]] = []
    for source_path in source_paths:
        with source_path.open("r", encoding="utf-8-sig", newline="") as src:
            reader = csv.DictReader(src)
            fieldnames = list(reader.fieldnames or [])
            for field in fieldnames:
                if field not in union_fieldnames:
                    union_fieldnames.append(field)
            for row in reader:
                rows.append({"__source_file__": source_path.name, **row})

    extra_fields = ["slice_row_id", "source_file", "start_ns", "end_ns", "dur_ns"]
    with output_path.open("w", encoding="utf-8", newline="") as dst:
        writer = csv.DictWriter(dst, fieldnames=union_fieldnames + extra_fields)
        writer.writeheader()
        kept = 0
        for row in rows:
            start_ns, end_ns = infer_times(row)
            if start_ns is None or end_ns is None:
                continue
            if end_ns < window_start_ns or start_ns > window_end_ns:
                continue
            kept += 1
            payload = {field: row.get(field, "") for field in union_fieldnames}
            payload["slice_row_id"] = f"{kept:06d}"
            payload["source_file"] = row["__source_file__"]
            payload["start_ns"] = str(start_ns)
            payload["end_ns"] = str(end_ns)
            payload["dur_ns"] = str(end_ns - start_ns)
            writer.writerow(payload)

    state["artifacts"]["op_summary_slice_path"] = str(output_path)
    save_state(workspace_dir, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
