from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from typing import Any

from workflow_common import load_json, load_state, save_state


OP_SUMMARY_START_KEYS = ["Task Start Time(us)", "Start Time(us)"]
OP_SUMMARY_DURATION_KEYS = ["Task Duration(us)", "Duration(us)"]
OPERATOR_DURATION_KEYS = [
    "Device Total Duration(us)",
    "Device Self Duration(us)",
    "Device Duration(us)",
    "Host Total Duration(us)",
    "Host Self Duration(us)",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="切片 operator_details.csv。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def find_first(row: dict[str, str], keys: list[str]) -> str:
    for key in keys:
        value = str(row.get(key, "")).strip()
        if value:
            return value
    return ""


def parse_us_float(value: str) -> int:
    return int(float(str(value).strip().replace("\t", "")) * 1000)


def normalize_name(value: str) -> str:
    return " ".join(str(value).strip().lower().split())


def infer_op_summary_times(row: dict[str, str]) -> tuple[int | None, int | None]:
    start_raw = find_first(row, OP_SUMMARY_START_KEYS)
    dur_raw = find_first(row, OP_SUMMARY_DURATION_KEYS)
    if not start_raw or not dur_raw:
        return None, None
    start_ns = parse_us_float(start_raw)
    dur_ns = parse_us_float(dur_raw)
    return start_ns, start_ns + dur_ns


def infer_operator_duration_ns(row: dict[str, str]) -> int | None:
    duration_raw = find_first(row, OPERATOR_DURATION_KEYS)
    if not duration_raw:
        return None
    try:
        return parse_us_float(duration_raw)
    except ValueError:
        return None


def load_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as src:
        reader = csv.DictReader(src)
        fieldnames = list(reader.fieldnames or [])
        rows = [{key: str(value or "") for key, value in row.items()} for row in reader]
    return fieldnames, rows


def build_op_summary_matches(
    source_paths: list[Path],
    window_start_ns: int,
    window_end_ns: int,
) -> dict[str, list[dict[str, Any]]]:
    matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    global_row_id = 0
    for source_path in source_paths:
        _, rows = load_csv_rows(source_path)
        for row in rows:
            start_ns, end_ns = infer_op_summary_times(row)
            if start_ns is None or end_ns is None:
                continue
            if end_ns < window_start_ns or start_ns > window_end_ns:
                continue
            op_name = find_first(row, ["Op Name", "Name"])
            if not op_name:
                continue
            global_row_id += 1
            matches[normalize_name(op_name)].append(
                {
                    "op_name": op_name,
                    "task_id": find_first(row, ["Task ID", "Task Id", "task_id"]),
                    "stream_id": find_first(row, ["Stream ID", "stream_id"]),
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "dur_ns": end_ns - start_ns,
                    "source_file": source_path.name,
                    "matched_op_summary_row_id": f"{global_row_id:06d}",
                }
            )
    for key in matches:
        matches[key].sort(key=lambda item: (int(item["start_ns"]), int(item["end_ns"]), str(item["task_id"])))
    return matches


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    inventory = load_json(workspace_dir / "input" / "source_inventory.json")
    source_path = Path(inventory["raw_operator_details_path"])
    op_summary_paths = [Path(item) for item in inventory["raw_op_summary_paths"]]
    output_path = workspace_dir / "artifacts" / "slices" / "operator_details_slice.csv"
    window_start_ns = int(state["inputs"]["window_start_ns"])
    window_end_ns = int(state["inputs"]["window_end_ns"])
    op_summary_matches = build_op_summary_matches(op_summary_paths, window_start_ns, window_end_ns)
    match_offsets: dict[str, int] = defaultdict(int)

    fieldnames, operator_rows = load_csv_rows(source_path)
    output_fields = list(fieldnames)
    for extra_column in ["Task ID", "Stream ID", "Op Name"]:
        if extra_column not in output_fields:
            output_fields.append(extra_column)
    extra_fields = [
        "slice_row_id",
        "source_file",
        "matched_op_summary_source_file",
        "matched_op_summary_row_id",
        "start_ns",
        "end_ns",
        "dur_ns",
        "time_source",
        "time_match_confidence",
        "time_match_score",
        "time_match_rank",
        "time_match_basis",
        "time_alignment_notes",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as dst:
        writer = csv.DictWriter(dst, fieldnames=output_fields + extra_fields)
        writer.writeheader()
        row_index = 0
        for row in operator_rows:
            op_name = find_first(row, ["Name", "Op Name"])
            match_key = normalize_name(op_name)
            candidates = op_summary_matches.get(match_key, [])
            candidate_index = match_offsets[match_key]
            operator_duration_ns = infer_operator_duration_ns(row)
            row_index += 1
            payload = {field: row.get(field, "") for field in output_fields}
            payload["Op Name"] = op_name
            payload["slice_row_id"] = f"{row_index:06d}"
            payload["source_file"] = source_path.name
            if candidate_index < len(candidates):
                match = candidates[candidate_index]
                match_offsets[match_key] += 1
                basis = ["name", "sequence"]
                notes = ["matched by op name and local sequence"]
                score = 0.75
                if operator_duration_ns is not None and int(match["dur_ns"]) > 0:
                    basis.append("duration")
                    delta = abs(operator_duration_ns - int(match["dur_ns"]))
                    duration_ratio = max(0.0, 1.0 - (delta / max(operator_duration_ns, int(match["dur_ns"]), 1)))
                    score = min(0.95, score + 0.2 * duration_ratio)
                    notes.append(f"duration_delta_ns={delta}")
                payload["Task ID"] = str(match["task_id"])
                payload["Stream ID"] = str(match["stream_id"])
                payload["matched_op_summary_source_file"] = str(match["source_file"])
                payload["matched_op_summary_row_id"] = str(match["matched_op_summary_row_id"])
                payload["start_ns"] = str(match["start_ns"])
                payload["end_ns"] = str(match["end_ns"])
                payload["dur_ns"] = str(match["dur_ns"])
                payload["time_source"] = "op_summary_name_sequence_match"
                payload["time_match_confidence"] = "medium"
                payload["time_match_score"] = f"{score:.2f}"
                payload["time_match_rank"] = "1"
                payload["time_match_basis"] = "|".join(basis)
                payload["time_alignment_notes"] = "; ".join(notes)
            else:
                payload["Task ID"] = ""
                payload["Stream ID"] = ""
                payload["matched_op_summary_source_file"] = ""
                payload["matched_op_summary_row_id"] = ""
                payload["start_ns"] = ""
                payload["end_ns"] = ""
                payload["dur_ns"] = ""
                payload["time_source"] = "raw_operator_details_without_absolute_time"
                payload["time_match_confidence"] = "low"
                payload["time_match_score"] = "0.00"
                payload["time_match_rank"] = ""
                payload["time_match_basis"] = "missing_absolute_time"
                payload["time_alignment_notes"] = "operator_details.csv 不含绝对时间，未命中 op_summary 候选"
            writer.writerow(payload)

    state["artifacts"]["operator_slice_path"] = str(output_path)
    save_state(workspace_dir, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
