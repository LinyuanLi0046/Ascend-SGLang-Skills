from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from span_scope_rules import match_exclude_rule, match_force_include_rule
from workflow_common import dump_json, load_json, load_state, save_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查 Step 3 的硬件 span 作用域门禁。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def check_scope_gate_for_workspace(workspace_dir: Path) -> dict:
    state = load_state(workspace_dir)
    classified = load_json(Path(state["artifacts"]["classified_spans_path"]))

    excluded_matches: list[str] = []
    force_include_missed: list[str] = []
    semantic_without_stream: list[str] = []
    unexpected_runtime_control_semantic: list[str] = []
    semantic_count = 0
    unexpected_name_counter: Counter[str] = Counter()
    unexpected_class_counter: Counter[str] = Counter()

    for stream in classified.get("streams", []):
        for span in stream.get("spans", []):
            span_id = str(span.get("span_id", ""))
            span_name = str(span.get("span_name", ""))
            if not span.get("exclude_from_code_mapping"):
                semantic_count += 1
            if span.get("scope_class") == "hardware_semantic_candidate" and not span.get("has_stream_id", False):
                semantic_without_stream.append(span_id)
            if not span.get("exclude_from_code_mapping") and match_exclude_rule(span_name, {}):
                excluded_matches.append(span_id)
                unexpected_name_counter[span_name] += 1
                unexpected_class_counter[str(span.get("semantic_class", "")) or "<missing>"] += 1
            if span.get("exclude_from_code_mapping") and match_force_include_rule(span_name, {}):
                force_include_missed.append(span_id)
            if not span.get("exclude_from_code_mapping") and str(span.get("semantic_class", "")).strip() == "runtime_control":
                unexpected_runtime_control_semantic.append(span_id)
                unexpected_name_counter[span_name] += 1
                unexpected_class_counter["runtime_control"] += 1

    severe_issues = []
    if semantic_without_stream:
        severe_issues.append("存在缺少 streamId/stream_id 的 span 仍进入了 semantic 集合。")
    if force_include_missed:
        severe_issues.append("存在强制保留的功能性算子被错误排除。")
    if unexpected_runtime_control_semantic:
        severe_issues.append("存在 runtime_control span 仍进入 semantic 集合。")

    warnings = []
    if excluded_matches:
        warnings.append("部分明显应排除 span 仍进入 semantic 集合。")

    result = {
        "status": "passed" if not severe_issues else "failed",
        "scope_summary": classified.get("scope_summary", {}),
        "semantic_span_count": semantic_count,
        "violations": {
            "semantic_without_stream": semantic_without_stream,
            "excluded_matches_still_semantic": excluded_matches,
            "force_include_missed": force_include_missed,
            "unexpected_runtime_control_semantic": unexpected_runtime_control_semantic,
        },
        "unexpected_semantic_scope_summary": {
            "top_span_names": unexpected_name_counter.most_common(10),
            "top_semantic_classes": unexpected_class_counter.most_common(10),
        },
        "warnings": warnings,
        "severe_issues": severe_issues,
    }

    result_path = workspace_dir / "output" / "scope_gate_result.json"
    dump_json(result_path, result)
    state["artifacts"]["scope_gate_result_path"] = str(result_path)
    state["flags"]["scope_gate_passed"] = result["status"] == "passed"
    save_state(workspace_dir, state)
    return result


def main() -> int:
    args = build_parser().parse_args()
    check_scope_gate_for_workspace(Path(args.workspace_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
