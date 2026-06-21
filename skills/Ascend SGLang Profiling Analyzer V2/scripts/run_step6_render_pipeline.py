from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from workflow_common import load_json, load_state


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
ALLOWED_OPERATOR_EVIDENCE_KINDS = {
    "torch_call",
    "torch_functional_call",
    "torch_npu_call",
    "npu_custom_op",
    "triton_call",
    "tensor_expression",
    "collective_call",
    "device_cache_op",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按固定顺序执行 Step 6 渲染流水线并做后验检查。")
    parser.add_argument("--workspace-dir", required=True)
    return parser


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run_script(script_name: str, workspace_dir: Path) -> None:
    script_path = SCRIPT_DIR / script_name
    ensure(script_path.exists(), f"缺少子脚本: {script_path}")
    start_ts = time.perf_counter()
    print(f"[step6] start {script_name}", flush=True)
    subprocess.run(
        [sys.executable, str(script_path), "--workspace-dir", str(workspace_dir)],
        cwd=REPO_ROOT,
        check=True,
    )
    duration_s = time.perf_counter() - start_ts
    print(f"[step6] done  {script_name} ({duration_s:.2f}s)", flush=True)


def ensure_state_artifact(workspace_dir: Path, artifact_key: str, expected_path: Path, flag_key: str) -> None:
    state = load_state(workspace_dir)
    actual_path = str(state.get("artifacts", {}).get(artifact_key, "")).strip()
    ensure(actual_path == str(expected_path), f"{artifact_key} 未正确回写到 state: {actual_path}")
    ensure(expected_path.exists() and expected_path.is_file(), f"{artifact_key} 对应文件不存在: {expected_path}")
    ensure(bool(state.get("flags", {}).get(flag_key)), f"{flag_key} 未置为 true。")


def ensure_render_outputs(workspace_dir: Path) -> None:
    render_result_path = workspace_dir / "output" / "render_result.json"
    render_report_path = workspace_dir / "output" / "render_report.md"
    ensure(render_result_path.exists(), f"缺少 Step 6 正式 JSON: {render_result_path}")
    ensure(render_report_path.exists(), f"缺少 Step 6 正式报告: {render_report_path}")
    payload = load_json(render_result_path)
    status = str(payload.get("status", "")).strip()
    ensure(status == "passed", f"render_result.json.status 必须为 passed，当前为 {status!r}")


def extract_graph_alignment_items(payload: dict) -> list[dict]:
    for key in ("items", "rows"):
        value = payload.get(key, [])
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def require_string_list(value: object, label: str) -> list[str]:
    ensure(isinstance(value, list), f"{label} 必须是列表，说明 Step5 promotion 写入了非法 schema。")
    normalized = [str(item).strip() for item in value if str(item).strip()]
    return normalized


def collect_frozen_graph_span_ids(graph_mapping_targets: dict) -> set[str]:
    rows = graph_mapping_targets.get("rows", [])
    ensure(isinstance(rows, list), "graph_mapping_targets.json.rows 必须是列表。")
    graph_mapping_target_ids = {
        str(row.get("span_id", "")).strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("span_id", "")).strip()
    }
    ensure(
        graph_mapping_target_ids,
        "Step6 检测到 graph_mapping_targets.json 为空；正式 frozen graph scope 只能来自 graph_mapping_targets.json，不允许回退到 graph_execution_plan 旧 scope。",
    )
    return graph_mapping_target_ids


