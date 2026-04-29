#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
读取 kernel_details_slice.csv,输出适合 agent / 人快速阅读的摘要层,
但不替代原始 kernel_details_slice.csv。

输出文件：
- stream_summary.json
- top_kernels.json
- bubble_candidates.json
- kernel_analysis.md

推荐输入：
- 由 scripts/slice_kernel_csv.py 产生的 kernel_details_slice.csv
- 若存在 effective_start_us / effective_end_us / effective_duration_us,会优先使用

用法示例：
python3 scripts/process_kernel.py \
  kernel_details_slice.csv \
  --outdir kernel_summary

如果你想显式指定窗口（用于更准确的 prelaunch / tail 判断）：
python3 scripts/process_kernel.py \
  kernel_details_slice.csv \
  --outdir kernel_summary \
  --window-start-ns 1776493036180691730 \
  --window-end-ns   1776493037180691730

也支持直接传 us
python3 scripts/process_kernel.py \
  kernel_details_slice.csv \
  --outdir kernel_summary \
  --window-start-us 1776493036180691.730 \
  --window-end-us   1776493037180691.730
"""

import argparse
import csv
import json
import math
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# -----------------------------
# 配置
# -----------------------------

COL_STEP_ID = "Step Id"
COL_DEVICE_ID = "Device_id"
COL_MODEL_ID = "Model ID"
COL_TASK_ID = "Task ID"
COL_STREAM_ID = "Stream ID"
COL_NAME = "Name"
COL_TYPE = "Type"            # 你的表头
COL_ALT_TYPE = "Task Type"   # 兼容其它版本
COL_OP_STATE = "OP State"
COL_ACCELERATOR = "Accelerator Core"
COL_START_US = "Start Time(us)"
COL_DURATION_US = "Duration(us)"
COL_WAIT_US = "Wait Time(us)"
COL_CONTEXT_ID = "Context ID"

COL_EFFECTIVE_START_US = "effective_start_us"
COL_EFFECTIVE_END_US = "effective_end_us"
COL_EFFECTIVE_DURATION_US = "effective_duration_us"


# -----------------------------
# 工具函数
# -----------------------------

def parse_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def ns_to_us_decimal(ns_value: int) -> Decimal:
    return Decimal(ns_value) / Decimal(1000)


def decimal_to_str(d: Decimal) -> str:
    return format(d, "f")


def format_us(us: Decimal) -> str:
    if us >= Decimal("1000000"):
        return f"{(us / Decimal('1000000')):.3f}s"
    if us >= Decimal("1000"):
        return f"{(us / Decimal('1000')):.3f}ms"
    return f"{us:.3f}us"


def compact_json(obj: Any, max_len: int = 240) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        s = str(obj)
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


# -----------------------------
# 区间
# -----------------------------

@dataclass
class Interval:
    start_us: Decimal
    end_us: Decimal

    @property
    def dur_us(self) -> Decimal:
        return max(Decimal("0"), self.end_us - self.start_us)


def merge_intervals(intervals: List[Interval]) -> List[Interval]:
    if not intervals:
        return []
    items = sorted(
        [x for x in intervals if x.end_us > x.start_us],
        key=lambda x: (x.start_us, x.end_us),
    )
    if not items:
        return []

    merged = [Interval(items[0].start_us, items[0].end_us)]
    for cur in items[1:]:
        last = merged[-1]
        if cur.start_us <= last.end_us:
            if cur.end_us > last.end_us:
                last.end_us = cur.end_us
        else:
            merged.append(Interval(cur.start_us, cur.end_us))
    return merged


def interval_union_us(intervals: List[Interval]) -> Decimal:
    return sum((x.dur_us for x in merge_intervals(intervals)), Decimal("0"))


# -----------------------------
# kernel 行对象
# -----------------------------

@dataclass
class KernelRow:
    row_id: int
    step_id: Optional[str]
    device_id: Optional[str]
    model_id: Optional[str]
    task_id: Optional[str]
    stream_id: str
    name: str
    task_type: str
    op_state: str
    accelerator_core: str
    start_us: Decimal
    end_us: Decimal
    duration_us: Decimal
    wait_us: Decimal
    total_cost_us: Decimal
    context_id: Optional[str]
    clipped_start_us: Decimal
    clipped_end_us: Decimal
    clipped_duration_us: Decimal


# -----------------------------
# 主逻辑
# -----------------------------

def resolve_window_us(args) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    if args.window_start_ns is not None and args.window_start_us is not None:
        raise ValueError("--window-start-ns 和 --window-start-us 不能同时指定")
    if args.window_end_ns is not None and args.window_end_us is not None:
        raise ValueError("--window-end-ns 和 --window-end-us 不能同时指定")

    start_us = None
    end_us = None

    if args.window_start_ns is not None:
        start_us = ns_to_us_decimal(args.window_start_ns)
    elif args.window_start_us is not None:
        start_us = parse_decimal(args.window_start_us)

    if args.window_end_ns is not None:
        end_us = ns_to_us_decimal(args.window_end_ns)
    elif args.window_end_us is not None:
        end_us = parse_decimal(args.window_end_us)

    if (start_us is None) ^ (end_us is None):
        raise ValueError("窗口起止必须同时提供,或者都不提供")

    if start_us is not None and end_us is not None and end_us <= start_us:
        raise ValueError("window_end 必须大于 window_start")

    return start_us, end_us


def load_kernel_rows(csv_path: Path,
                     explicit_window_start_us: Optional[Decimal],
                     explicit_window_end_us: Optional[Decimal]) -> Tuple[List[KernelRow], Dict[str, Any]]:
    rows: List[KernelRow] = []

    total_rows = 0
    bad_rows = 0
    used_effective_cols = False

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("CSV 没有表头")

        headers = set(reader.fieldnames)
        if COL_START_US not in headers:
            raise ValueError(f"缺少列: {COL_START_US}")
        if COL_DURATION_US not in headers:
            raise ValueError(f"缺少列: {COL_DURATION_US}")

        has_effective = (
            COL_EFFECTIVE_START_US in headers
            and COL_EFFECTIVE_END_US in headers
            and COL_EFFECTIVE_DURATION_US in headers
        )
        used_effective_cols = has_effective

        for idx, row in enumerate(reader, start=1):
            total_rows += 1

            start_us = parse_decimal(row.get(COL_START_US))
            duration_us = parse_decimal(row.get(COL_DURATION_US))
            wait_us = parse_decimal(row.get(COL_WAIT_US)) or Decimal("0")

            if start_us is None or duration_us is None or duration_us < 0:
                bad_rows += 1
                continue

            end_us = start_us + duration_us

            if has_effective:
                clipped_start_us = parse_decimal(row.get(COL_EFFECTIVE_START_US))
                clipped_end_us = parse_decimal(row.get(COL_EFFECTIVE_END_US))
                clipped_duration_us = parse_decimal(row.get(COL_EFFECTIVE_DURATION_US))
                if (
                    clipped_start_us is None
                    or clipped_end_us is None
                    or clipped_duration_us is None
                    or clipped_end_us < clipped_start_us
                ):
                    bad_rows += 1
                    continue
            else:
                # 没有 effective 列时,如果显式提供了窗口,则临时 clip；否则直接用原始区间
                if explicit_window_start_us is not None and explicit_window_end_us is not None:
                    clipped_start_us = max(start_us, explicit_window_start_us)
                    clipped_end_us = min(end_us, explicit_window_end_us)
                    clipped_duration_us = max(Decimal("0"), clipped_end_us - clipped_start_us)
                    if clipped_duration_us <= 0:
                        # 理论上 slice 过的文件不应进来,但这里兜底
                        continue
                else:
                    clipped_start_us = start_us
                    clipped_end_us = end_us
                    clipped_duration_us = duration_us

            task_type = (row.get(COL_TYPE) or row.get(COL_ALT_TYPE) or "").strip()
            stream_id = (row.get(COL_STREAM_ID) or "").strip()
            if stream_id == "":
                stream_id = "UNKNOWN"

            rows.append(
                KernelRow(
                    row_id=idx,
                    step_id=(row.get(COL_STEP_ID) or "").strip() or None,
                    device_id=(row.get(COL_DEVICE_ID) or "").strip() or None,
                    model_id=(row.get(COL_MODEL_ID) or "").strip() or None,
                    task_id=(row.get(COL_TASK_ID) or "").strip() or None,
                    stream_id=stream_id,
                    name=(row.get(COL_NAME) or "").strip(),
                    task_type=task_type,
                    op_state=(row.get(COL_OP_STATE) or "").strip(),
                    accelerator_core=(row.get(COL_ACCELERATOR) or "").strip(),
                    start_us=start_us,
                    end_us=end_us,
                    duration_us=duration_us,
                    wait_us=wait_us,
                    total_cost_us=duration_us + wait_us,
                    context_id=(row.get(COL_CONTEXT_ID) or "").strip() or None,
                    clipped_start_us=clipped_start_us,
                    clipped_end_us=clipped_end_us,
                    clipped_duration_us=clipped_duration_us,
                )
            )

    meta = {
        "total_rows": total_rows,
        "bad_rows": bad_rows,
        "loaded_rows": len(rows),
        "used_effective_cols": used_effective_cols,
    }
    return rows, meta


def build_stream_summary(rows: List[KernelRow]) -> Dict[str, Any]:
    by_stream: Dict[str, List[KernelRow]] = defaultdict(list)
    for r in rows:
        by_stream[r.stream_id].append(r)

    stream_summary: Dict[str, Any] = {}
    for stream_id, items in by_stream.items():
        intervals = [Interval(x.clipped_start_us, x.clipped_end_us) for x in items]
        merged = merge_intervals(intervals)

        step_counter = Counter(x.step_id or "UNKNOWN" for x in items)
        type_counter = Counter(x.task_type or "UNKNOWN" for x in items)
        accel_counter = Counter(x.accelerator_core or "UNKNOWN" for x in items)

        top_by_duration = sorted(items, key=lambda x: (-x.clipped_duration_us, x.row_id))[:5]
        top_by_total_cost = sorted(items, key=lambda x: (-x.total_cost_us, x.row_id))[:5]

        stream_summary[stream_id] = {
            "kernel_count": len(items),
            "step_ids": sorted(step_counter.keys()),
            "dominant_step_id": step_counter.most_common(1)[0][0] if step_counter else None,
            "dominant_task_type": type_counter.most_common(1)[0][0] if type_counter else None,
            "dominant_accelerator_core": accel_counter.most_common(1)[0][0] if accel_counter else None,
            "start_us": decimal_to_str(min(x.clipped_start_us for x in items)),
            "end_us": decimal_to_str(max(x.clipped_end_us for x in items)),
            "wall_us": decimal_to_str(
                max(x.clipped_end_us for x in items) - min(x.clipped_start_us for x in items)
            ),
            "sum_duration_us": decimal_to_str(sum((x.clipped_duration_us for x in items), Decimal("0"))),
            "sum_wait_us": decimal_to_str(sum((x.wait_us for x in items), Decimal("0"))),
            "sum_total_cost_us": decimal_to_str(sum((x.total_cost_us for x in items), Decimal("0"))),
            "busy_union_us": decimal_to_str(sum((m.dur_us for m in merged), Decimal("0"))),
            "merged_segment_count": len(merged),
            "top_kernels_by_duration": [
                kernel_brief(x) for x in top_by_duration
            ],
            "top_kernels_by_total_cost": [
                kernel_brief(x) for x in top_by_total_cost
            ],
        }

    return {
        "stream_count": len(stream_summary),
        "streams": stream_summary,
    }


def kernel_brief(x: KernelRow) -> Dict[str, Any]:
    wait_ratio = Decimal("0")
    if x.total_cost_us > 0:
        wait_ratio = x.wait_us / x.total_cost_us

    return {
        "row_id": x.row_id,
        "step_id": x.step_id,
        "stream_id": x.stream_id,
        "name": x.name,
        "task_type": x.task_type,
        "accelerator_core": x.accelerator_core,
        "start_us": decimal_to_str(x.start_us),
        "end_us": decimal_to_str(x.end_us),
        "duration_us": decimal_to_str(x.duration_us),
        "wait_us": decimal_to_str(x.wait_us),
        "total_cost_us": decimal_to_str(x.total_cost_us),
        "clipped_start_us": decimal_to_str(x.clipped_start_us),
        "clipped_end_us": decimal_to_str(x.clipped_end_us),
        "clipped_duration_us": decimal_to_str(x.clipped_duration_us),
        "wait_ratio": float(wait_ratio),
    }


def build_top_kernels(rows: List[KernelRow], topk: int) -> Dict[str, Any]:
    top_by_duration = sorted(rows, key=lambda x: (-x.clipped_duration_us, x.row_id))[:topk]
    top_by_total_cost = sorted(rows, key=lambda x: (-x.total_cost_us, x.row_id))[:topk]

    return {
        "top_by_duration": [kernel_brief(x) for x in top_by_duration],
        "top_by_total_cost": [kernel_brief(x) for x in top_by_total_cost],
    }


def build_global_busy_and_bubbles(rows: List[KernelRow],
                                  explicit_window_start_us: Optional[Decimal],
                                  explicit_window_end_us: Optional[Decimal],
                                  max_bubbles: int,
                                  neighbor_count: int) -> Dict[str, Any]:
    if not rows:
        return {
            "window_start_us": None,
            "window_end_us": None,
            "capture_wall_us": "0",
            "global_busy_union_us": "0",
            "global_gap_us": "0",
            "global_gap_ratio": 0.0,
            "merged_segment_count": 0,
            "bubble_candidates": [],
        }

    # 分析窗口优先用显式窗口,否则用切片后的有效区间范围
    data_start = min(x.clipped_start_us for x in rows)
    data_end = max(x.clipped_end_us for x in rows)

    window_start_us = explicit_window_start_us if explicit_window_start_us is not None else data_start
    window_end_us = explicit_window_end_us if explicit_window_end_us is not None else data_end

    intervals = [Interval(x.clipped_start_us, x.clipped_end_us) for x in rows]
    merged = merge_intervals(intervals)
    global_busy_union_us = sum((m.dur_us for m in merged), Decimal("0"))
    capture_wall_us = max(Decimal("0"), window_end_us - window_start_us)
    global_gap_us = max(Decimal("0"), capture_wall_us - global_busy_union_us)
    global_gap_ratio = float(global_gap_us / capture_wall_us) if capture_wall_us > 0 else 0.0

    # bubbles
    bubbles: List[Dict[str, Any]] = []

    if merged:
        # prelaunch
        if merged[0].start_us > window_start_us:
            bubbles.append({
                "scope": "prelaunch",
                "start_us": window_start_us,
                "end_us": merged[0].start_us,
                "duration_us": merged[0].start_us - window_start_us,
            })

        # internal
        for left, right in zip(merged[:-1], merged[1:]):
            if right.start_us > left.end_us:
                bubbles.append({
                    "scope": "internal",
                    "start_us": left.end_us,
                    "end_us": right.start_us,
                    "duration_us": right.start_us - left.end_us,
                })

        # tail
        if merged[-1].end_us < window_end_us:
            bubbles.append({
                "scope": "tail",
                "start_us": merged[-1].end_us,
                "end_us": window_end_us,
                "duration_us": window_end_us - merged[-1].end_us,
            })
    else:
        bubbles.append({
            "scope": "global_empty",
            "start_us": window_start_us,
            "end_us": window_end_us,
            "duration_us": capture_wall_us,
        })

    # 给每个 bubble 找左右邻居
    rows_by_end = sorted(rows, key=lambda x: (x.clipped_end_us, x.row_id))
    ends = [x.clipped_end_us for x in rows_by_end]

    rows_by_start = sorted(rows, key=lambda x: (x.clipped_start_us, x.row_id))
    starts = [x.clipped_start_us for x in rows_by_start]

    enriched_bubbles = []
    for i, b in enumerate(sorted(bubbles, key=lambda x: (-x["duration_us"], x["start_us"]))[:max_bubbles], start=1):
        left_idx = bisect_right(ends, b["start_us"])
        left_neighbors = rows_by_end[max(0, left_idx - neighbor_count):left_idx]
        left_neighbors = sorted(left_neighbors, key=lambda x: (-x.clipped_end_us, x.row_id))

        right_idx = bisect_left(starts, b["end_us"])
        right_neighbors = rows_by_start[right_idx:right_idx + neighbor_count]
        right_neighbors = sorted(right_neighbors, key=lambda x: (x.clipped_start_us, x.row_id))

        enriched_bubbles.append({
            "bubble_id": f"bubble_{i}",
            "scope": b["scope"],
            "start_us": decimal_to_str(b["start_us"]),
            "end_us": decimal_to_str(b["end_us"]),
            "duration_us": decimal_to_str(b["duration_us"]),
            "duration_ms": float(b["duration_us"] / Decimal("1000")),
            "left_neighbors": [kernel_brief(x) for x in left_neighbors],
            "right_neighbors": [kernel_brief(x) for x in right_neighbors],
        })

    return {
        "window_start_us": decimal_to_str(window_start_us),
        "window_end_us": decimal_to_str(window_end_us),
        "capture_wall_us": decimal_to_str(capture_wall_us),
        "global_busy_union_us": decimal_to_str(global_busy_union_us),
        "global_gap_us": decimal_to_str(global_gap_us),
        "global_gap_ratio": global_gap_ratio,
        "merged_segment_count": len(merged),
        "bubble_candidates": enriched_bubbles,
    }


def build_markdown(rows: List[KernelRow],
                   stream_summary: Dict[str, Any],
                   top_kernels: Dict[str, Any],
                   global_bubbles: Dict[str, Any],
                   meta: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# Kernel Slice Analysis")
    lines.append("")
    lines.append("这是 `kernel_details_slice.csv` 的摘要层,不替代原始切片表。")
    lines.append("")
    lines.append("## Overview")
    lines.append("")
    lines.append(f"- loaded rows: {meta['loaded_rows']}")
    lines.append(f"- bad rows skipped: {meta['bad_rows']}")
    lines.append(f"- used effective columns: {meta['used_effective_cols']}")
    lines.append(f"- stream count: {stream_summary['stream_count']}")
    lines.append(f"- analysis window: [{global_bubbles['window_start_us']}, {global_bubbles['window_end_us']}] us")
    lines.append(f"- capture wall: {global_bubbles['capture_wall_us']} us ({format_us(Decimal(global_bubbles['capture_wall_us']))})")
    lines.append(f"- global busy union: {global_bubbles['global_busy_union_us']} us ({format_us(Decimal(global_bubbles['global_busy_union_us']))})")
    lines.append(f"- global gap ratio: {global_bubbles['global_gap_ratio']:.4f}")
    lines.append("")

    lines.append("## Stream Summary")
    lines.append("")
    for stream_id, info in sorted(
        stream_summary["streams"].items(),
        key=lambda kv: Decimal(kv[1]["busy_union_us"]),
        reverse=True,
    ):
        lines.append(
            f"- stream {stream_id}: "
            f"kernel_count={info['kernel_count']}, "
            f"busy_union={info['busy_union_us']}us, "
            f"sum_duration={info['sum_duration_us']}us, "
            f"sum_wait={info['sum_wait_us']}us, "
            f"dominant_type={info['dominant_task_type']}, "
            f"dominant_core={info['dominant_accelerator_core']}"
        )
    lines.append("")

    lines.append("## Top Kernels by Duration")
    lines.append("")
    for x in top_kernels["top_by_duration"][:10]:
        lines.append(
            f"- row#{x['row_id']} stream={x['stream_id']} name=`{x['name']}` "
            f"type={x['task_type']} core={x['accelerator_core']} "
            f"clipped_dur={x['clipped_duration_us']}us total_cost={x['total_cost_us']}us "
            f"wait_ratio={x['wait_ratio']:.4f}"
        )
    lines.append("")

    lines.append("## Top Kernels by Total Cost")
    lines.append("")
    for x in top_kernels["top_by_total_cost"][:10]:
        lines.append(
            f"- row#{x['row_id']} stream={x['stream_id']} name=`{x['name']}` "
            f"type={x['task_type']} core={x['accelerator_core']} "
            f"dur={x['duration_us']}us wait={x['wait_us']}us total_cost={x['total_cost_us']}us "
            f"wait_ratio={x['wait_ratio']:.4f}"
        )
    lines.append("")

    lines.append("## Bubble Candidates")
    lines.append("")
    if not global_bubbles["bubble_candidates"]:
        lines.append("- no bubble candidates")
        lines.append("")
    else:
        for b in global_bubbles["bubble_candidates"][:10]:
            lines.append(
                f"- {b['bubble_id']} scope={b['scope']} "
                f"[{b['start_us']}, {b['end_us']}] us "
                f"dur={b['duration_us']}us"
            )
            if b["left_neighbors"]:
                lines.append("  - left neighbors:")
                for k in b["left_neighbors"]:
                    lines.append(
                        f"    - row#{k['row_id']} stream={k['stream_id']} name=`{k['name']}` "
                        f"type={k['task_type']} dur={k['clipped_duration_us']}us"
                    )
            if b["right_neighbors"]:
                lines.append("  - right neighbors:")
                for k in b["right_neighbors"]:
                    lines.append(
                        f"    - row#{k['row_id']} stream={k['stream_id']} name=`{k['name']}` "
                        f"type={k['task_type']} dur={k['clipped_duration_us']}us"
                    )
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="总结 kernel_details_slice.csv 的 stream/top-kernel/bubble 信息")
    parser.add_argument("input_csv", help="输入 kernel_details_slice.csv")
    parser.add_argument("--outdir", default="kernel_summary", help="输出目录")

    parser.add_argument("--topk", type=int, default=30, help="top kernels 数量")
    parser.add_argument("--max-bubbles", type=int, default=20, help="最多输出多少 bubble candidates")
    parser.add_argument("--neighbor-count", type=int, default=3, help="每个 bubble 两侧保留多少邻接 kernel")

    parser.add_argument("--window-start-ns", type=int, default=None, help="可选,显式窗口起始 ns")
    parser.add_argument("--window-end-ns", type=int, default=None, help="可选,显式窗口结束 ns")
    parser.add_argument("--window-start-us", type=str, default=None, help="可选,显式窗口起始 us")
    parser.add_argument("--window-end-us", type=str, default=None, help="可选,显式窗口结束 us")

    args = parser.parse_args()

    input_path = Path(args.input_csv)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    explicit_window_start_us, explicit_window_end_us = resolve_window_us(args)
    rows, meta = load_kernel_rows(
        csv_path=input_path,
        explicit_window_start_us=explicit_window_start_us,
        explicit_window_end_us=explicit_window_end_us,
    )

    stream_summary = build_stream_summary(rows)
    top_kernels = build_top_kernels(rows, topk=args.topk)
    global_bubbles = build_global_busy_and_bubbles(
        rows=rows,
        explicit_window_start_us=explicit_window_start_us,
        explicit_window_end_us=explicit_window_end_us,
        max_bubbles=args.max_bubbles,
        neighbor_count=args.neighbor_count,
    )
    md = build_markdown(
        rows=rows,
        stream_summary=stream_summary,
        top_kernels=top_kernels,
        global_bubbles=global_bubbles,
        meta=meta,
    )

    with (outdir / "stream_summary.json").open("w", encoding="utf-8") as f:
        json.dump(stream_summary, f, ensure_ascii=False, indent=2)

    with (outdir / "top_kernels.json").open("w", encoding="utf-8") as f:
        json.dump(top_kernels, f, ensure_ascii=False, indent=2)

    with (outdir / "bubble_candidates.json").open("w", encoding="utf-8") as f:
        json.dump(global_bubbles, f, ensure_ascii=False, indent=2)

    (outdir / "kernel_analysis.md").write_text(md, encoding="utf-8")

    print(json.dumps({
        "input_csv": str(input_path),
        "outdir": str(outdir),
        "loaded_rows": meta["loaded_rows"],
        "bad_rows": meta["bad_rows"],
        "stream_count": stream_summary["stream_count"],
        "bubble_candidate_count": len(global_bubbles["bubble_candidates"]),
        "outputs": [
            str(outdir / "stream_summary.json"),
            str(outdir / "top_kernels.json"),
            str(outdir / "bubble_candidates.json"),
            str(outdir / "kernel_analysis.md"),
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
