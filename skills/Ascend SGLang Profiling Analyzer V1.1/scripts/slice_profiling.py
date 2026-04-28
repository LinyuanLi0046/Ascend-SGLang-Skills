#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
高精度按 ns 时间窗口截取 chrome trace json。

特点：
1. 输入窗口 start/end 用 ns
2. trace 中 ts/dur 可为:
   - 字符串: "1776493036180691.730"
   - 数字: 1776493036180691.73
   - 整数
3. 内部统一转换成 int ns,避免 float 精度问题
4. X 事件会裁剪到窗口内
5. B/E 会先全局配对,再转成 X 输出,避免截断后 begin/end 不配对
6. i/I/C 等点事件按 ts 是否落入窗口保留
7. M 元数据默认全部保留
8. 可选 shift-to-zero,把窗口起点平移到 0

用法示例：
python3 scripts/slice_profiling.py \
  trace_view.json \
  trace_slice.json \
  --start-ns 1776493036180691730 \
  --end-ns   1776493037180691730 \
  --trace-unit us \
  --shift-to-zero \
  --write-stats slice_stats.json
"""

import argparse
import copy
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# -----------------------------
# 基础解析与序列化
# -----------------------------

def load_trace(path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any], bool]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        if "traceEvents" in data:
            events = data["traceEvents"]
            meta = {k: v for k, v in data.items() if k != "traceEvents"}
            return events, meta, True
        elif "events" in data:
            events = data["events"]
            meta = {k: v for k, v in data.items() if k != "events"}
            return events, meta, False
        else:
            raise ValueError("JSON 顶层是 dict,但没有 traceEvents/events 字段。")
    elif isinstance(data, list):
        return data, {}, True
    else:
        raise ValueError("不支持的 trace json 格式。")


def dump_trace(
    path: Path,
    events: List[Dict[str, Any]],
    meta: Dict[str, Any],
    use_trace_events_key: bool = True,
) -> None:
    if use_trace_events_key:
        out = dict(meta)
        out["traceEvents"] = events
    else:
        out = dict(meta)
        out["events"] = events

    with path.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def detect_prefer_string(events: List[Dict[str, Any]]) -> bool:
    """
    只要原 trace 中有 ts/dur 是字符串,就默认输出也用字符串,
    这样最稳,也最接近你的原始格式。
    """
    for evt in events[:1000]:
        if "ts" in evt and isinstance(evt["ts"], str):
            return True
        if "dur" in evt and isinstance(evt["dur"], str):
            return True
    return False


def parse_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def trace_value_to_ns(value: Any, trace_unit: str) -> Optional[int]:
    """
    把 trace 中的 ts/dur 转成整数 ns。
    - trace_unit == 'ns': 直接转 int ns
    - trace_unit == 'us': 乘 1000,支持 3 位小数表示 ns
    """
    d = parse_decimal(value)
    if d is None:
        return None

    if trace_unit == "ns":
        return int(d.to_integral_value(rounding=ROUND_DOWN))
    elif trace_unit == "us":
        ns = d * Decimal(1000)
        return int(ns.to_integral_value(rounding=ROUND_DOWN))
    else:
        raise ValueError(f"不支持的 trace_unit: {trace_unit}")


def ns_to_trace_value(ns_value: int, trace_unit: str, prefer_string: bool) -> Any:
    """
    把整数 ns 转回 trace 单位。
    为了避免大数 float 精度问题：
    - prefer_string=True 时,输出字符串
    - 否则尽量输出 int / float
    """
    if trace_unit == "ns":
        if prefer_string:
            return str(ns_value)
        return ns_value

    if trace_unit == "us":
        whole = ns_value // 1000
        frac = ns_value % 1000

        if prefer_string:
            # 固定保留 3 位小数,精确表达 ns
            return f"{whole}.{frac:03d}"

        if frac == 0:
            return whole
        # 注意：这里如果用 float 仍有精度风险,但仅在用户显式不想要字符串时才走到这
        return float(f"{whole}.{frac:03d}")

    raise ValueError(f"不支持的 trace_unit: {trace_unit}")


def get_ts_ns(evt: Dict[str, Any], trace_unit: str) -> Optional[int]:
    return trace_value_to_ns(evt.get("ts"), trace_unit)


def get_dur_ns(evt: Dict[str, Any], trace_unit: str) -> Optional[int]:
    return trace_value_to_ns(evt.get("dur"), trace_unit)


# -----------------------------
# 时间窗口与裁剪
# -----------------------------

def maybe_shift_ns(value_ns: Optional[int], window_start_ns: int, shift_to_zero: bool) -> Optional[int]:
    if value_ns is None:
        return None
    return value_ns - window_start_ns if shift_to_zero else value_ns


def clip_interval_ns(start_ns: int, end_ns: int, win_start_ns: int, win_end_ns: int) -> Optional[Tuple[int, int]]:
    s = max(start_ns, win_start_ns)
    e = min(end_ns, win_end_ns)
    if s < e:
        return s, e
    return None


def build_sliced_x_event_from_x(
    evt: Dict[str, Any],
    trace_unit: str,
    win_start_ns: int,
    win_end_ns: int,
    shift_to_zero: bool,
    prefer_string: bool,
) -> Optional[Dict[str, Any]]:
    ts_ns = get_ts_ns(evt, trace_unit)
    dur_ns = get_dur_ns(evt, trace_unit)
    if ts_ns is None or dur_ns is None:
        return None

    end_ns = ts_ns + dur_ns
    clipped = clip_interval_ns(ts_ns, end_ns, win_start_ns, win_end_ns)
    if clipped is None:
        return None

    s_ns, e_ns = clipped
    out = copy.deepcopy(evt)
    out["ph"] = "X"
    out["ts"] = ns_to_trace_value(
        maybe_shift_ns(s_ns, win_start_ns, shift_to_zero),
        trace_unit,
        prefer_string,
    )
    out["dur"] = ns_to_trace_value(e_ns - s_ns, trace_unit, prefer_string)
    return out


# -----------------------------
# B/E 配对
# -----------------------------

def pair_be_events(events: List[Dict[str, Any]], trace_unit: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    按 (pid, tid) 栈配对 B/E。
    这是标准同步 begin/end 的常见处理方式。
    """
    stacks: Dict[Tuple[Any, Any], List[Dict[str, Any]]] = defaultdict(list)
    intervals: List[Dict[str, Any]] = []
    anomalies: List[Dict[str, Any]] = []

    def sort_key(evt: Dict[str, Any]):
        ts_ns = get_ts_ns(evt, trace_unit)
        return (1 if ts_ns is None else 0, 10**30 if ts_ns is None else ts_ns, evt.get("_orig_index", 0))

    for evt in sorted(events, key=sort_key):
        ph = evt.get("ph")
        pid = evt.get("pid")
        tid = evt.get("tid")
        ts_ns = get_ts_ns(evt, trace_unit)

        if ph == "B":
            if ts_ns is not None:
                stacks[(pid, tid)].append(evt)

        elif ph == "E":
            if ts_ns is None:
                continue

            stack = stacks[(pid, tid)]
            if not stack:
                anomalies.append({
                    "type": "unmatched_E",
                    "pid": pid,
                    "tid": tid,
                    "ts_ns": ts_ns,
                    "event_name": evt.get("name"),
                })
            else:
                begin_evt = stack.pop()
                intervals.append({
                    "begin": begin_evt,
                    "end": evt,
                    "pid": pid,
                    "tid": tid,
                })

    for (pid, tid), stack in stacks.items():
        for evt in stack:
            anomalies.append({
                "type": "unmatched_B",
                "pid": pid,
                "tid": tid,
                "ts_ns": get_ts_ns(evt, trace_unit),
                "event_name": evt.get("name"),
            })

    return intervals, anomalies


