from __future__ import annotations

import argparse
import os
import time
from collections import Counter
from pathlib import Path

from workflow_common import (
    child_run_logs_dir,
    collect_graph_mapping_target_ids,
    collect_graph_operator_span_ids,
    dump_json,
    extract_graph_alignment_rows,
    graph_source_line_violation,
    load_json,
    load_state,
    now_iso,
    run_child_script_with_logs,
    validate_code_location,
)


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
LOCK_SCHEMA_VERSION = "step6_wrapper_lock_v2"
STATUS_SCHEMA_VERSION = "step6_wrapper_status_v1"
TOTAL_SCRIPTS = 4
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


def lock_path_for_workspace(workspace_dir: Path) -> Path:
    return child_run_logs_dir(workspace_dir) / "step6_wrapper.lock.json"


def status_path_for_workspace(workspace_dir: Path) -> Path:
    return workspace_dir / "audit" / "step6_in_progress.json"


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_lock_payload(lock_path: Path) -> dict:
    if not lock_path.exists():
        return {}
    try:
        return load_json(lock_path)
    except Exception:
        return {}


def write_lock_payload(lock_path: Path, payload: dict) -> None:
    dump_json(lock_path, payload)


def write_wrapper_status(
    workspace_dir: Path,
    *,
    status: str,
    active_stage: str,
    stage_phase: str,
    script_index: int,
    total_scripts: int,
    current_child_script: str,
    current_child_log_path: str,
    last_child_meta_path: str,
    heartbeat_count: int = 0,
    output_line_count: int = 0,
    idle_seconds: float = 0.0,
    completed_stages: list[str] | None = None,
) -> None:
    dump_json(
        status_path_for_workspace(workspace_dir),
        {
            "schema_version": STATUS_SCHEMA_VERSION,
            "status": status,
            "pid": os.getpid(),
            "workspace_dir": str(workspace_dir),
            "active_stage": active_stage,
            "stage_phase": stage_phase,
            "script_index": script_index,
            "total_scripts": total_scripts,
            "current_child_script": current_child_script,
            "current_child_log_path": current_child_log_path,
            "last_child_meta_path": last_child_meta_path,
            "last_heartbeat_at": now_iso(),
            "heartbeat_count": heartbeat_count,
            "output_line_count": output_line_count,
            "idle_seconds": round(idle_seconds, 6),
            "completed_stages": list(completed_stages or []),
        },
    )


def acquire_wrapper_lock(workspace_dir: Path) -> Path:
    lock_path = lock_path_for_workspace(workspace_dir)
    existing = read_lock_payload(lock_path)
    if existing.get("status") == "running":
        existing_pid = int(existing.get("pid", 0) or 0)
        if process_is_alive(existing_pid) and existing_pid != os.getpid():
            raise RuntimeError(
                "检测到已有活跃的 Step 6 wrapper 实例正在运行；禁止重跑。"
                f" active_pid={existing_pid}, lock={lock_path}"
            )
    payload = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "status": "running",
        "pid": os.getpid(),
        "workspace_dir": str(workspace_dir),
        "started_at": now_iso(),
        "last_heartbeat_at": now_iso(),
        "active_stage": "initializing",
        "stage_phase": "initializing",
        "script_index": 0,
        "total_scripts": TOTAL_SCRIPTS,
        "completed_stages": [],
        "current_child_script": "",
        "current_child_log_path": "",
        "last_child_meta_path": "",
        "heartbeat_count": 0,
        "output_line_count": 0,
        "idle_seconds": 0.0,
    }
    write_lock_payload(lock_path, payload)
    write_wrapper_status(
        workspace_dir,
        status="running",
        active_stage="initializing",
        stage_phase="initializing",
        script_index=0,
        total_scripts=TOTAL_SCRIPTS,
        current_child_script="",
        current_child_log_path="",
        last_child_meta_path="",
        completed_stages=[],
    )
    return lock_path


