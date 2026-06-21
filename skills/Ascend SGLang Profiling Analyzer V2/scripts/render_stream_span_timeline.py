from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from workflow_common import dump_json, iter_classified_streams, load_json, load_state, save_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 stream_span_timeline.json。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    mapping = load_json(Path(state["artifacts"]["span_code_mapping_path"]))
    mapping_by_span = {row["span_id"]: row for row in mapping.get("rows", [])}

    streams_payload: list[dict[str, Any]] = []
    global_order: list[dict[str, Any]] = []
    classified_path = Path(state["artifacts"]["classified_spans_path"])
    for stream in iter_classified_streams(classified_path):
        stream_id = stream["stream_id"]
        spans = sorted(stream.get("spans", []), key=lambda item: (item["start_ns"], item["end_ns"], item["span_id"]))
        rendered_spans = []
        for index, span in enumerate(spans):
            mapping_row = mapping_by_span.get(span["span_id"], {})
            rendered_spans.append(
                {
                    "span_id": span["span_id"],
                    "trace_event_ref": span.get("trace_event_ref", {}),
                    "task_ids": span.get("task_ids", []),
                    "start_ns": span["start_ns"],
                    "end_ns": span["end_ns"],
                    "dur_ns": span["dur_ns"],
                    "semantic_class": span.get("semantic_class", "unknown"),
                    "phase": mapping_row.get("phase", "unknown"),
                    "owner_class": mapping_row.get("owner_class", "unknown"),
                    "code_location": mapping_row.get("code_location", ""),
                    "code_location_confidence": mapping_row.get("confidence", "low"),
                    "mapped_region": mapping_row.get("mapped_region", ""),
                    "evidence_sources": mapping_row.get("evidence_sources", []),
                    "parallel_group": span.get("parallel_group", ""),
                    "prev_in_stream": spans[index - 1]["span_id"] if index > 0 else "",
                    "next_in_stream": spans[index + 1]["span_id"] if index + 1 < len(spans) else "",
                    "notes": mapping_row.get("notes", []),
                }
            )
        streams_payload.append(
            {
                "stream_id": stream_id,
                "stream_role": stream.get("stream_role", "unknown"),
                "device_id": "0",
                "spans": rendered_spans,
            }
        )
        for span in rendered_spans:
            global_order.append(
                {
                    "seq": 0,
                    "span_id": span["span_id"],
                    "stream_id": stream_id,
                    "start_ns": span["start_ns"],
                    "end_ns": span["end_ns"],
                    "parallel_group": span["parallel_group"],
                }
            )

    global_order.sort(key=lambda item: (item["start_ns"], item["end_ns"], item["stream_id"], item["span_id"]))
    for index, row in enumerate(global_order, start=1):
        row["seq"] = index

    output = {
        "schema_version": "p0_stack_mapping_v1",
        "profiling_root": state["inputs"]["profiling_root_path"],
        "code_repo_root": state["inputs"]["code_repo_path"],
        "window_start_ns": int(state["inputs"]["window_start_ns"]),
        "window_end_ns": int(state["inputs"]["window_end_ns"]),
        "stream_count": len(streams_payload),
        "streams": streams_payload,
        "global_order": global_order,
        "coverage": mapping.get("coverage", {}),
        "notes": [],
    }
    output_path = workspace_dir / "artifacts" / "timeline" / "stream_span_timeline.json"
    dump_json(output_path, output)
    state["artifacts"]["stream_span_timeline_path"] = str(output_path)
    state["flags"]["timeline_generated"] = True
    save_state(workspace_dir, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