def build_sliced_x_event_from_be_pair(
    begin_evt: Dict[str, Any],
    end_evt: Dict[str, Any],
    trace_unit: str,
    win_start_ns: int,
    win_end_ns: int,
    shift_to_zero: bool,
    prefer_string: bool,
) -> Optional[Dict[str, Any]]:
    b_ns = get_ts_ns(begin_evt, trace_unit)
    e_ns = get_ts_ns(end_evt, trace_unit)
    if b_ns is None or e_ns is None or e_ns <= b_ns:
        return None

    clipped = clip_interval_ns(b_ns, e_ns, win_start_ns, win_end_ns)
    if clipped is None:
        return None

    s_ns, e_ns = clipped

    out: Dict[str, Any] = {
        "name": begin_evt.get("name"),
        "cat": begin_evt.get("cat", ""),
        "ph": "X",
        "ts": ns_to_trace_value(
            maybe_shift_ns(s_ns, win_start_ns, shift_to_zero),
            trace_unit,
            prefer_string,
        ),
        "dur": ns_to_trace_value(e_ns - s_ns, trace_unit, prefer_string),
        "pid": begin_evt.get("pid"),
        "tid": begin_evt.get("tid"),
    }

    args = {}
    if isinstance(begin_evt.get("args"), dict):
        args.update(begin_evt["args"])
    if isinstance(end_evt.get("args"), dict):
        args.update(end_evt["args"])
    if args:
        out["args"] = args

    for key in ["id", "id2", "cname", "tts"]:
        if key in begin_evt:
            out[key] = begin_evt[key]

    return out


