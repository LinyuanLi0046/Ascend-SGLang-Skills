#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
process_profiling.py

把 chrome://tracing / Perfetto 可读的 trace json,转换成更适合大模型分析、
同时尽量紧凑的输出：

1) llm_trace_summary.md   主输入：先给大模型读这个
2) llm_trace_bundle.json  证据包：需要精确核对时再给
3) stats.json             本次处理统计与输出索引

设计目标：
- 内部统一用 int ns,避免大数 float 精度问题
- 把 X / B-E 标准化成 span(区间)
- 保留 span 树(parent/depth)
- 不生成逐边界 time slices,改为固定数量 coarse bins,防止体积爆炸
- 只为“重要 span”保留截断后的 args,避免 bundle 膨胀
- 适合已经切过窗口的 trace_slice.json,也支持直接处理完整 trace

推荐工作流：
1. 先用 slice 脚本切出可疑窗口
2. 再用本脚本生成 summary + bundle
3. 把 llm_trace_summary.md 作为主输入给 agent
4. 把 llm_trace_bundle.json 作为证据补充
5. 把 stats.json 作为处理元数据补充

示例：
python3 scripts/process_profiling.py trace_slice.json --outdir trace_llm --trace-unit us

如果 trace 里的 ts 长这样：
"ts": "1776493036180691.730"
那么一般应使用：
--trace-unit us
"""

import argparse
import copy
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# -----------------------------
# 解析 / 序列化
# -----------------------------

def load_trace(path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any], bool]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        if "traceEvents" in data:
            return data["traceEvents"], {k: v for k, v in data.items() if k != "traceEvents"}, True
        if "events" in data:
            return data["events"], {k: v for k, v in data.items() if k != "events"}, False
        raise ValueError("JSON 顶层是 dict,但没有 traceEvents/events 字段。")
    if isinstance(data, list):
        return data, {}, True
    raise ValueError("不支持的 trace json 格式。")


def dump_json(path: Path, obj: Any, pretty: bool = False) -> None:
    with path.open("w", encoding="utf-8") as f:
        if pretty:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        else:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def parse_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def trace_value_to_ns(value: Any, trace_unit: str) -> Optional[int]:
    d = parse_decimal(value)
    if d is None:
        return None
    if trace_unit == "ns":
        return int(d.to_integral_value(rounding=ROUND_DOWN))
    if trace_unit == "us":
        return int((d * Decimal(1000)).to_integral_value(rounding=ROUND_DOWN))
    raise ValueError(f"不支持的 trace_unit: {trace_unit}")


def get_ts_ns(evt: Dict[str, Any], trace_unit: str) -> Optional[int]:
    return trace_value_to_ns(evt.get("ts"), trace_unit)


def get_dur_ns(evt: Dict[str, Any], trace_unit: str) -> Optional[int]:
    return trace_value_to_ns(evt.get("dur"), trace_unit)


def compact_json(obj: Any, max_len: int = 240) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        s = str(obj)
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


def format_ns(ns: int) -> str:
    if ns >= 1_000_000_000:
        return f"{ns / 1_000_000_000:.3f}s"
    if ns >= 1_000_000:
        return f"{ns / 1_000_000:.3f}ms"
    if ns >= 1_000:
        return f"{ns / 1_000:.3f}us"
    return f"{ns}ns"


# -----------------------------
# 元数据 / track
# -----------------------------

def build_metadata_maps(events: List[Dict[str, Any]]) -> Tuple[Dict[Any, str], Dict[Tuple[Any, Any], str]]:
    process_names: Dict[Any, str] = {}
    thread_names: Dict[Tuple[Any, Any], str] = {}

    for evt in events:
        if evt.get("ph") != "M":
            continue
        name = evt.get("name")
        args = evt.get("args", {}) or {}
        pid = evt.get("pid")
        tid = evt.get("tid")

        if name == "process_name":
            proc_name = args.get("name")
            if proc_name is not None:
                process_names[pid] = str(proc_name)
        elif name == "thread_name":
            thr_name = args.get("name")
            if thr_name is not None:
                thread_names[(pid, tid)] = str(thr_name)

    return process_names, thread_names


def track_label(pid: Any, tid: Any,
                process_names: Dict[Any, str],
                thread_names: Dict[Tuple[Any, Any], str]) -> str:
    tn = thread_names.get((pid, tid))
    pn = process_names.get(pid)
    if tn and pn:
        return f"{tn}(pid={pid},tid={tid},proc={pn})"
    if tn:
        return f"{tn}(pid={pid},tid={tid})"
    if pn:
        return f"pid={pid},tid={tid},proc={pn}"
    return f"pid={pid},tid={tid}"


# -----------------------------
# 标准化成 spans / markers
# -----------------------------

def pair_be_events(events: List[Dict[str, Any]], trace_unit: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
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
                    "name": evt.get("name"),
                    "ts_ns": ts_ns,
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
                "name": evt.get("name"),
                "ts_ns": get_ts_ns(evt, trace_unit),
            })

    return intervals, anomalies


def normalize_trace(
    raw_events: List[Dict[str, Any]],
    trace_unit: str,
    process_names: Dict[Any, str],
    thread_names: Dict[Tuple[Any, Any], str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    返回:
    - spans: 标准化区间事件
    - markers: 点事件/计数器/其它带 ts 的非区间事件
    - anomalies
    """
    spans: List[Dict[str, Any]] = []
    markers: List[Dict[str, Any]] = []
    anomalies: List[Dict[str, Any]] = []

    # 原始顺序编号
    events = []
    for i, evt in enumerate(raw_events):
        evt2 = copy.deepcopy(evt)
        evt2["_orig_index"] = i
        events.append(evt2)

    # 1) 原生 X
    for evt in events:
        if evt.get("ph") != "X":
            continue
        ts_ns = get_ts_ns(evt, trace_unit)
        dur_ns = get_dur_ns(evt, trace_unit)
        if ts_ns is None or dur_ns is None or dur_ns < 0:
            continue

        pid = evt.get("pid")
        tid = evt.get("tid")
        spans.append({
            "src": "X",
            "pid": pid,
            "tid": tid,
            "track_label": track_label(pid, tid, process_names, thread_names),
            "name": str(evt.get("name", "")),
            "cat": str(evt.get("cat", "")),
            "start_ns": ts_ns,
            "dur_ns": dur_ns,
            "end_ns": ts_ns + dur_ns,
            "args": evt.get("args", {}) or {},
            "orig_begin_idx": evt["_orig_index"],
            "orig_end_idx": evt["_orig_index"],
        })

    # 2) B/E -> span
    be_pairs, be_anomalies = pair_be_events(events, trace_unit)
    anomalies.extend(be_anomalies)

    for pair in be_pairs:
        b = pair["begin"]
        e = pair["end"]
        b_ns = get_ts_ns(b, trace_unit)
        e_ns = get_ts_ns(e, trace_unit)
        if b_ns is None or e_ns is None or e_ns < b_ns:
            continue

        pid = b.get("pid")
        tid = b.get("tid")
        args = {}
        if isinstance(b.get("args"), dict):
            args.update(b["args"])
        if isinstance(e.get("args"), dict):
            args.update(e["args"])

        spans.append({
            "src": "BE",
            "pid": pid,
            "tid": tid,
            "track_label": track_label(pid, tid, process_names, thread_names),
            "name": str(b.get("name", "")),
            "cat": str(b.get("cat", "")),
            "start_ns": b_ns,
            "dur_ns": e_ns - b_ns,
            "end_ns": e_ns,
            "args": args,
            "orig_begin_idx": b["_orig_index"],
            "orig_end_idx": e["_orig_index"],
        })

    # 3) markers：保留除 M/B/E/X 外,其它有 ts 的事件
    skip_ph = {"M", "B", "E", "X"}
    for evt in events:
        ph = evt.get("ph")
        if ph in skip_ph:
            continue
        ts_ns = get_ts_ns(evt, trace_unit)
        if ts_ns is None:
            continue
        pid = evt.get("pid")
        tid = evt.get("tid")
        markers.append({
            "pid": pid,
            "tid": tid,
            "track_label": track_label(pid, tid, process_names, thread_names),
            "ph": str(ph),
            "name": str(evt.get("name", "")),
            "cat": str(evt.get("cat", "")),
            "ts_ns": ts_ns,
            "args": evt.get("args", {}) or {},
            "orig_idx": evt["_orig_index"],
        })

    spans.sort(key=lambda x: (x["start_ns"], -x["dur_ns"], x["orig_begin_idx"]))
    markers.sort(key=lambda x: (x["ts_ns"], x["orig_idx"]))
    return spans, markers, anomalies


