#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
按 trace 的时间窗口ns截取 kernel_details.csv。

默认按“区间重叠”保留：
kernel interval = [Start Time(us), Start Time(us) + Duration(us))

保留条件：
    kernel_end_us > window_start_us
and kernel_start_us < window_end_us

并且可选输出裁剪后的窗口内有效区间：
- effective_start_us
- effective_end_us
- effective_duration_us

用法示例：
python3 scripts/slice_kernel_csv.py \
  kernel_details.csv \
  kernel_details_slice.csv \
  --start-ns 1776493036180691730 \
  --end-ns   1776493037180691730

如果想输出裁剪列：
python3 scripts/slice_kernel_csv.py \
  kernel_details.csv \
  kernel_details_slice.csv \
  --start-ns 1776493036180691730 \
  --end-ns   1776493037180691730 \
  --add-effective-columns
"""

import argparse
import csv
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path


START_COL = "Start Time(us)"
DUR_COL = "Duration(us)"


def parse_decimal(value: str):
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
    # 保留微秒小数，避免 float 精度问题
    return Decimal(ns_value) / Decimal(1000)


def decimal_to_str(d: Decimal) -> str:
    # 避免科学计数法
    return format(d, "f")


def main():
    parser = argparse.ArgumentParser(description="按 trace 的 ns 时间窗口截取 kernel_details.csv")
    parser.add_argument("input_csv", help="输入 kernel_details.csv")
    parser.add_argument("output_csv", help="输出截取后的 csv")
    parser.add_argument("--start-ns", type=int, required=True, help="窗口起始时间戳ns")
    parser.add_argument("--end-ns", type=int, required=True, help="窗口结束时间戳ns")
    parser.add_argument(
        "--add-effective-columns",
        action="store_true",
        help="额外输出 effective_start_us / effective_end_us / effective_duration_us",
    )
    args = parser.parse_args()

    if args.end_ns <= args.start_ns:
        raise ValueError("end_ns 必须大于 start_ns")

    input_path = Path(args.input_csv)
    output_path = Path(args.output_csv)

    window_start_us = ns_to_us_decimal(args.start_ns)
    window_end_us = ns_to_us_decimal(args.end_ns)

    kept = 0
    total = 0
    skipped_bad_rows = 0

    with input_path.open("r", encoding="utf-8-sig", newline="") as fin:
        reader = csv.DictReader(fin)

        if reader.fieldnames is None:
            raise ValueError("CSV 没有表头")

        if START_COL not in reader.fieldnames:
            raise ValueError(f"缺少列: {START_COL}")
        if DUR_COL not in reader.fieldnames:
            raise ValueError(f"缺少列: {DUR_COL}")

        out_fields = list(reader.fieldnames)
        if args.add_effective_columns:
            out_fields += [
                "effective_start_us",
                "effective_end_us",
                "effective_duration_us",
            ]

        with output_path.open("w", encoding="utf-8-sig", newline="") as fout:
            writer = csv.DictWriter(fout, fieldnames=out_fields)
            writer.writeheader()

            for row in reader:
                total += 1

                start_us = parse_decimal(row.get(START_COL))
                dur_us = parse_decimal(row.get(DUR_COL))

                if start_us is None or dur_us is None:
                    skipped_bad_rows += 1
                    continue

                if dur_us < 0:
                    skipped_bad_rows += 1
                    continue

                end_us = start_us + dur_us

                # overlap 条件：
                # [start_us, end_us) 与 [window_start_us, window_end_us) 有交集
                if end_us > window_start_us and start_us < window_end_us:
                    out_row = dict(row)

                    if args.add_effective_columns:
                        eff_start = max(start_us, window_start_us)
                        eff_end = min(end_us, window_end_us)
                        eff_dur = max(Decimal("0"), eff_end - eff_start)

                        out_row["effective_start_us"] = decimal_to_str(eff_start)
                        out_row["effective_end_us"] = decimal_to_str(eff_end)
                        out_row["effective_duration_us"] = decimal_to_str(eff_dur)

                    writer.writerow(out_row)
                    kept += 1

    print(f"window_start_us = {decimal_to_str(window_start_us)}")
    print(f"window_end_us   = {decimal_to_str(window_end_us)}")
    print(f"total_rows      = {total}")
    print(f"kept_rows       = {kept}")
    print(f"skipped_bad_rows= {skipped_bad_rows}")
    print(f"output_csv      = {output_path}")


if __name__ == "__main__":
    main()

# python3 scripts/slice_kernel_csv.py \
#   kernel_details.csv \
#   kernel_details_slice.csv \
#   --start-ns 1776493038560004900 \
#   --end-ns 1776493038600497200 \
#   --add-effective-columns