def update_wrapper_state(
    workspace_dir: Path,
    lock_path: Path,
    *,
    status: str = "running",
    active_stage: str | None = None,
    completed_stage: str | None = None,
    current_child_script: str | None = None,
    current_child_log_path: str | None = None,
    last_child_meta_path: str | None = None,
    stage_phase: str | None = None,
    script_index: int | None = None,
    total_scripts: int | None = None,
    heartbeat_count: int | None = None,
    output_line_count: int | None = None,
    idle_seconds: float | None = None,
) -> dict:
    payload = read_lock_payload(lock_path)
    completed = payload.get("completed_stages", [])
    if not isinstance(completed, list):
        completed = []
    if completed_stage and completed_stage not in completed:
        completed.append(completed_stage)
    payload.update(
        {
            "schema_version": LOCK_SCHEMA_VERSION,
            "status": status,
            "pid": os.getpid(),
            "workspace_dir": str(workspace_dir),
            "last_heartbeat_at": now_iso(),
            "completed_stages": completed,
        }
    )
    if active_stage is not None:
        payload["active_stage"] = active_stage
    if current_child_script is not None:
        payload["current_child_script"] = current_child_script
    if current_child_log_path is not None:
        payload["current_child_log_path"] = current_child_log_path
    if last_child_meta_path is not None:
        payload["last_child_meta_path"] = last_child_meta_path
    if stage_phase is not None:
        payload["stage_phase"] = stage_phase
    if script_index is not None:
        payload["script_index"] = script_index
    if total_scripts is not None:
        payload["total_scripts"] = total_scripts
    if heartbeat_count is not None:
        payload["heartbeat_count"] = heartbeat_count
    if output_line_count is not None:
        payload["output_line_count"] = output_line_count
    if idle_seconds is not None:
        payload["idle_seconds"] = round(idle_seconds, 6)
    if status != "running":
        payload["ended_at"] = now_iso()
    write_lock_payload(lock_path, payload)
    write_wrapper_status(
        workspace_dir,
        status=str(payload.get("status", status)).strip(),
        active_stage=str(payload.get("active_stage", "")).strip(),
        stage_phase=str(payload.get("stage_phase", "")).strip(),
        script_index=int(payload.get("script_index", 0) or 0),
        total_scripts=int(payload.get("total_scripts", TOTAL_SCRIPTS) or TOTAL_SCRIPTS),
        current_child_script=str(payload.get("current_child_script", "")).strip(),
        current_child_log_path=str(payload.get("current_child_log_path", "")).strip(),
        last_child_meta_path=str(payload.get("last_child_meta_path", "")).strip(),
        heartbeat_count=int(payload.get("heartbeat_count", 0) or 0),
        output_line_count=int(payload.get("output_line_count", 0) or 0),
        idle_seconds=float(payload.get("idle_seconds", 0.0) or 0.0),
        completed_stages=completed,
    )
    return payload


def run_script(
    script_name: str,
    workspace_dir: Path,
    lock_path: Path,
    *,
    stage_token: str,
    script_index: int,
    total_scripts: int,
) -> dict:
    script_path = SCRIPT_DIR / script_name
    ensure(script_path.exists(), f"缺少子脚本: {script_path}")
    start_ts = time.perf_counter()
    print(f"[step6] start {script_name}", flush=True)
    update_wrapper_state(
        workspace_dir,
        lock_path,
        active_stage=f"{stage_token}_running",
        stage_phase="launching_child",
        script_index=script_index,
        total_scripts=total_scripts,
        current_child_script=script_name,
        current_child_log_path="",
        last_child_meta_path="",
        heartbeat_count=0,
        output_line_count=0,
        idle_seconds=0.0,
    )

    def _on_heartbeat(payload: dict) -> None:
        update_wrapper_state(
            workspace_dir,
            lock_path,
            active_stage=f"{stage_token}_running",
            stage_phase="child_running",
            script_index=script_index,
            total_scripts=total_scripts,
            current_child_script=script_name,
            current_child_log_path=str(payload.get("combined_log_path", "")),
            last_child_meta_path=str(payload.get("metadata_path", "")),
            heartbeat_count=int(payload.get("heartbeat_count", 0) or 0),
            output_line_count=int(payload.get("output_line_count", 0) or 0),
            idle_seconds=float(payload.get("idle_seconds", 0.0) or 0.0),
        )

    metadata = run_child_script_with_logs(
        script_path=script_path,
        workspace_dir=workspace_dir,
        repo_root=REPO_ROOT,
        log_prefix=f"step6_{Path(script_name).stem}",
        heartbeat_seconds=30,
        on_heartbeat=_on_heartbeat,
    )
    update_wrapper_state(
        workspace_dir,
        lock_path,
        active_stage=f"{stage_token}_post_checking",
        stage_phase="post_checking",
        script_index=script_index,
        total_scripts=total_scripts,
        current_child_script=script_name,
        current_child_log_path=str(metadata["combined_log_path"]),
        last_child_meta_path=str(metadata["metadata_path"]),
        heartbeat_count=int(metadata.get("heartbeat_count", 0) or 0),
        output_line_count=int(metadata.get("output_line_count", 0) or 0),
        idle_seconds=0.0,
    )
    duration_s = time.perf_counter() - start_ts
    print(
        f"[step6] done  {script_name} ({duration_s:.2f}s, 输出 {metadata['output_line_count']} 行, "
        f"log={metadata['combined_log_path']})",
        flush=True,
    )
    return metadata


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