# -----------------------------
# 主流程
# -----------------------------

def slice_trace(
    events: List[Dict[str, Any]],
    start_ns: int,
    end_ns: int,
    trace_unit: str,
    shift_to_zero: bool,
    keep_metadata: bool = True,
    keep_other_events_in_window: bool = True,
    prefer_string_output: Optional[bool] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if end_ns <= start_ns:
        raise ValueError("end_ns 必须大于 start_ns。")

    if prefer_string_output is None:
        prefer_string_output = detect_prefer_string(events)

    enriched_events = []
    for i, evt in enumerate(events):
        evt2 = copy.deepcopy(evt)
        evt2["_orig_index"] = i
        enriched_events.append(evt2)

    out_events: List[Dict[str, Any]] = []
    stats = {
        "window_start_ns": start_ns,
        "window_end_ns": end_ns,
        "trace_unit": trace_unit,
        "shift_to_zero": shift_to_zero,
        "prefer_string_output": prefer_string_output,
        "kept_metadata_events": 0,
        "kept_original_point_events": 0,
        "kept_other_window_events": 0,
        "kept_sliced_X_events": 0,
        "kept_sliced_BE_as_X_events": 0,
        "dropped_unmatched_BE": 0,
        "input_event_count": len(events),
        "output_event_count": 0,
    }

    # 1) 保留 metadata
    if keep_metadata:
        for evt in enriched_events:
            if evt.get("ph") == "M":
                evt_out = copy.deepcopy(evt)
                evt_out.pop("_orig_index", None)
                out_events.append(evt_out)
                stats["kept_metadata_events"] += 1

    # 2) 裁剪原生 X
    for evt in enriched_events:
        if evt.get("ph") != "X":
            continue
        sliced = build_sliced_x_event_from_x(
            evt=evt,
            trace_unit=trace_unit,
            win_start_ns=start_ns,
            win_end_ns=end_ns,
            shift_to_zero=shift_to_zero,
            prefer_string=prefer_string_output,
        )
        if sliced is not None:
            sliced.pop("_orig_index", None)
            out_events.append(sliced)
            stats["kept_sliced_X_events"] += 1

    # 3) B/E -> X
    be_intervals, anomalies = pair_be_events(enriched_events, trace_unit)
    stats["dropped_unmatched_BE"] = len(anomalies)

    for pair in be_intervals:
        sliced = build_sliced_x_event_from_be_pair(
            begin_evt=pair["begin"],
            end_evt=pair["end"],
            trace_unit=trace_unit,
            win_start_ns=start_ns,
            win_end_ns=end_ns,
            shift_to_zero=shift_to_zero,
            prefer_string=prefer_string_output,
        )
        if sliced is not None:
            out_events.append(sliced)
            stats["kept_sliced_BE_as_X_events"] += 1

    # 4) 保留其它点事件 / 异步事件,只要 ts 落在窗口里
    #    不保留原始 M/B/E/X,避免重复
    if keep_other_events_in_window:
        skip_ph = {"M", "B", "E", "X"}
        for evt in enriched_events:
            ph = evt.get("ph")
            if ph in skip_ph:
                continue

            ts_ns = get_ts_ns(evt, trace_unit)
            if ts_ns is None:
                continue

            if start_ns <= ts_ns <= end_ns:
                evt_out = copy.deepcopy(evt)
                evt_out["ts"] = ns_to_trace_value(
                    maybe_shift_ns(ts_ns, start_ns, shift_to_zero),
                    trace_unit,
                    prefer_string_output,
                )
                evt_out.pop("_orig_index", None)
                out_events.append(evt_out)

                if ph in {"i", "I", "C"}:
                    stats["kept_original_point_events"] += 1
                else:
                    stats["kept_other_window_events"] += 1

    # 5) 排序
    def sort_key(evt: Dict[str, Any]):
        ph = evt.get("ph")
        if ph == "M":
            return (-1, -1, -1)

        ts_ns = get_ts_ns(evt, trace_unit)
        if ts_ns is None:
            return (10, 0, 0)

        dur_ns = get_dur_ns(evt, trace_unit)
        if dur_ns is None:
            dur_ns = 0

        return (0, ts_ns, -dur_ns)

    out_events.sort(key=sort_key)
    stats["output_event_count"] = len(out_events)
    stats["estimated_complexity"] = "需要扫描整个输入 trace 整体耗时与原文件事件总数强相关"

    return out_events, stats


def main():
    parser = argparse.ArgumentParser(description="高精度按 ns 窗口截取 chrome trace json")
    parser.add_argument("input", help="输入 trace json 文件")
    parser.add_argument("output", help="输出 trace json 文件")
    parser.add_argument("--start-ns", type=int, required=True, help="窗口起始时间戳 ns ")
    parser.add_argument("--end-ns", type=int, required=True, help="窗口结束时间戳 ns ")
    parser.add_argument(
        "--trace-unit",
        choices=["us", "ns"],
        default="us",
        help="输入 trace 文件中的 ts/dur 单位。你的这个例子应使用 us",
    )
    parser.add_argument(
        "--shift-to-zero",
        action="store_true",
        help="把输出 trace 的窗口起点平移到 0",
    )
    parser.add_argument(
        "--force-string-output",
        action="store_true",
        help="强制输出 ts/dur 为字符串,避免大数精度损失",
    )
    parser.add_argument(
        "--force-number-output",
        action="store_true",
        help="强制输出 ts/dur 为数字；大数 us 可能有精度风险,不推荐",
    )
    parser.add_argument(
        "--write-stats",
        default="",
        help="可选,写一份统计 json 到这个路径",
    )

    args = parser.parse_args()

    if args.force_string_output and args.force_number_output:
        raise ValueError("--force-string-output 和 --force-number-output 不能同时指定")

    input_path = Path(args.input)
    output_path = Path(args.output)

    events, meta, use_trace_events_key = load_trace(input_path)

    prefer_string_output = None
    if args.force_string_output:
        prefer_string_output = True
    elif args.force_number_output:
        prefer_string_output = False

    out_events, stats = slice_trace(
        events=events,
        start_ns=args.start_ns,
        end_ns=args.end_ns,
        trace_unit=args.trace_unit,
        shift_to_zero=args.shift_to_zero,
        prefer_string_output=prefer_string_output,
    )

    out_meta = dict(meta)
    out_meta.setdefault("displayTimeUnit", args.trace_unit)
    out_meta["_slice_info"] = stats

    dump_trace(output_path, out_events, out_meta, use_trace_events_key=use_trace_events_key)

    if args.write_stats:
        stats_path = Path(args.write_stats)
        with stats_path.open("w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

    print(json.dumps({
        "input": str(input_path),
        "output": str(output_path),
        "output_event_count": len(out_events),
        "stats": stats,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

# python3 slice_profiling.py \
#   trace_view.json \
#   trace_slice.json \
#   --start-ns 1776493038560004900 \
#   --end-ns 1776493038600497200 \
#   --trace-unit us \
#   --shift-to-zero
