from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from pathlib import Path

from slice_profiling import (
    build_sliced_x_event_from_be_pair,
    build_sliced_x_event_from_x,
    dump_trace,
    get_ts_ns,
    load_trace,
    slice_trace,
)
from workflow_common import load_state, save_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="基于 workspace 状态切片 trace_view.json。")
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument("--trace-unit", default="us", choices=["us", "ns"])
    return parser


def read_first_non_ws_char(path: Path) -> str:
    with path.open("r", encoding="utf-8") as handle:
        while True:
            chunk = handle.read(4096)
            if not chunk:
                return ""
            for char in chunk:
                if not char.isspace():
                    return char
    return ""


def iter_top_level_json_list(path: Path):
    decoder = json.JSONDecoder()
    buffer = ""
    started = False
    with path.open("r", encoding="utf-8") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            buffer += chunk
            while True:
                if not started:
                    stripped = buffer.lstrip()
                    if not stripped:
                        buffer = ""
                        break
                    if stripped[0] != "[":
                        raise ValueError("低内存切片仅支持顶层 list trace。")
                    buffer = stripped[1:]
                    started = True

                stripped = buffer.lstrip()
                leading_trim = len(buffer) - len(stripped)
                if leading_trim:
                    buffer = stripped
                if not buffer:
                    break
                if buffer[0] == ",":
                    buffer = buffer[1:]
                    continue
                if buffer[0] == "]":
                    return
                try:
                    event, end_index = decoder.raw_decode(buffer)
                except json.JSONDecodeError:
                    break
                yield event
                buffer = buffer[end_index:]

    buffer = buffer.lstrip()
    if buffer and buffer != "]":
        decoder = json.JSONDecoder()
        while buffer:
            if buffer[0] == ",":
                buffer = buffer[1:].lstrip()
                continue
            if buffer[0] == "]":
                break
            event, end_index = decoder.raw_decode(buffer)
            yield event
            buffer = buffer[end_index:].lstrip()


def resolve_progress_interval(input_size: int) -> int:
    if os.name == "nt" and input_size > 200 * 1024 * 1024:
        return 50000
    return 200000