def require_string_list(value: object, label: str) -> list[str]:
    ensure(isinstance(value, list), f"{label} 必须是列表，说明 Step5 promotion 写入了非法 schema。")
    normalized = [str(item).strip() for item in value if str(item).strip()]
    return normalized


def collect_frozen_graph_span_ids(graph_mapping_targets: dict) -> set[str]:
    rows = graph_mapping_targets.get("rows", [])
    ensure(isinstance(rows, list), "graph_mapping_targets.json.rows 必须是列表。")
    graph_mapping_target_ids = collect_graph_mapping_target_ids(graph_mapping_targets)
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
    alignment_items = extract_graph_alignment_rows(graph_alignment)
    operator_span_ids = collect_graph_operator_span_ids(graph_operator_spans)
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
    invalid_code_location_count = 0
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
        code_location = str(item.get("code_location", "")).strip()
        if not validate_code_location(code_location):
            invalid_code_location_count += 1
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
        "invalid_code_location_count": invalid_code_location_count,
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
        f"invalid_code_location_count={diagnostics.get('invalid_code_location_count', 0)}, "
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
    graph_repo_root = Path(str(state.get("inputs", {}).get("code_repo_path", "")).strip())
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
        ensure(
            diagnostics["invalid_code_location_count"] == 0,
            "Step6 检测到 graph_span_alignment 存在非法 code_location；graph 正式 mapping 只能消费 file:line。"
            + diagnostics_suffix,
        )
        source_line_violations = []
        for index, item in enumerate(alignment_items):
            code_location = str(item.get("code_location", "")).strip()
            violation = graph_source_line_violation(code_location, graph_repo_root)
            if violation:
                source_line_violations.append(
                    f"graph_span_alignment[{index}] code_location 仍停在 {violation}: {code_location}"
                )
        ensure(
            not source_line_violations,
            "Step6 检测到 graph_span_alignment 仍停在模块调用边界、构造行或 replay 入口，拒绝继续渲染正式交付物。"
            + diagnostics_suffix
            + f" violations={source_line_violations[:10]}",
        )


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    lock_path = acquire_wrapper_lock(workspace_dir)
    span_code_mapping_path = workspace_dir / "artifacts" / "mapping" / "span_code_mapping.json"
    annotated_trace_path = workspace_dir / "output" / "trace_view.annotated.json"
    stream_timeline_path = workspace_dir / "artifacts" / "timeline" / "stream_span_timeline.json"
    try:
        update_wrapper_state(
            workspace_dir,
            lock_path,
            active_stage="validate_inputs_running",
            stage_phase="preflight_validation",
            script_index=0,
            total_scripts=TOTAL_SCRIPTS,
            current_child_script="",
            current_child_log_path="",
            last_child_meta_path="",
            heartbeat_count=0,
            output_line_count=0,
            idle_seconds=0.0,
        )
        state = load_state(workspace_dir)
        validate_step6_inputs(workspace_dir, state)
        update_wrapper_state(
            workspace_dir,
            lock_path,
            active_stage="validate_inputs_done",
            stage_phase="post_check_done",
            script_index=0,
            total_scripts=TOTAL_SCRIPTS,
            current_child_script="",
            current_child_log_path="",
            last_child_meta_path="",
            heartbeat_count=0,
            output_line_count=0,
            idle_seconds=0.0,
        )

        map_meta = run_script(
            "map_spans_to_code.py",
            workspace_dir,
            lock_path,
            stage_token="map_spans_to_code",
            script_index=1,
            total_scripts=TOTAL_SCRIPTS,
        )
        ensure_state_artifact(
            workspace_dir,
            artifact_key="span_code_mapping_path",
            expected_path=span_code_mapping_path,
            flag_key="span_mapping_done",
        )
        update_wrapper_state(
            workspace_dir,
            lock_path,
            active_stage="map_spans_to_code_done",
            completed_stage="map_spans_to_code",
            stage_phase="post_check_done",
            script_index=1,
            total_scripts=TOTAL_SCRIPTS,
            current_child_script="map_spans_to_code.py",
            current_child_log_path=str(map_meta["combined_log_path"]),
            last_child_meta_path=str(map_meta["metadata_path"]),
            heartbeat_count=int(map_meta.get("heartbeat_count", 0) or 0),
            output_line_count=int(map_meta.get("output_line_count", 0) or 0),
            idle_seconds=0.0,
        )

        annotate_meta = run_script(
            "annotate_trace_view.py",
            workspace_dir,
            lock_path,
            stage_token="annotate_trace_view",
            script_index=2,
            total_scripts=TOTAL_SCRIPTS,
        )
        ensure_state_artifact(
            workspace_dir,
            artifact_key="annotated_trace_path",
            expected_path=annotated_trace_path,
            flag_key="annotated_trace_generated",
        )
        update_wrapper_state(
            workspace_dir,
            lock_path,
            active_stage="annotate_trace_view_done",
            completed_stage="annotate_trace_view",
            stage_phase="post_check_done",
            script_index=2,
            total_scripts=TOTAL_SCRIPTS,
            current_child_script="annotate_trace_view.py",
            current_child_log_path=str(annotate_meta["combined_log_path"]),
            last_child_meta_path=str(annotate_meta["metadata_path"]),
            heartbeat_count=int(annotate_meta.get("heartbeat_count", 0) or 0),
            output_line_count=int(annotate_meta.get("output_line_count", 0) or 0),
            idle_seconds=0.0,
        )

        timeline_meta = run_script(
            "render_stream_span_timeline.py",
            workspace_dir,
            lock_path,
            stage_token="render_stream_span_timeline",
            script_index=3,
            total_scripts=TOTAL_SCRIPTS,
        )
        ensure_state_artifact(
            workspace_dir,
            artifact_key="stream_span_timeline_path",
            expected_path=stream_timeline_path,
            flag_key="timeline_generated",
        )
        update_wrapper_state(
            workspace_dir,
            lock_path,
            active_stage="render_stream_span_timeline_done",
            completed_stage="render_stream_span_timeline",
            stage_phase="post_check_done",
            script_index=3,
            total_scripts=TOTAL_SCRIPTS,
            current_child_script="render_stream_span_timeline.py",
            current_child_log_path=str(timeline_meta["combined_log_path"]),
            last_child_meta_path=str(timeline_meta["metadata_path"]),
            heartbeat_count=int(timeline_meta.get("heartbeat_count", 0) or 0),
            output_line_count=int(timeline_meta.get("output_line_count", 0) or 0),
            idle_seconds=0.0,
        )

        write_outputs_meta = run_script(
            "write_render_outputs.py",
            workspace_dir,
            lock_path,
            stage_token="write_render_outputs",
            script_index=4,
            total_scripts=TOTAL_SCRIPTS,
        )
        ensure_render_outputs(workspace_dir)
        update_wrapper_state(
            workspace_dir,
            lock_path,
            status="passed",
            active_stage="done",
            completed_stage="write_render_outputs",
            stage_phase="completed",
            script_index=TOTAL_SCRIPTS,
            total_scripts=TOTAL_SCRIPTS,
            current_child_script="",
            current_child_log_path=str(write_outputs_meta["combined_log_path"]),
            last_child_meta_path=str(write_outputs_meta["metadata_path"]),
            heartbeat_count=int(write_outputs_meta.get("heartbeat_count", 0) or 0),
            output_line_count=int(write_outputs_meta.get("output_line_count", 0) or 0),
            idle_seconds=0.0,
        )
        return 0
    except Exception:
        failure_payload = read_lock_payload(lock_path)
        update_wrapper_state(
            workspace_dir,
            lock_path,
            status="failed",
            active_stage="failed",
            stage_phase="failed",
            script_index=int(failure_payload.get("script_index", 0) or 0),
            total_scripts=int(failure_payload.get("total_scripts", TOTAL_SCRIPTS) or TOTAL_SCRIPTS),
            current_child_script=str(failure_payload.get("current_child_script", "")).strip(),
            current_child_log_path=str(failure_payload.get("current_child_log_path", "")).strip(),
            last_child_meta_path=str(failure_payload.get("last_child_meta_path", "")).strip(),
            heartbeat_count=int(failure_payload.get("heartbeat_count", 0) or 0),
            output_line_count=int(failure_payload.get("output_line_count", 0) or 0),
            idle_seconds=float(failure_payload.get("idle_seconds", 0.0) or 0.0),
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