def build_graph_alignment_diagnostics(
    graph_plan: dict,
    graph_mapping_targets: dict,
    graph_alignment: dict,
    graph_operator_spans: dict,
) -> dict:
    alignment_items = extract_graph_alignment_items(graph_alignment)
    operator_span_ids = {
        str(item.get("graph_operator_span_id", "")).strip()
        for item in graph_operator_spans.get("rows", [])
        if isinstance(item, dict) and str(item.get("graph_operator_span_id", "")).strip()
    }
    frozen_graph_span_ids = collect_frozen_graph_span_ids(graph_mapping_targets)
    alignment_span_ids = {
        str(item.get("span_id", "")).strip()
        for item in alignment_items
        if str(item.get("span_id", "")).strip()
    }
    location_kind_counter: Counter[str] = Counter()
    operator_evidence_counter: Counter[str] = Counter()
    missing_span_id_count = 0
    missing_operator_span_id_count = 0
    invalid_operator_ref_count = 0
    bad_location_kind_count = 0
    unresolved_count = 0
    missing_operator_evidence_count = 0
    for item in alignment_items:
        location_kind = str(item.get("location_kind", "")).strip()
        operator_evidence_kind = str(item.get("operator_evidence_kind", "")).strip()
        graph_operator_span_id = str(item.get("graph_operator_span_id", "")).strip()
        location_kind_counter[location_kind or "<missing>"] += 1
        operator_evidence_counter[operator_evidence_kind or "<missing>"] += 1
        if not str(item.get("span_id", "")).strip():
            missing_span_id_count += 1
        if not graph_operator_span_id:
            missing_operator_span_id_count += 1
        elif graph_operator_span_id not in operator_span_ids:
            invalid_operator_ref_count += 1
        if location_kind != "operator_call":
            bad_location_kind_count += 1
        if item.get("requires_further_drilldown") is not False:
            unresolved_count += 1
        if operator_evidence_kind not in ALLOWED_OPERATOR_EVIDENCE_KINDS:
            missing_operator_evidence_count += 1
    missing_frozen_graph_span_ids = sorted(frozen_graph_span_ids - alignment_span_ids)
    return {
        "alignment_items": alignment_items,
        "operator_span_ids": operator_span_ids,
        "frozen_graph_span_ids": frozen_graph_span_ids,
        "missing_frozen_graph_span_ids": missing_frozen_graph_span_ids,
        "missing_span_id_count": missing_span_id_count,
        "missing_operator_span_id_count": missing_operator_span_id_count,
        "invalid_operator_ref_count": invalid_operator_ref_count,
        "bad_location_kind_count": bad_location_kind_count,
        "unresolved_count": unresolved_count,
        "missing_operator_evidence_count": missing_operator_evidence_count,
        "location_kind_breakdown": dict(location_kind_counter),
        "operator_evidence_breakdown": dict(operator_evidence_counter),
    }


def format_graph_alignment_diagnostics(diagnostics: dict) -> str:
    return (
        " [graph_alignment_diagnostics: "
        f"frozen_span_count={len(diagnostics.get('frozen_graph_span_ids', []))}, "
        f"alignment_item_count={len(diagnostics.get('alignment_items', []))}, "
        f"missing_frozen_span_count={len(diagnostics.get('missing_frozen_graph_span_ids', []))}, "
        f"missing_span_id_count={diagnostics.get('missing_span_id_count', 0)}, "
        f"missing_operator_span_id_count={diagnostics.get('missing_operator_span_id_count', 0)}, "
        f"invalid_operator_ref_count={diagnostics.get('invalid_operator_ref_count', 0)}, "
        f"bad_location_kind_count={diagnostics.get('bad_location_kind_count', 0)}, "
        f"unresolved_count={diagnostics.get('unresolved_count', 0)}, "
        f"missing_operator_evidence_count={diagnostics.get('missing_operator_evidence_count', 0)}, "
        f"location_kind_breakdown={diagnostics.get('location_kind_breakdown', {})}, "
        f"operator_evidence_breakdown={diagnostics.get('operator_evidence_breakdown', {})}"
        "]"
    )