def slice_large_trace_list(
    input_path: Path,
    start_ns: int,
    end_ns: int,
    trace_unit: str,
    progress_interval: int,
) -> tuple[list[dict], dict, bool]:
    out_events: list[dict] = []
    stats = {
        "window_start_ns": start_ns,
        "window_end_ns": end_ns,
        "trace_unit": trace_unit,
        "shift_to_zero": False,
        "prefer_string_output": True,
        "kept_metadata_events": 0,
        "kept_original_point_events": 0,
        "kept_other_window_events": 0,
        "kept_sliced_X_events": 0,
        "kept_sliced_BE_as_X_events": 0,
        "dropped_unmatched_BE": 0,
        "input_event_count": 0,
        "output_event_count": 0,
        "estimated_complexity": "streaming top-level list trace",
    }
    prefer_string_output = True
    stacks: dict[tuple[object, object], list[dict]] = defaultdict(list)
    for index, evt in enumerate(iter_top_level_json_list(input_path)):
        stats["input_event_count"] += 1
        if index > 0 and index % progress_interval == 0:
            print(
                f"[slice_trace_workspace] 已扫描 {index} 条顶层 trace 事件，当前保留 {len(out_events)} 条。",
                flush=True,
            )
        evt["_orig_index"] = index
        if index < 1000 and (isinstance(evt.get("ts"), str) or isinstance(evt.get("dur"), str)):
            prefer_string_output = True
        ph = evt.get("ph")
        if ph == "M":
            out = dict(evt)
            out.pop("_orig_index", None)
            out_events.append(out)
            stats["kept_metadata_events"] += 1
            continue
        if ph == "X":
            sliced = build_sliced_x_event_from_x(
                evt=evt,
                trace_unit=trace_unit,
                win_start_ns=start_ns,
                win_end_ns=end_ns,
                shift_to_zero=False,
                prefer_string=prefer_string_output,
            )
            if sliced is not None:
                out_events.append(sliced)
                stats["kept_sliced_X_events"] += 1
            continue
        if ph == "B":
            ts_ns = get_ts_ns(evt, trace_unit)
            if ts_ns is not None:
                stacks[(evt.get("pid"), evt.get("tid"))].append(evt)
            continue
        if ph == "E":
            ts_ns = get_ts_ns(evt, trace_unit)
            if ts_ns is None:
                continue
            stack = stacks[(evt.get("pid"), evt.get("tid"))]
            if not stack:
                stats["dropped_unmatched_BE"] += 1
                continue
            begin_evt = stack.pop()
            sliced = build_sliced_x_event_from_be_pair(
                begin_evt=begin_evt,
                end_evt=evt,
                trace_unit=trace_unit,
                win_start_ns=start_ns,
                win_end_ns=end_ns,
                shift_to_zero=False,
                prefer_string=prefer_string_output,
            )
            if sliced is not None:
                out_events.append(sliced)
                stats["kept_sliced_BE_as_X_events"] += 1
            continue

        ts_ns = get_ts_ns(evt, trace_unit)
        if ts_ns is None or not (start_ns <= ts_ns <= end_ns):
            continue
        out = dict(evt)
        out.pop("_orig_index", None)
        out_events.append(out)
        if ph in {"i", "I", "C"}:
            stats["kept_original_point_events"] += 1
        else:
            stats["kept_other_window_events"] += 1

    for stack in stacks.values():
        stats["dropped_unmatched_BE"] += len(stack)

    out_events.sort(
        key=lambda evt: (
            -1 if evt.get("ph") == "M" else 0,
            0 if evt.get("ph") == "M" else int(get_ts_ns(evt, trace_unit) or 0),
            -(int(evt.get("dur", 0)) if str(evt.get("dur", "")).isdigit() else 0),
        )
    )
    stats["output_event_count"] = len(out_events)
    stats["prefer_string_output"] = prefer_string_output
    return out_events, stats, True


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    input_path = Path(state["artifacts"]["raw_trace_path"])
    output_path = workspace_dir / "artifacts" / "slices" / "trace_slice.json"
    start_ns = int(state["inputs"]["window_start_ns"])
    end_ns = int(state["inputs"]["window_end_ns"])
    input_size = input_path.stat().st_size
    print(
        f"[slice_trace_workspace] 开始切片 trace，input={input_path}，size_bytes={input_size}，window=[{start_ns}, {end_ns}]",
        flush=True,
    )
    progress_interval = resolve_progress_interval(input_size)
    print(
        f"[slice_trace_workspace] progress_interval={progress_interval} (is_windows={os.name == 'nt'})",
        flush=True,
    )
    stage_start = time.perf_counter()
    if input_size > 200 * 1024 * 1024 and read_first_non_ws_char(input_path) == "[":
        print("[slice_trace_workspace] 检测到大文件顶层 list trace，启用流式低内存切片。", flush=True)
        meta = {}
        sliced_events, stats, use_trace_events_key = slice_large_trace_list(
            input_path=input_path,
            start_ns=start_ns,
            end_ns=end_ns,
            trace_unit=args.trace_unit,
            progress_interval=progress_interval,
        )
    else:
        print("[slice_trace_workspace] 使用常规 trace 加载与切片路径。", flush=True)
        events, meta, use_trace_events_key = load_trace(input_path)
        sliced_events, stats = slice_trace(
            events=events,
            start_ns=start_ns,
            end_ns=end_ns,
            trace_unit=args.trace_unit,
            shift_to_zero=False,
            keep_metadata=True,
            keep_other_events_in_window=True,
            prefer_string_output=None,
        )
    print(
        f"[slice_trace_workspace] 事件裁剪阶段完成，当前输出事件数={len(sliced_events)}，耗时={time.perf_counter() - stage_start:.2f}s",
        flush=True,
    )
    for index, event in enumerate(sliced_events):
        if event.get("ph") == "X":
            args_dict = event.setdefault("args", {})
            if isinstance(args_dict, dict):
                args_dict["trace_event_index"] = index
    meta = dict(meta)
    meta["_slice_info"] = stats
    print("[slice_trace_workspace] 开始写出 trace_slice.json", flush=True)
    dump_start = time.perf_counter()
    dump_trace(output_path, sliced_events, meta, use_trace_events_key=use_trace_events_key)
    print(
        f"[slice_trace_workspace] trace_slice.json 写出完成，耗时={time.perf_counter() - dump_start:.2f}s",
        flush=True,
    )
    print(
        f"[slice_trace_workspace] 切片完成，输出事件数={len(sliced_events)}，output={output_path}",
        flush=True,
    )
    state["artifacts"]["trace_slice_path"] = str(output_path)
    print("[slice_trace_workspace] 开始回写 state.artifacts.trace_slice_path", flush=True)
    save_state(workspace_dir, state)
    print("[slice_trace_workspace] state 回写完成", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