# -----------------------------
# span 树 / depth / parent
# -----------------------------

def annotate_span_tree(spans: List[Dict[str, Any]]) -> None:
    by_track: Dict[Tuple[Any, Any], List[Dict[str, Any]]] = defaultdict(list)
    for s in spans:
        by_track[(s["pid"], s["tid"])].append(s)

    sid = 0
    for s in spans:
        s["id"] = sid
        s["parent"] = None
        s["depth"] = 0
        s["crossing"] = False
        sid += 1

    # 重新建一个 id -> obj 的便捷映射
    spans_by_id = {s["id"]: s for s in spans}

    for _, rows in by_track.items():
        rows.sort(key=lambda x: (x["start_ns"], -x["dur_ns"], x["id"]))
        stack: List[Dict[str, Any]] = []

        for row in rows:
            start_ns = row["start_ns"]
            end_ns = row["end_ns"]

            while stack and start_ns >= stack[-1]["end_ns"]:
                stack.pop()

            if stack:
                top = stack[-1]
                # 严格包含时判为 parent-child
                if end_ns <= top["end_ns"]:
                    row["parent"] = top["id"]
                    row["depth"] = top["depth"] + 1
                else:
                    row["crossing"] = True
                    row["parent"] = top["id"]
                    row["depth"] = top["depth"] + 1

            stack.append(row)

    # child ids
    for s in spans:
        s["children"] = []
    for s in spans:
        if s["parent"] is not None and s["parent"] in spans_by_id:
            spans_by_id[s["parent"]]["children"].append(s["id"])


