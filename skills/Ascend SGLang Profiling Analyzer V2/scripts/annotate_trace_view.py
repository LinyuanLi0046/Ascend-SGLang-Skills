from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, TextIO

from workflow_common import load_json, load_state, save_state


ARRAY_FIELD_RE = re.compile(r'"(?P<field>traceEvents|events)"\s*:\s*\[')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="回写 trace_view.annotated.json。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def build_index_to_code(mapping_payload: dict[str, Any]) -> dict[int, str]:
    index_to_code: dict[int, str] = {}
    for row in mapping_payload.get("rows", []):
        if row.get("exclude_from_code_mapping"):
            continue
        code_location = str(row.get("code_location", "")).strip()
        if not code_location:
            continue
        ref = row.get("trace_event_ref", {})
        event_index = ref.get("trace_event_index")
        if isinstance(event_index, int) and event_index >= 0:
            index_to_code[event_index] = code_location
    return index_to_code


def inject_code_location(event: dict[str, Any], code_location: str) -> dict[str, Any]:
    event.pop("code_location", None)
    event_args = event.get("args")
    if not isinstance(event_args, dict):
        event_args = {}
        event["args"] = event_args
    event_args["code_location"] = code_location
    return event


def _write_event_array(
    reader: TextIO,
    writer: TextIO,
    initial_buffer: str,
    index_to_code: dict[int, str],
) -> None:
    decoder = json.JSONDecoder()
    buffer = initial_buffer
    event_index = 0
    first_event = True
    while True:
        stripped = buffer.lstrip()
        while not stripped:
            chunk = reader.read(65536)
            if not chunk:
                raise ValueError("trace 事件数组未正常结束。")
            buffer += chunk
            stripped = buffer.lstrip()
        buffer = stripped
        if buffer[0] == ",":
            buffer = buffer[1:]
            continue
        if buffer[0] == "]":
            writer.write("]")
            writer.write(buffer[1:])
            writer.write(reader.read())
            return
        while True:
            try:
                event_payload, end_index = decoder.raw_decode(buffer)
                break
            except json.JSONDecodeError:
                chunk = reader.read(65536)
                if not chunk:
                    raise ValueError(f"trace 第 {event_index} 个事件解析失败或文件不完整。") from None
                buffer += chunk
        if not isinstance(event_payload, dict):
            raise ValueError(f"trace 第 {event_index} 个事件不是对象。")
        code_location = index_to_code.get(event_index)
        if code_location:
            event_payload = inject_code_location(event_payload, code_location)
        if not first_event:
            writer.write(",\n")
        writer.write(json.dumps(event_payload, ensure_ascii=False))
        first_event = False
        event_index += 1
        buffer = buffer[end_index:]


def annotate_trace_streaming(trace_path: Path, output_path: Path, index_to_code: dict[int, str]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_output_path = output_path.with_name(output_path.name + ".tmp")
    with trace_path.open("r", encoding="utf-8") as reader, temp_output_path.open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as writer:
        prefix = ""
        first_non_ws = ""
        while True:
            chunk = reader.read(4096)
            if not chunk:
                raise ValueError(f"trace 文件为空或格式非法: {trace_path}")
            prefix += chunk
            for char in chunk:
                if not char.isspace():
                    first_non_ws = char
                    break
            if first_non_ws:
                break
        if first_non_ws == "[":
            array_start = prefix.index("[")
            writer.write(prefix[: array_start + 1])
            _write_event_array(reader, writer, prefix[array_start + 1 :], index_to_code)
        elif first_non_ws == "{":
            search_buffer = prefix
            while True:
                match = ARRAY_FIELD_RE.search(search_buffer)
                if match:
                    writer.write(search_buffer[: match.end()])
                    _write_event_array(reader, writer, search_buffer[match.end() :], index_to_code)
                    break
                chunk = reader.read(65536)
                if not chunk:
                    raise ValueError("trace json 缺少 traceEvents/events 数组。")
                search_buffer += chunk
        else:
            raise ValueError(f"不支持的 trace 顶层格式: {trace_path}")
    temp_output_path.replace(output_path)


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    trace_path = Path(state["artifacts"]["trace_slice_path"])
    mapping_path = Path(state["artifacts"]["span_code_mapping_path"])
    mapping_payload = load_json(mapping_path)
    index_to_code = build_index_to_code(mapping_payload)

    output_path = workspace_dir / "output" / "trace_view.annotated.json"
    annotate_trace_streaming(trace_path, output_path, index_to_code)
    state["artifacts"]["annotated_trace_path"] = str(output_path)
    state["flags"]["annotated_trace_generated"] = True
    save_state(workspace_dir, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