def validate_step6_inputs(workspace_dir: Path, state: dict) -> None:
    artifacts = state.get("artifacts", {})
    graph_plan_path = Path(str(artifacts.get("graph_execution_plan_path", "")).strip())
    graph_forward_context_path = Path(str(artifacts.get("graph_forward_context_path", "")).strip())
    graph_mapping_targets_path = Path(str(artifacts.get("graph_mapping_targets_path", "")).strip())
    graph_alignment_path = Path(str(artifacts.get("graph_span_alignment_path", "")).strip())
    graph_operator_spans_path = Path(str(artifacts.get("graph_operator_spans_path", "")).strip())
    ensure(graph_plan_path.exists(), f"Step6 缺少 graph_execution_plan.json: {graph_plan_path}")
    ensure(graph_forward_context_path.exists(), f"Step6 缺少 graph_forward_context.json: {graph_forward_context_path}")
    ensure(graph_mapping_targets_path.exists(), f"Step6 缺少 graph_mapping_targets.json: {graph_mapping_targets_path}")
    ensure(graph_alignment_path.exists(), f"Step6 缺少 graph_span_alignment.json: {graph_alignment_path}")
    ensure(graph_operator_spans_path.exists(), f"Step6 缺少 graph_operator_spans.json: {graph_operator_spans_path}")
    graph_plan = load_json(graph_plan_path)
    graph_forward_context = load_json(graph_forward_context_path)
    graph_mapping_targets = load_json(graph_mapping_targets_path)
    graph_alignment = load_json(graph_alignment_path)
    graph_operator_spans = load_json(graph_operator_spans_path)
    diagnostics = build_graph_alignment_diagnostics(graph_plan, graph_mapping_targets, graph_alignment, graph_operator_spans)
    alignment_items = diagnostics["alignment_items"]
    operator_span_ids = diagnostics["operator_span_ids"]
    identified_graph_span_ids = diagnostics["frozen_graph_span_ids"]
    diagnostics_suffix = format_graph_alignment_diagnostics(diagnostics)
    if identified_graph_span_ids:
        ensure(
            graph_plan.get("mapping_granularity") == "per_span_forward_code",
            "Step6 检测到 graph candidate spans，但 graph_execution_plan.mapping_granularity 仍不是 per_span_forward_code。",
        )
        ensure(
            graph_forward_context.get("mapping_granularity") == "per_span_forward_code",
            "Step6 检测到 graph candidate spans，但 graph_forward_context.mapping_granularity 仍不是 per_span_forward_code。",
        )
        ensure(
            alignment_items,
            "Step6 检测到 graph candidate spans，但 graph_span_alignment.json 缺少可消费 items/rows；拒绝继续静默 fallback 到 phase hint 或全量 stack 扫描。"
            + diagnostics_suffix,
        )
        ensure(
            not diagnostics["missing_frozen_graph_span_ids"],
            "Step6 检测到 graph_execution_plan 已冻结的 graph spans 未全部获得逐 span graph_alignment，拒绝继续渲染正式交付物。"
            + diagnostics_suffix,
        )
        ensure(
            diagnostics["missing_span_id_count"] == 0,
            "Step6 检测到 graph_span_alignment 不是逐 span 结构：存在缺少 span_id 的条目，拒绝继续运行 wrapper。"
            + diagnostics_suffix,
        )
        ensure(
            diagnostics["missing_operator_span_id_count"] == 0,
            "Step6 检测到 graph_span_alignment 缺少 graph_operator_span_id，拒绝继续运行 wrapper。"
            + diagnostics_suffix,
        )
        ensure(
            diagnostics["invalid_operator_ref_count"] == 0,
            "Step6 检测到 graph_span_alignment 中存在无法回溯到 graph_operator_spans.json 的 graph_operator_span_id。"
            + diagnostics_suffix,
        )
        ensure(
            diagnostics["bad_location_kind_count"] == 0,
            "Step6 检测到 graph_span_alignment 仍包含非 operator_call 的 graph 条目，拒绝继续渲染正式交付物。"
            + diagnostics_suffix,
        )
        ensure(
            diagnostics["unresolved_count"] == 0,
            "Step6 检测到 graph_span_alignment 仍包含 requires_further_drilldown=true 的 graph 条目，拒绝继续渲染正式交付物。"
            + diagnostics_suffix,
        )
        ensure(
            diagnostics["missing_operator_evidence_count"] == 0,
            "Step6 检测到 graph_span_alignment 缺少或使用了非法的 operator_evidence_kind，拒绝继续渲染正式交付物。"
            + diagnostics_suffix,
        )


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    state = load_state(workspace_dir)
    validate_step6_inputs(workspace_dir, state)

    span_code_mapping_path = workspace_dir / "artifacts" / "mapping" / "span_code_mapping.json"
    annotated_trace_path = workspace_dir / "output" / "trace_view.annotated.json"
    stream_timeline_path = workspace_dir / "artifacts" / "timeline" / "stream_span_timeline.json"

    run_script("map_spans_to_code.py", workspace_dir)
    ensure_state_artifact(
        workspace_dir,
        artifact_key="span_code_mapping_path",
        expected_path=span_code_mapping_path,
        flag_key="span_mapping_done",
    )

    run_script("annotate_trace_view.py", workspace_dir)
    ensure_state_artifact(
        workspace_dir,
        artifact_key="annotated_trace_path",
        expected_path=annotated_trace_path,
        flag_key="annotated_trace_generated",
    )

    run_script("render_stream_span_timeline.py", workspace_dir)
    ensure_state_artifact(
        workspace_dir,
        artifact_key="stream_span_timeline_path",
        expected_path=stream_timeline_path,
        flag_key="timeline_generated",
    )

    run_script("write_render_outputs.py", workspace_dir)
    ensure_render_outputs(workspace_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