def merged_busy_ns(intervals: List[Tuple[int, int]]) -> int:
    if not intervals:
        return 0
    intervals = sorted(intervals)
    merged: List[Tuple[int, int]] = []
    cs, ce = intervals[0]
    for s, e in intervals[1:]:
        if s <= ce:
            ce = max(ce, e)
        else:
            merged.append((cs, ce))
            cs, ce = s, e
    merged.append((cs, ce))
    return sum(e - s for s, e in merged)


# -----------------------------
# 字典 / 压缩 bundle
# -----------------------------

class StringTable:
    def __init__(self):
        self.values: List[str] = []
        self.index: Dict[str, int] = {}

    def add(self, s: str) -> int:
        s = s or ""
        if s not in self.index:
            self.index[s] = len(self.values)
            self.values.append(s)
        return self.index[s]


def build_compact_tables(spans: List[Dict[str, Any]], markers: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str], List[str], Dict[Tuple[Any, Any], int]]:
    tracks: List[Dict[str, Any]] = []
    track_index: Dict[Tuple[Any, Any], int] = {}
    names = StringTable()
    cats = StringTable()

    def ensure_track(pid: Any, tid: Any, label: str) -> int:
        key = (pid, tid)
        if key not in track_index:
            track_index[key] = len(tracks)
            tracks.append({
                "id": track_index[key],
                "pid": pid,
                "tid": tid,
                "label": label,
                "busy_ns": 0,
                "root_ids": [],
            })
        return track_index[key]

    for s in spans:
        t = ensure_track(s["pid"], s["tid"], s["track_label"])
        s["track_id"] = t
        s["name_id"] = names.add(s["name"])
        s["cat_id"] = cats.add(s["cat"])

    for m in markers:
        t = ensure_track(m["pid"], m["tid"], m["track_label"])
        m["track_id"] = t
        m["name_id"] = names.add(m["name"])
        m["cat_id"] = cats.add(m["cat"])

    # busy / roots
    by_track_intervals: Dict[int, List[Tuple[int, int]]] = defaultdict(list)
    for s in spans:
        by_track_intervals[s["track_id"]].append((s["start_ns"], s["end_ns"]))
        if s["parent"] is None:
            tracks[s["track_id"]]["root_ids"].append(s["id"])

    for tid, ivs in by_track_intervals.items():
        tracks[tid]["busy_ns"] = merged_busy_ns(ivs)

    for t in tracks:
        t["root_ids"].sort()

    return tracks, names.values, cats.values, track_index


# -----------------------------
# coarse bins(替代爆大的逐边界 time_slices)
# -----------------------------

def build_coarse_bins(
    spans: List[Dict[str, Any]],
    markers: List[Dict[str, Any]],
    num_bins: int,
    top_spans_per_bin: int,
) -> Tuple[int, int, List[Dict[str, Any]]]:
    ts_candidates: List[int] = []
    ts_candidates.extend(s["start_ns"] for s in spans)
    ts_candidates.extend(s["end_ns"] for s in spans)
    ts_candidates.extend(m["ts_ns"] for m in markers)

    if not ts_candidates:
        return 0, 0, []

    global_start = min(ts_candidates)
    global_end = max(ts_candidates)
    duration = max(1, global_end - global_start)

    num_bins = max(1, num_bins)
    bin_size = max(1, (duration + num_bins - 1) // num_bins)

    active_tracks: List[set] = [set() for _ in range(num_bins)]
    span_cover: List[Dict[int, int]] = [defaultdict(int) for _ in range(num_bins)]

    for s in spans:
        start = s["start_ns"]
        end = s["end_ns"]
        if end <= start:
            continue

        i0 = max(0, min(num_bins - 1, (start - global_start) // bin_size))
        i1 = max(0, min(num_bins - 1, (max(start, end - 1) - global_start) // bin_size))

        for i in range(i0, i1 + 1):
            b_start = global_start + i * bin_size
            b_end = min(global_end, b_start + bin_size)
            overlap = min(end, b_end) - max(start, b_start)
            if overlap > 0:
                active_tracks[i].add(s["track_id"])
                span_cover[i][s["id"]] += overlap

    # marker 也让对应 track 在该 bin 上激活
    for m in markers:
        ts = m["ts_ns"]
        i = max(0, min(num_bins - 1, (ts - global_start) // bin_size))
        active_tracks[i].add(m["track_id"])

    bins: List[Dict[str, Any]] = []
    for i in range(num_bins):
        b_start = global_start + i * bin_size
        b_end = min(global_end, b_start + bin_size)
        if b_end <= b_start:
            continue

        cov = span_cover[i]
        top_ids = [sid for sid, _ in sorted(cov.items(), key=lambda x: (-x[1], x[0]))[:top_spans_per_bin]]

        bins.append({
            "start_ns": b_start - global_start,
            "dur_ns": b_end - b_start,
            "active_track_ids": sorted(active_tracks[i]),
            "top_span_ids": top_ids,
        })

    return global_start, global_end, bins


# -----------------------------
# 选重要 span / marker
# -----------------------------

def select_important_span_ids(
    spans: List[Dict[str, Any]],
    tracks: List[Dict[str, Any]],
    bins: List[Dict[str, Any]],
    top_spans: int,
    top_roots_per_track: int,
) -> List[int]:
    important = set()

    # 全局最长 spans
    for s in sorted(spans, key=lambda x: (-x["dur_ns"], x["id"]))[:top_spans]:
        important.add(s["id"])

    # 每个 track 最长 root spans
    by_id = {s["id"]: s for s in spans}
    for t in tracks:
        roots = [by_id[rid] for rid in t["root_ids"] if rid in by_id]
        roots = sorted(roots, key=lambda x: (-x["dur_ns"], x["start_ns"]))[:top_roots_per_track]
        for r in roots:
            important.add(r["id"])
            for cid in sorted(r["children"], key=lambda cid: (-by_id[cid]["dur_ns"], by_id[cid]["start_ns"]))[:top_roots_per_track]:
                important.add(cid)

    # 每个 bin 的 dominant span
    for b in bins:
        for sid in b["top_span_ids"]:
            important.add(sid)

    return sorted(important)


def select_markers(markers: List[Dict[str, Any]], max_markers: int) -> List[Dict[str, Any]]:
    # 优先保留带 args 的,再按时间
    markers = sorted(markers, key=lambda x: (0 if x["args"] else 1, x["ts_ns"], x["orig_idx"]))
    return sorted(markers[:max_markers], key=lambda x: (x["ts_ns"], x["orig_idx"]))


# -----------------------------
# 摘要 markdown
# -----------------------------

def write_summary_md(
    path: Path,
    spans: List[Dict[str, Any]],
    tracks: List[Dict[str, Any]],
    names: List[str],
    cats: List[str],
    markers: List[Dict[str, Any]],
    bins: List[Dict[str, Any]],
    important_span_ids: List[int],
    anomalies: List[Dict[str, Any]],
    global_start_ns: int,
    global_end_ns: int,
    max_roots_per_track_md: int,
    max_children_per_root_md: int,
) -> None:
    by_id = {s["id"]: s for s in spans}
    duration_ns = max(0, global_end_ns - global_start_ns)

    lines: List[str] = []
    lines.append("# LLM Trace Summary")
    lines.append("")
    lines.append("先读这个文件；如果要核对细节,再查同目录的 llm_trace_bundle.json。")
    lines.append("")
    lines.append("## Window")
    lines.append("")
    lines.append(f"- trace duration: {format_ns(duration_ns)}")
    lines.append(f"- interval spans: {len(spans)}")
    lines.append(f"- kept markers: {len(markers)}")
    lines.append(f"- tracks: {len(tracks)}")
    lines.append(f"- anomalies: {len(anomalies)}")
    lines.append("")

    # 热门 track
    lines.append("## Hot tracks")
    lines.append("")
    for t in sorted(tracks, key=lambda x: (-x["busy_ns"], x["id"]))[:12]:
        lines.append(
            f"- track#{t['id']} {t['label']} | busy={format_ns(t['busy_ns'])} | root_spans={len(t['root_ids'])}"
        )
    lines.append("")

    # 全局最长 root spans
    root_spans = [s for s in spans if s["parent"] is None]
    lines.append("## Long root spans")
    lines.append("")
    for s in sorted(root_spans, key=lambda x: (-x["dur_ns"], x["start_ns"]))[:20]:
        rel_start = s["start_ns"] - global_start_ns
        rel_end = s["end_ns"] - global_start_ns
        lines.append(
            f"- span#{s['id']} track#{s['track_id']} {s['track_label']} | "
            f"[{format_ns(rel_start)}, {format_ns(rel_end)}] dur={format_ns(s['dur_ns'])} | "
            f"name={s['name']} | cat={s['cat']}"
        )
    lines.append("")

    # 全局最长非 root spans
    non_roots = [s for s in spans if s["parent"] is not None]
    if non_roots:
        lines.append("## Long child spans")
        lines.append("")
        for s in sorted(non_roots, key=lambda x: (-x["dur_ns"], x["start_ns"]))[:20]:
            rel_start = s["start_ns"] - global_start_ns
            rel_end = s["end_ns"] - global_start_ns
            lines.append(
                f"- span#{s['id']} parent#{s['parent']} track#{s['track_id']} | "
                f"[{format_ns(rel_start)}, {format_ns(rel_end)}] dur={format_ns(s['dur_ns'])} | "
                f"name={s['name']} | depth={s['depth']}"
            )
        lines.append("")

    # coarse bins
    lines.append("## Coarse global timeline")
    lines.append("")
    non_empty_bins = [b for b in bins if b["active_track_ids"] or b["top_span_ids"]]
    for i, b in enumerate(non_empty_bins[:64]):
        active_tracks_text = ", ".join(f"track#{tid}" for tid in b["active_track_ids"]) or "(none)"
        top_span_text_parts = []
        for sid in b["top_span_ids"]:
            s = by_id.get(sid)
            if s is None:
                continue
            top_span_text_parts.append(f"span#{sid}:{s['name']}")
        top_span_text = ", ".join(top_span_text_parts) or "(none)"
        lines.append(
            f"- bin#{i} [{format_ns(b['start_ns'])}, {format_ns(b['start_ns'] + b['dur_ns'])}] | "
            f"active={active_tracks_text} | dominant={top_span_text}"
        )
    lines.append("")

    # per track tree excerpt
    lines.append("## Per-track span tree excerpt")
    lines.append("")
    for t in sorted(tracks, key=lambda x: (-x["busy_ns"], x["id"]))[:12]:
        lines.append(f"### track#{t['id']} {t['label']}")
        roots = [by_id[rid] for rid in t["root_ids"] if rid in by_id]
        roots = sorted(roots, key=lambda x: (-x["dur_ns"], x["start_ns"]))[:max_roots_per_track_md]
        if not roots:
            lines.append("- (no root spans)")
            lines.append("")
            continue

        for r in roots:
            rel_start = r["start_ns"] - global_start_ns
            rel_end = r["end_ns"] - global_start_ns
            lines.append(
                f"- span#{r['id']} [{format_ns(rel_start)}, {format_ns(rel_end)}] "
                f"dur={format_ns(r['dur_ns'])} name={r['name']} cat={r['cat']}"
            )
            children = [by_id[cid] for cid in r["children"] if cid in by_id]
            children = sorted(children, key=lambda x: (-x["dur_ns"], x["start_ns"]))[:max_children_per_root_md]
            for c in children:
                c_rel_start = c["start_ns"] - global_start_ns
                c_rel_end = c["end_ns"] - global_start_ns
                lines.append(
                    f"  - child span#{c['id']} [{format_ns(c_rel_start)}, {format_ns(c_rel_end)}] "
                    f"dur={format_ns(c['dur_ns'])} name={c['name']} depth={c['depth']}"
                )
        lines.append("")

    # anomalies
    if anomalies:
        lines.append("## Anomalies")
        lines.append("")
        for a in anomalies[:50]:
            lines.append(f"- {compact_json(a, 300)}")
        if len(anomalies) > 50:
            lines.append(f"- ... total {len(anomalies)} anomalies")
        lines.append("")

    # how to read bundle
    lines.append("## How to use llm_trace_bundle.json")
    lines.append("")
    lines.append("- spans 数组中的每一项字段顺序为：")
    lines.append("  [id, track_id, parent_id, start_rel_ns, dur_ns, name_id, cat_id, depth, src]")
    lines.append("- src: 0 表示原始 X,1 表示由 B/E 配对得到")
    lines.append("- bins 数组中的每一项字段顺序为：")
    lines.append("  [start_rel_ns, dur_ns, active_track_ids, top_span_ids]")
    lines.append("- span_args 只保留“重要 span”的精简 args,用于节省体积")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# -----------------------------
# bundle
# -----------------------------

def build_bundle(
    spans: List[Dict[str, Any]],
    tracks: List[Dict[str, Any]],
    names: List[str],
    cats: List[str],
    markers: List[Dict[str, Any]],
    bins: List[Dict[str, Any]],
    important_span_ids: List[int],
    anomalies: List[Dict[str, Any]],
    global_start_ns: int,
    global_end_ns: int,
    max_arg_chars: int,
) -> Dict[str, Any]:
    by_id = {s["id"]: s for s in spans}
    important_set = set(important_span_ids)

    # 压缩 spans
    span_rows: List[List[Any]] = []
    for s in spans:
        span_rows.append([
            s["id"],
            s["track_id"],
            s["parent"],
            s["start_ns"] - global_start_ns,
            s["dur_ns"],
            s["name_id"],
            s["cat_id"],
            s["depth"],
            0 if s["src"] == "X" else 1,
        ])

    # 只给重要 span 保留 args
    span_args: Dict[str, str] = {}
    for sid in important_span_ids:
        s = by_id.get(sid)
        if not s:
            continue
        if s["args"]:
            span_args[str(sid)] = compact_json(s["args"], max_arg_chars)

    # marker 只保留少量、简化字段
    marker_rows: List[List[Any]] = []
    for m in markers:
        marker_rows.append([
            m["track_id"],
            m["ts_ns"] - global_start_ns,
            m["name_id"],
            m["cat_id"],
            m["ph"],
            compact_json(m["args"], max_arg_chars) if m["args"] else "",
        ])

    # bins 压缩
    bin_rows: List[List[Any]] = []
    for b in bins:
        bin_rows.append([
            b["start_ns"],
            b["dur_ns"],
            b["active_track_ids"],
            b["top_span_ids"],
        ])

    root_ids = [s["id"] for s in spans if s["parent"] is None]
    root_ids.sort(key=lambda sid: (-by_id[sid]["dur_ns"], by_id[sid]["start_ns"]))

    bundle = {
        "schema": "compact_llm_trace/v2",
        "time_unit": "relative_ns",
        "summary": {
            "trace_start_ns_rel": 0,
            "trace_end_ns_rel": max(0, global_end_ns - global_start_ns),
            "duration_ns": max(0, global_end_ns - global_start_ns),
            "interval_span_count": len(spans),
            "kept_marker_count": len(markers),
            "track_count": len(tracks),
            "anomaly_count": len(anomalies),
            "hot_track_ids": [t["id"] for t in sorted(tracks, key=lambda x: (-x["busy_ns"], x["id"]))[:12]],
            "top_root_span_ids": root_ids[:20],
            "important_span_ids": important_span_ids[:200],
        },
        "legend": {
            "span_fields": ["id", "track_id", "parent_id", "start_rel_ns", "dur_ns", "name_id", "cat_id", "depth", "src"],
            "marker_fields": ["track_id", "ts_rel_ns", "name_id", "cat_id", "ph", "args_short"],
            "bin_fields": ["start_rel_ns", "dur_ns", "active_track_ids", "top_span_ids"],
            "src_values": {"0": "X", "1": "BE"},
        },
        "tracks": tracks,
        "names": names,
        "cats": cats,
        "spans": span_rows,
        "span_args": span_args,
        "markers": marker_rows,
        "bins": bin_rows,
        "anomalies": anomalies[:100],
    }
    return bundle


# -----------------------------
# main
# -----------------------------

def main():
    parser = argparse.ArgumentParser(description="Compact trace -> LLM-friendly summary + bundle")
    parser.add_argument("input", help="输入 trace json 文件(建议是切过窗口的 trace_slice.json)")
    parser.add_argument("--outdir", default="trace_llm", help="输出目录")
    parser.add_argument(
        "--trace-unit",
        choices=["us", "ns"],
        default="us",
        help="trace 中 ts/dur 的单位。若 ts 像 1776493036180691.730 通常应使用 us",
    )
    parser.add_argument("--num-bins", type=int, default=128, help="coarse timeline 的 bin 数量")
    parser.add_argument("--top-spans", type=int, default=60, help="全局最长 span 保留数量")
    parser.add_argument("--top-roots-per-track", type=int, default=8, help="每个 track 摘要里保留的 root span 数")
    parser.add_argument("--top-spans-per-bin", type=int, default=3, help="每个 bin 保留的 dominant span 数")
    parser.add_argument("--max-markers", type=int, default=200, help="bundle 中最多保留多少 marker")
    parser.add_argument("--max-arg-chars", type=int, default=240, help="args 截断长度")
    parser.add_argument("--pretty-json", action="store_true", help="输出 pretty JSON 默认是紧凑 JSON")
    args = parser.parse_args()

    input_path = Path(args.input)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    raw_events, meta, _ = load_trace(input_path)
    process_names, thread_names = build_metadata_maps(raw_events)

    spans, markers, anomalies = normalize_trace(
        raw_events=raw_events,
        trace_unit=args.trace_unit,
        process_names=process_names,
        thread_names=thread_names,
    )

    annotate_span_tree(spans)
    tracks, names, cats, _ = build_compact_tables(spans, markers)

    global_start_ns, global_end_ns, bins = build_coarse_bins(
        spans=spans,
        markers=markers,
        num_bins=args.num_bins,
        top_spans_per_bin=args.top_spans_per_bin,
    )

    important_span_ids = select_important_span_ids(
        spans=spans,
        tracks=tracks,
        bins=bins,
        top_spans=args.top_spans,
        top_roots_per_track=args.top_roots_per_track,
    )

    kept_markers = select_markers(markers, args.max_markers)

    summary_md_path = outdir / "llm_trace_summary.md"
    bundle_json_path = outdir / "llm_trace_bundle.json"

    write_summary_md(
        path=summary_md_path,
        spans=spans,
        tracks=tracks,
        names=names,
        cats=cats,
        markers=kept_markers,
        bins=bins,
        important_span_ids=important_span_ids,
        anomalies=anomalies,
        global_start_ns=global_start_ns,
        global_end_ns=global_end_ns,
        max_roots_per_track_md=args.top_roots_per_track,
        max_children_per_root_md=args.top_roots_per_track,
    )

    bundle = build_bundle(
        spans=spans,
        tracks=tracks,
        names=names,
        cats=cats,
        markers=kept_markers,
        bins=bins,
        important_span_ids=important_span_ids,
        anomalies=anomalies,
        global_start_ns=global_start_ns,
        global_end_ns=global_end_ns,
        max_arg_chars=args.max_arg_chars,
    )
    dump_json(bundle_json_path, bundle, pretty=args.pretty_json)

    stats = {
        "input_file": str(input_path),
        "outdir": str(outdir),
        "trace_unit": args.trace_unit,
        "raw_event_count": len(raw_events),
        "interval_span_count": len(spans),
        "kept_marker_count": len(kept_markers),
        "track_count": len(tracks),
        "anomaly_count": len(anomalies),
        "bin_count": len(bins),
        "outputs": [
            str(summary_md_path),
            str(bundle_json_path),
        ],
    }
    dump_json(outdir / "stats.json", stats, pretty=True)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

# python process_profiling.py "/home/l00951279/LongCat/profiling/trace_slice.json" --outdir ./output
