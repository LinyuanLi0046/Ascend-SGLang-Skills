from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from agent_contracts import AGENT_CONFIG, effective_agent_config, resolve_workspace_paths
from step4_bootstrap_plan import (
    REQUIRED_ARTIFACTS_BY_TARGET as STEP4_REQUIRED_ARTIFACTS_BY_TARGET,
    REQUIRED_FLAGS_BY_TARGET as STEP4_REQUIRED_FLAGS_BY_TARGET,
    TARGET_SCRIPT_SEQUENCE as STEP4_TARGET_SCRIPT_SEQUENCE,
    step4_bootstrap_lock_path,
    step4_bootstrap_status_path,
)
from step5_graph_bootstrap_plan import (
    REQUIRED_ARTIFACTS_BY_TARGET as STEP5_REQUIRED_ARTIFACTS_BY_TARGET,
    REQUIRED_FLAGS_BY_TARGET as STEP5_REQUIRED_FLAGS_BY_TARGET,
    TARGET_SCRIPT_SEQUENCE as STEP5_TARGET_SCRIPT_SEQUENCE,
    step5_graph_bootstrap_lock_path,
    step5_graph_bootstrap_status_path,
)
from workflow_common import dump_json, load_json, load_state


TASK_FILENAME_BY_AGENT = {
    "profiling_preprocessor": "preprocess_task.json",
    "timeline_analyst": "timeline_task.json",
    "step4_bootstrap_runner": "step4_bootstrap_task.json",
    "stack_mapper": "stack_mapping_task.json",
    "graph_bootstrap_runner": "graph_bootstrap_task.json",
    "graph_path_analyst": "graph_path_task.json",
    "profiling_debugger": "debug_task.json",
    "artifact_renderer": "render_task.json",
    "artifact_validator": "validation_task.json",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成正式子 agent 的 task input JSON。")
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument("--agent-name", required=True, choices=sorted(TASK_FILENAME_BY_AGENT))
    return parser


def _collect_identified_graph_span_ids(graph_plan: dict[str, Any]) -> list[str]:
    identified = graph_plan.get("identified_graph_span_ids", [])
    if not isinstance(identified, list):
        return []
    return sorted({str(item).strip() for item in identified if str(item).strip()})


def _collect_phase_window_span_ids(graph_plan: dict[str, Any]) -> list[str]:
    span_ids: set[str] = set()
    for window in graph_plan.get("phase_windows", []):
        if not isinstance(window, dict):
            continue
        for span_id in window.get("span_ids", []):
            normalized = str(span_id).strip()
            if normalized:
                span_ids.add(normalized)
    return sorted(span_ids)


def _collect_frozen_graph_span_ids(graph_plan: dict[str, Any]) -> list[str]:
    return sorted(set(_collect_identified_graph_span_ids(graph_plan)) | set(_collect_phase_window_span_ids(graph_plan)))


def _collect_graph_mapping_target_ids(graph_mapping_targets: dict[str, Any]) -> list[str]:
    rows = graph_mapping_targets.get("rows", [])
    if not isinstance(rows, list):
        return []
    span_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        span_id = str(row.get("span_id", "")).strip()
        if span_id:
            span_ids.add(span_id)
    return sorted(span_ids)


def _graph_mapping_target_summary(graph_mapping_targets: dict[str, Any]) -> dict[str, Any]:
    summary = graph_mapping_targets.get("summary", {})
    if not isinstance(summary, dict):
        return {
            "approved_target_count": len(_collect_graph_mapping_target_ids(graph_mapping_targets)),
            "counts_by_phase": {},
            "counts_by_semantic_class": {},
        }
    return {
        "approved_target_count": int(summary.get("approved_target_count", 0) or 0),
        "counts_by_phase": dict(summary.get("counts_by_phase", {})) if isinstance(summary.get("counts_by_phase"), dict) else {},
        "counts_by_semantic_class": dict(summary.get("counts_by_semantic_class", {}))
        if isinstance(summary.get("counts_by_semantic_class"), dict)
        else {},
    }


def _collect_frozen_graph_operator_span_ids(graph_operator_spans: dict[str, Any]) -> list[str]:
    span_ids: set[str] = set()
    for row in graph_operator_spans.get("rows", []):
        if not isinstance(row, dict):
            continue
        graph_operator_span_id = str(row.get("graph_operator_span_id", "")).strip()
        if graph_operator_span_id:
            span_ids.add(graph_operator_span_id)
    return sorted(span_ids)


def build_payload(workspace_dir: Path, agent_name: str) -> tuple[Path, dict[str, Any]]:
    state = load_state(workspace_dir)
    config = effective_agent_config(agent_name, int(state["current_step"]))
    input_files = resolve_workspace_paths(workspace_dir, config["input_files"])
    output_files = resolve_workspace_paths(workspace_dir, config["output_files"])
    task_path = workspace_dir / "input" / TASK_FILENAME_BY_AGENT[agent_name]

    payload: dict[str, Any] = {
        "task_type": agent_name,
        "step": int(config["step"]),
        "workspace_dir": str(workspace_dir),
        "allowed_status": sorted(config["allowed_status"]),
        "required_input_files": [str(path) for path in input_files if path != task_path],
        "expected_output_files": [str(path) for path in output_files],
        "contract_schema_file": str(Path(state["skill_dir"]) / config["contract_schema_file"])
        if config.get("contract_schema_file")
        else "",
        "secondary_contract_schema_files": [
            str(Path(state["skill_dir"]) / item)
            for item in config.get("secondary_contract_schema_files", [])
            if str(item).strip()
        ],
        "contract_example_files": [
            str(Path(state["skill_dir"]) / item)
            for item in config.get("contract_example_files", [])
            if str(item).strip()
        ],
    }

    if agent_name == "profiling_preprocessor":
        current_step = int(config["step"])
        if current_step == 1:
            payload.update(
                {
                    "goal": "通过单一 wrapper 脚本执行完整 Step 1 切片流水线，生成全部 slice 工件和正式结果，并为 Step 2 的 X-only 主索引准备 trace slice。",
                    "input_resolution_path": str(workspace_dir / "input" / "input_resolution.json"),
                    "required_wrapper_script": "scripts/run_step1_preprocess_pipeline.py",
                    "allowed_official_scripts": ["scripts/run_step1_preprocess_pipeline.py"],
                    "wrapper_lock_path": str(workspace_dir / "logs" / "wrapper_runs" / "step1_wrapper.lock.json"),
                    "wrapper_status_path": str(workspace_dir / "audit" / "step1_in_progress.json"),
                    "must_not_do": [
                        "不得自行拆开逐条运行 Step 1 内部 6 个脚本。",
                        "不得在 wrapper 失败后拆脚本补跑、修改 state/audit 或伪造完成状态。",
                        "不得手工修改 state.json、audit 文件或正式输出 JSON。",
                        "不得把 Step 1 的 trace slice 收缩成只保留 X 事件；非 X 事件仍需保留在 trace_slice.json 中供兼容渲染与审计使用。",
                    ],
                    "allowed_diagnostics": [
                        "允许只读查看 audit/step1_in_progress.json，确认 script_index/total_scripts、current_child_script、stage_phase、heartbeat_count、output_line_count、idle_seconds。",
                        "允许只读查看 logs/wrapper_runs/*.combined.log 与 *.meta.json，确认当前 wrapper 是否仍在运行。",
                        "允许只读检查 artifacts/slices/*、artifacts/stacks/python_tracer_index.json 的文件是否持续增长或已生成。",
                        "允许基于 wrapper 心跳日志与 in_progress 状态判断长时间静默是正常运行、写盘阶段还是仍在等待子进程收口。",
                        "禁止把上述诊断动作升级为拆脚本补跑、修改 state/audit 或手工写正式结果。",
                    ],
                    "wrapper_log_contract": [
                        "Step 1 wrapper 会为每个子脚本写 logs/wrapper_runs/step1_<script>.combined.log 与对应 meta.json。",
                        "Step 1 wrapper 还会持续维护 audit/step1_in_progress.json，暴露 current_child_script、current_child_log_path、stage_phase、heartbeat_count、output_line_count、idle_seconds 等运行态字段。",
                        "当子脚本持续运行但暂时没有新输出时，wrapper 会打印 heartbeat，避免把长时间写盘/排序误判为卡死。",
                        "Windows + 大 trace 场景下会自动启用更细的 trace 扫描进度粒度。",
                        "Step 1 wrapper 会写 logs/wrapper_runs/step1_wrapper.lock.json；若检测到已有活跃实例，新的 wrapper 调用必须立即停止，不得重复启动。",
                    ],
                    "terminal_strategy": [
                        "只能用一条 blocking 命令直接运行 scripts/run_step1_preprocess_pipeline.py --workspace-dir <workspace>。",
                        "运行 wrapper 的同一 terminal 在 wrapper 返回前不得再发送任何命令，否则可能导致当前进程树被终止。",
                        "若需要观察进度，只能在另一个 terminal 只读查看 audit/step1_in_progress.json、logs/wrapper_runs/*.combined.log / *.meta.json / step1_wrapper.lock.json。",
                        "Step 1 中终端 exit code、命令返回、Task 本轮回复都不是可采信的完成信号；即使 CLI 看起来已经结束，也必须继续核对 wrapper lock。",
                    ],
                    "forbidden_launch_methods": [
                        "禁止 Start-Process",
                        "禁止 Start-Job",
                        "禁止 cmd /c start",
                        "禁止后台 detached 方式运行 wrapper",
                        "禁止生成 .ps1/.cmd 临时包装脚本再调用 wrapper",
                    ],
                    "retry_policy": [
                        "在未确认当前 wrapper 失败前，禁止再次启动同一个 Step 1 wrapper。",
                        "若发现 step1_wrapper.lock.json 显示已有活跃实例，只能等待或只读观察，禁止重跑。",
                        "若误触发第二次调用，wrapper 会自行拒绝重入；子 agent 不得继续尝试其他启动方式。",
                    ],
                    "completion_signal_priority": [
                        "对 Step 1 wrapper，终端 exit code 是明确不可靠的弱信号；logs/wrapper_runs/step1_wrapper.lock.json 的状态优先级绝对高于 exit code。",
                        "若终端显示命令结束或 exit code=0/1，但 step1_wrapper.lock.json 仍为 running，必须认定 wrapper 仍在运行或刚被外部中断；严禁据此判成功、判失败、结束 Task 或重跑。",
                        "只有当 step1_wrapper.lock.json 明确收口为 passed/failed，或 wrapper lock、step1_in_progress、child 日志与必需工件同时证明流程已结束时，才允许判定 Step 1 完成。",
                        "只要 step1_wrapper.lock.json 仍为 running，就不得仅因长时间无新 stdout、无新产物或 idle_seconds 偏大而判失败。",
                    ],
                    "required_post_checks": [
                        "trace_slice.json、kernel_details_slice.csv、operator_details_slice.csv、task_time_slice.csv、op_summary_slice.csv 必须存在。",
                        "state.artifacts 中对应的 *_slice_path 必须已回写。",
                        "python_tracer_index.json 必须存在且 state.artifacts.python_tracer_index_path 已回写。",
                        "state.flags.python_tracer_index_built 必须为 true。",
                        "preprocess_step1_result.json.status 必须在 allowed_status 内。",
                        "preprocess_step1_result.json 必须显式说明 Step 2 只把 X 事件作为正式 trace span 来源。",
                        "state.flags.slicing_done 必须为 true。",
                    ],
                    "acceptance_checks": [
                        "只能调用 scripts/run_step1_preprocess_pipeline.py --workspace-dir <workspace>。",
                        "若 wrapper 返回非 0，必须直接视为 Step 1 失败，不得继续人工补跑内部脚本。",
                    ],
                }
            )
        elif current_step == 2:
            payload.update(
                {
                    "goal": "通过单一 wrapper 脚本执行完整 Step 2 timeline index 流水线，生成只基于 X 事件构建 trace_spans 的 timeline_index.json 和正式结果，并避免逐脚本观测误判。",
                    "required_wrapper_script": "scripts/run_step2_preprocess_pipeline.py",
                    "allowed_official_scripts": ["scripts/run_step2_preprocess_pipeline.py"],
                    "wrapper_lock_path": str(workspace_dir / "logs" / "wrapper_runs" / "step2_wrapper.lock.json"),
                    "wrapper_status_path": str(workspace_dir / "audit" / "step2_in_progress.json"),
                    "must_not_do": [
                        "不得自行拆开逐条运行 build_timeline_index.py 与 write_preprocess_step2_outputs.py。",
                        "不得在 wrapper 失败后拆脚本补跑、修改 state/audit 或伪造完成状态。",
                        "不得修改 Step 1 slice 工件。",
                        "不得把 C、s、f、i、I、M 等非 X 事件混入 trace_spans 主索引。",
                    ],
                    "allowed_diagnostics": [
                        "允许只读查看 audit/step2_in_progress.json，确认 script_index/total_scripts、current_child_script、stage_phase、heartbeat_count、output_line_count、idle_seconds。",
                        "允许只读查看 logs/wrapper_runs/step2_*.combined.log 与 *.meta.json，确认当前 wrapper 是否仍在运行。",
                        "允许只读检查 artifacts/index/timeline_index.json 是否已生成。",
                        "禁止把诊断动作升级为拆脚本补跑或手工改写 timeline_index/state。",
                    ],
                    "wrapper_log_contract": [
                        "Step 2 wrapper 会为每个子脚本生成 logs/wrapper_runs/step2_<script>.combined.log 与对应 *.meta.json。",
                        "Step 2 wrapper 还会持续维护 audit/step2_in_progress.json，暴露 current_child_script、current_child_log_path、stage_phase、heartbeat_count、output_line_count、idle_seconds 等运行态字段。",
                        "Step 2 wrapper 入口会维护 logs/wrapper_runs/step2_wrapper.lock.json；若检测到已有活跃实例，新的调用会立即失败并提示禁止重跑。",
                        "若终端显示命令结束，但 step2_wrapper.lock.json 仍为 running，应优先以 wrapper lock 与 child 日志判定真实状态，不得据此重跑。",
                    ],
                    "terminal_strategy": [
                        "只能用单条 blocking 命令直接运行 required_wrapper_script。",
                        "运行 wrapper 的 terminal 在 wrapper 完成前不得再发任何命令。",
                        "若需要观察进度，只能在另一个 terminal 只读查看 audit/step2_in_progress.json、logs/wrapper_runs/step2_*.combined.log 或 *.meta.json。",
                        "Step 2 中终端 exit code、命令返回、Task 本轮回复都不是可采信的完成信号；即使 CLI 看起来已经结束，也必须继续核对 wrapper lock。",
                    ],
                    "forbidden_launch_methods": [
                        "禁止 Start-Process",
                        "禁止 Start-Job",
                        "禁止 cmd /c start",
                        "禁止后台 detached 运行",
                        "禁止生成 .ps1/.cmd 临时包装脚本",
                    ],
                    "retry_policy": [
                        "若 step2_wrapper.lock.json 显示已有活跃实例，禁止重跑；只能等待或只读查看日志。",
                        "若 wrapper 返回非 0，必须直接视为 Step 2 失败，不得拆脚本补跑。",
                    ],
                    "completion_signal_priority": [
                        "对 Step 2 wrapper，终端 exit code 是明确不可靠的弱信号；logs/wrapper_runs/step2_wrapper.lock.json 的状态优先级绝对高于 exit code。",
                        "若终端显示命令结束或 exit code=0/1，但 step2_wrapper.lock.json 仍为 running，必须认定 wrapper 仍在运行或刚被外部中断；严禁据此判成功、判失败、结束 Task 或重跑。",
                        "只有当 step2_wrapper.lock.json 明确收口为 passed/failed，或 wrapper lock、step2_in_progress、child 日志与 timeline_index 正式工件同时证明流程已结束时，才允许判定 Step 2 完成。",
                        "只要 step2_wrapper.lock.json 仍为 running，就不得仅因长时间无新 stdout、无新产物或 idle_seconds 偏大而判失败。",
                    ],
                    "required_post_checks": [
                        "timeline_index.json 必须存在且 state.artifacts.timeline_index_path 已回写。",
                        "timeline_index.json 必须显式声明 trace_span_build_policy=x_only 且 trace_span_source_ph=X。",
                        "timeline_index.json 必须显式声明 trace_time_unit_policy 与 task_identity_policy。",
                        "state.flags.timeline_index_built 必须为 true。",
                        "preprocess_step2_result.json.status 必须在 allowed_status 内。",
                    ],
                    "acceptance_checks": [
                        "只能调用 scripts/run_step2_preprocess_pipeline.py --workspace-dir <workspace>。",
                        "若 wrapper 返回非 0，必须直接视为 Step 2 失败，不得继续人工补跑内部脚本。",
                    ],
                }
            )
        else:
            raise ValueError(f"profiling_preprocessor 不支持的 step: {current_step}")
    elif agent_name == "timeline_analyst":
        payload.update(
            {
                "goal": "先通过官方 Step3 wrapper 生成 base classified/scope gate，再对允许的语义字段做受控 patch 审阅，最后输出 patch 与分析结果；Step3 不负责 graph candidate 或 phase 判定。",
                "required_wrapper_script": "scripts/run_step3_analysis_pipeline.py",
                "allowed_official_scripts": ["scripts/run_step3_analysis_pipeline.py"],
                "wrapper_lock_path": str(workspace_dir / "logs" / "wrapper_runs" / "step3_wrapper.lock.json"),
                "wrapper_status_path": str(workspace_dir / "audit" / "step3_in_progress.json"),
                "base_classified_output_path": str(workspace_dir / "artifacts" / "classification" / "classified_spans.base.json"),
                "base_scope_gate_output_path": str(workspace_dir / "output" / "scope_gate_result.base.json"),
                "review_patch_output_path": str(workspace_dir / "output" / "timeline_review_patch.json"),
                "timeline_analysis_output_path": str(workspace_dir / "output" / "timeline_analysis.json"),
                "timeline_analysis_report_path": str(workspace_dir / "output" / "timeline_analysis.md"),
                "analysis_contract_schema_file": str(Path(state["skill_dir"]) / "references" / "contracts" / "timeline_analysis_result.schema.json"),
                "canonical_target_paths": {
                    "classified_spans_path": str(workspace_dir / "artifacts" / "classification" / "classified_spans.json"),
                    "scope_gate_result_path": str(workspace_dir / "output" / "scope_gate_result.json"),
                },
                "allowed_mutation_fields": [
                    "stream_role",
                    "semantic_class",
                    "exclude_from_code_mapping",
                    "exclude_reason",
                    "semantic_confidence",
                    "parallel_group",
                ],
                "forbidden_mutation_fields": [
                    "span_id",
                    "stream_id",
                    "start_ns",
                    "end_ns",
                    "dur_ns",
                    "task_ids",
                    "task_compound_ids",
                    "op_row_ids",
                    "related_task_types",
                    "related_op_names",
                    "trace_event_ref",
                    "has_stream_id",
                    "scope_class",
                    "matched_scope_rule_id",
                    "matched_scope_rule_source",
                    "external_mapping_required",
                    "span_count",
                    "semantic_span_count",
                    "excluded_span_count",
                    "scope_summary",
                ],
                "derived_fields_recomputed_by_merge": [
                    "external_mapping_required",
                    "stream_role",
                    "parallel_group",
                    "semantic_span_count",
                    "excluded_span_count",
                    "scope_summary",
                ],
                "must_exclude_patterns": [
                    "CAPTURE_*",
                    "NOTIFY_*",
                    "EVENT_*",
                    "AscendCL@*",
                    "Runtime@Event*",
                    "Enqueue@record",
                    "Dequeue@record",
                ],
                "must_include_patterns": [
                    "fill_new_verified_id",
                    "assign_req_to_token_pool",
                    "assign_draft_cache_locs*",
                    "cache_loc_assign",
                    "cache_loc_update",
                    "build_tree_efficient",
                    "compute_position_kernel",
                ],
                "must_not_do": [
                    "不得伪造不存在的 stream/span/phase",
                    "不得越权做代码定位或最终门禁判断",
                    "不得直接改写 canonical classified_spans.json 或 canonical scope_gate_result.json",
                    "不得直接修改 state.json",
                    "不得把 NOTIFY_RECORD_SQE、NOTIFY_WAIT_SQE、纯 CAPTURE_/EVENT_/AscendCL@/Runtime@Event 控制类 span 重新放回 semantic 集合。",
                ],
                "allowed_diagnostics": [
                    "允许只读查看 audit/step3_in_progress.json，确认 script_index/total_scripts、current_child_script、stage_phase、heartbeat_count、output_line_count、idle_seconds。",
                    "允许只读查看 logs/wrapper_runs/step3_*.combined.log 与 *.meta.json，确认当前 wrapper 是否仍在运行。",
                    "允许只读检查 artifacts/classification/classified_spans.base.json 与 output/scope_gate_result.base.json 是否已生成。",
                    "允许基于 wrapper lock 与 child 日志判断当前长时间静默是正常运行、写盘阶段还是疑似卡住。",
                    "禁止把上述诊断动作升级为拆脚本补跑、修改 state/audit 或手工写正式结果。",
                ],
                "wrapper_log_contract": [
                    "Step 3 wrapper 会为每个子脚本生成 logs/wrapper_runs/step3_<script>.combined.log 与对应 *.meta.json。",
                    "Step 3 wrapper 还会持续维护 audit/step3_in_progress.json，暴露 current_child_script、current_child_log_path、stage_phase、heartbeat_count、output_line_count、idle_seconds 等运行态字段。",
                    "Step 3 wrapper 入口会维护 logs/wrapper_runs/step3_wrapper.lock.json；若检测到已有活跃实例，新的调用会立即失败并提示禁止重跑。",
                    "若终端显示命令结束，但 step3_wrapper.lock.json 仍为 running，应优先以 wrapper lock 与 child 日志判定真实状态，不得据此重跑。",
                ],
                "terminal_strategy": [
                    "只能用单条 blocking 命令直接运行 scripts/run_step3_analysis_pipeline.py --workspace-dir <workspace>。",
                    "运行 wrapper 的同一 terminal 在 wrapper 返回前不得再发送任何命令，否则可能导致当前进程树被终止。",
                    "若需要观察进度，只能在另一个 terminal 只读查看 audit/step3_in_progress.json、logs/wrapper_runs/step3_*.combined.log / *.meta.json / step3_wrapper.lock.json。",
                    "Step 3 中终端 exit code、命令返回、Task 本轮回复都不是可采信的完成信号；即使 CLI 看起来已经结束，也必须继续核对 wrapper lock。",
                ],
                "forbidden_launch_methods": [
                    "禁止 Start-Process",
                    "禁止 Start-Job",
                    "禁止 cmd /c start",
                    "禁止后台 detached 方式运行 wrapper",
                    "禁止生成 .ps1/.cmd 临时包装脚本再调用 wrapper",
                ],
                "retry_policy": [
                    "在未确认当前 wrapper 失败前，禁止再次启动同一个 Step 3 wrapper。",
                    "若发现 step3_wrapper.lock.json 显示已有活跃实例，只能等待或只读观察，禁止重跑。",
                    "若误触发第二次调用，wrapper 会自行拒绝重入；子 agent 不得继续尝试其他启动方式。",
                ],
                "completion_signal_priority": [
                    "对 Step 3 wrapper，终端 exit code 是明确不可靠的弱信号；logs/wrapper_runs/step3_wrapper.lock.json 的状态优先级绝对高于 exit code。",
                    "若终端显示命令结束或 exit code=0/1，但 step3_wrapper.lock.json 仍为 running，必须认定 wrapper 仍在运行或刚被外部中断；严禁据此判成功、判失败、结束 Task 或重跑。",
                    "只有当 step3_wrapper.lock.json 明确收口为 passed/failed，或 wrapper lock、step3_in_progress、child 日志与 base 工件同时证明流程已结束时，才允许判定 Step 3 wrapper 完成。",
                    "只要 step3_wrapper.lock.json 仍为 running，就不得仅因长时间无新 stdout、无新 base 工件或 idle_seconds 偏大而判失败。",
                ],
                "acceptance_checks": [
                    "必须先运行 scripts/run_step3_analysis_pipeline.py 生成 base 工件，再输出 timeline_review_patch.json 与 timeline_analysis.json/.md。",
                    "若 wrapper 返回非 0，必须直接视为 Step 3 失败，不得继续人工拆跑 classify_spans.py / check_scope_gate.py。",
                    "timeline_review_patch.json.status 必须在 allowed_status 内。",
                    "timeline_analysis.json.status 必须在 allowed_status 内。",
                    "timeline_review_patch.json 只能修改 allowed_mutation_fields 中列出的字段。",
                    "timeline_analysis.json 至少包含 source、base_artifacts、review_patch_summary、mutation_summary。",
                    "timeline_analysis.json 必须满足 analysis_contract_schema_file 指向的结构约束。",
                    "Step3 不得输出 graph candidate 或 phase 级正式判断。",
                    "若 base scope gate 暴露出 runtime_control span 污染语义集合，必须在 timeline_analysis.json.notes 中显式解释。最终 reviewed canonical 结果仍必须让 scope gate 通过。",
                    "允许输出空 patch，但 patch 文件必须存在且结构合法。",
                ],
                "required_post_checks": [
                    "artifacts/classification/classified_spans.base.json 必须存在。",
                    "output/scope_gate_result.base.json 必须存在。",
                    "step3_wrapper.lock.json 必须已收口为 passed；仅 base scope gate.status=failed 不代表 wrapper 失败。",
                    "timeline_review_patch.json、timeline_analysis.json、timeline_analysis.md 必须全部存在。",
                ],
            }
        )
    elif agent_name == "step4_bootstrap_runner":
        bootstrap_target = "step4_stack_mapper"
        payload.update(
            {
                "goal": "执行 Step4A bootstrap freeze，只运行官方 runner，等待 wrapper 收口并产出 step4_bootstrap_result.json；不得替代 Step4B graph 外定位。",
                "substep": "A",
                "bootstrap_target": bootstrap_target,
                "required_wrapper_script": "scripts/run_step4_bootstrap_runner.py",
                "allowed_official_scripts": ["scripts/run_step4_bootstrap_runner.py"],
                "expected_script_sequence": STEP4_TARGET_SCRIPT_SEQUENCE[bootstrap_target],
                "required_ready_artifacts": STEP4_REQUIRED_ARTIFACTS_BY_TARGET[bootstrap_target],
                "required_ready_flags": STEP4_REQUIRED_FLAGS_BY_TARGET[bootstrap_target],
                "wrapper_lock_path": str(step4_bootstrap_lock_path(workspace_dir)),
                "wrapper_status_path": str(step4_bootstrap_status_path(workspace_dir)),
                "allowed_diagnostics": [
                    "允许只读查看 logs/wrapper_runs/step4_bootstrap.lock.json，确认 wrapper 当前正式状态。",
                    "允许只读查看 audit/step4_bootstrap_in_progress.json，确认当前 target、script_index/total_scripts、current_child_script 与 stage_phase。",
                    "允许只读查看对应 child combined log 与 child meta.json，确认 heartbeat 和阶段推进。",
                    "允许只读查看 ready set 工件是否正在逐步补齐。",
                ],
                "terminal_strategy": [
                    "只能运行 scripts/run_step4_bootstrap_runner.py --workspace-dir <workspace> --bootstrap-target step4_stack_mapper。",
                    "运行 wrapper 的同一 terminal 在 wrapper 明确收口前不得再发送任何新命令，包括 sleep、timeout、轮询、重试、再次启动 wrapper 或其他 python/PowerShell 命令。",
                    "若必须观察进度，只能在另一个 terminal 只读查看 step4_bootstrap.lock.json、bootstrap_in_progress、child combined log、child meta.json；不能占用正在运行 wrapper 的 terminal。",
                    "顶层 terminal exit code、命令返回、Task 本轮回复都不是 Step4A 的可信完成信号。",
                    "Step4A 是否真正结束，必须优先看 step4_bootstrap.lock.json 的最终状态，再结合 bootstrap_in_progress、child log、child meta 判断。",
                ],
                "retry_policy": [
                    "若 step4_bootstrap.lock.json.status=running，只能继续等待或只读观察，禁止重跑。",
                    "禁止用同一 terminal 发送 sleep/timeout 后再重试；这种做法会污染或打断当前 wrapper 进程树，不能作为合法等待策略。",
                    "即使顶层命令已经返回，只要 lock 仍为 running，也必须认定 shared bootstrap 尚未结束。",
                    "只有当 lock 明确收口为 passed/failed 后，才允许结束等待并作正式结论。",
                ],
                "completion_signal_priority": [
                    "第一正式状态源是 logs/wrapper_runs/step4_bootstrap.lock.json。",
                    "辅助观察源是 audit/step4_bootstrap_in_progress.json、child combined log、child meta.json。",
                    "lock.status=running 时，不能因为 lock 长时间未改写就直接判卡死；要结合 child heartbeat 与阶段信息判断。",
                    "只有当 lock.status=passed 且 ready set 完整时，才允许生成 step4_bootstrap_result.json.status=passed。",
                    "若 lock.status=failed，或 lock.status=passed 但 ready set 不完整，必须直接报错并停止，不得伪造 passed 结果。",
                ],
                "completion_signal_contract": [
                    "Step4A 的真正完成信号不是顶层 terminal exit code，而是 step4_bootstrap.lock.json.status=passed 且 Step4 bootstrap ready set 全部满足。",
                    "step4_bootstrap_result.json 必须显式回写 wrapper_lock_path、wrapper_lock_status、wrapper_status_path、ready_summary。",
                    "若 wrapper 失败或 ready set 不完整，必须直接报错并停止，不得伪造 passed 结果。",
                ],
                "must_not_do": [
                    "不得手工拆跑 check_repo_divergence.py、build_runtime_constraints.py、build_stack_evidence.py、build_graph_phase_stack_evidence.py、classify_graph_groups.py、build_graph_mapping_targets.py、build_external_mapping_targets.py、build_stack_call_paths.py。",
                    "不得修改 shared bootstrap 已生成的 artifacts/state 来伪造 ready。",
                    "不得替代 stack_mapper 生成 graph 外正式定位结果。",
                ],
                "acceptance_checks": [
                    "只能调用 scripts/run_step4_bootstrap_runner.py --workspace-dir <workspace> --bootstrap-target step4_stack_mapper。",
                    "output/step4_bootstrap_result.json.status 必须为 passed。",
                    "step4_bootstrap_result.json.ready_summary.ready 必须为 true。",
                    "step4_bootstrap_result.json.expected_script_sequence 必须与 task input 中的 expected_script_sequence 一致。",
                ],
            }
        )
    elif agent_name == "graph_bootstrap_runner":
        bootstrap_target = "step5_graph_path_analyst"
        payload.update(
            {
                "goal": "执行 Step5A graph bootstrap，只运行官方 runner，补齐 graph_forward_context / graph_seed_context / graph_operator_spans 并产出 graph_bootstrap_result.json；不得替代 Step5B 的 graph review。",
                "substep": "A",
                "bootstrap_target": bootstrap_target,
                "required_wrapper_script": "scripts/run_graph_bootstrap_runner.py",
                "allowed_official_scripts": ["scripts/run_graph_bootstrap_runner.py"],
                "expected_script_sequence": STEP5_TARGET_SCRIPT_SEQUENCE[bootstrap_target],
                "required_ready_artifacts": STEP5_REQUIRED_ARTIFACTS_BY_TARGET[bootstrap_target],
                "required_ready_flags": STEP5_REQUIRED_FLAGS_BY_TARGET[bootstrap_target],
                "wrapper_lock_path": str(step5_graph_bootstrap_lock_path(workspace_dir)),
                "wrapper_status_path": str(step5_graph_bootstrap_status_path(workspace_dir)),
                "allowed_diagnostics": [
                    "允许只读查看 logs/wrapper_runs/step5_graph_bootstrap.lock.json，确认 wrapper 当前正式状态。",
                    "允许只读查看 audit/step5_graph_bootstrap_in_progress.json，确认 script_index/total_scripts、current_child_script 与 stage_phase。",
                    "允许只读查看对应 child combined log 与 child meta.json，确认 heartbeat 和阶段推进。",
                ],
                "terminal_strategy": [
                    "只能运行 scripts/run_graph_bootstrap_runner.py --workspace-dir <workspace>。",
                    "运行 wrapper 的同一 terminal 在 wrapper 明确收口前不得再发送任何新命令，包括 sleep、timeout、轮询、重试、再次启动 wrapper 或其他 python/PowerShell 命令。",
                    "若必须观察进度，只能在另一个 terminal 只读查看 step5_graph_bootstrap.lock.json、bootstrap_in_progress、child combined log、child meta.json；不能占用正在运行 wrapper 的 terminal。",
                    "顶层 terminal exit code、命令返回、Task 本轮回复都不是 Step5A 的可信完成信号。",
                ],
                "retry_policy": [
                    "若 step5_graph_bootstrap.lock.json.status=running，只能继续等待或只读观察，禁止重跑。",
                    "即使顶层命令已经返回，只要 lock 仍为 running，也必须认定 graph bootstrap 尚未结束。",
                    "只有当 lock 明确收口为 passed/failed 后，才允许结束等待并作正式结论。",
                ],
                "must_not_do": [
                    "不得手工拆跑 build_graph_forward_context.py、build_graph_seed_context.py、build_graph_operator_spans.py。",
                    "不得修改 Step5A 已生成的 artifacts/state 来伪造 ready。",
                    "不得替代 graph_path_analyst 生成 graph review / alignment 结果。",
                ],
                "acceptance_checks": [
                    "只能调用 scripts/run_graph_bootstrap_runner.py --workspace-dir <workspace>。",
                    "output/graph_bootstrap_result.json.status 必须为 passed。",
                    "graph_bootstrap_result.json.ready_summary.ready 必须为 true。",
                    "graph_bootstrap_result.json.expected_script_sequence 必须与 task input 中的 expected_script_sequence 一致。",
                ],
            }
        )
    elif agent_name == "stack_mapper":
        python_tracer_path = workspace_dir / "artifacts" / "stacks" / "python_tracer_index.json"
        stack_call_paths_path = workspace_dir / "artifacts" / "mapping" / "stack_call_paths.json"
        flags = state.get("flags", {})
        python_tracer_summary = {
            "available": bool(flags.get("python_tracer_index_built")) and python_tracer_path.exists(),
            "status": "",
            "total_frame_count": 0,
            "repo_frame_count": 0,
        }
        if python_tracer_path.exists():
            python_tracer_payload = load_json(python_tracer_path)
            python_tracer_summary["status"] = str(python_tracer_payload.get("status", "")).strip()
            stats = python_tracer_payload.get("stats", {})
            python_tracer_summary["total_frame_count"] = int(stats.get("total_frame_count", 0) or 0)
            python_tracer_summary["repo_frame_count"] = int(stats.get("repo_frame_count", 0) or 0)
        payload.update(
            {
                "goal": "作为 Step4B，在 Step4A 已冻结的 shared bootstrap 结果基础上，基于 stack / tracer 证据，在 external_mapping_targets.json 已冻结的封闭 formal target set 内完成 graph 外逐 span 正式代码定位；Step4B 不再负责等待 wrapper，也不得回写 shared graph scope 工件。",
                "substep": "B",
                "required_evidence_sources": [
                    "operator_details.csv",
                    "stack_evidence.json",
                    "external_mapping_targets.json",
                    "classified_spans.json",
                    "timeline_index.json",
                    "python_tracer_index.json",
                    "stack_call_paths.json",
                ],
                "required_reference_files": [
                    "references/shared/stack_mapping_rules.md",
                    "references/shared/stream_classification_rules.md",
                ],
                "appendix_read_contract": [
                    "在开始正式分析和写 JSON 之前，必须先读完 references/shared/stack_mapping_rules.md。",
                    "由于 Step4 当前需要依赖 Step3 的 stream/scope 结果来理解 external target 边界、过滤 graph replay runtime wrapper 并解释 why-not-mapped 行为，也必须先读完 references/shared/stream_classification_rules.md。",
                    "不得再把上述 shared 文档理解成“仅当主输入证据不足时才按需补读”的可选参考。",
                ],
                "step4_bootstrap_result_path": str(workspace_dir / "output" / "step4_bootstrap_result.json"),
                "python_tracer_index_path": str(python_tracer_path) if python_tracer_path.exists() else "",
                "stack_call_paths_path": str(stack_call_paths_path) if stack_call_paths_path.exists() else "",
                "external_mapping_targets_path": str(workspace_dir / "artifacts" / "mapping" / "external_mapping_targets.json"),
                "precomputed_evidence_summary": {
                    "python_tracer_available": bool(flags.get("python_tracer_index_built")) and python_tracer_path.exists(),
                    "stack_call_paths_available": bool(flags.get("stack_call_paths_built")) and stack_call_paths_path.exists(),
                    "stack_call_paths_state_flag": bool(flags.get("stack_call_paths_built")),
                    "stack_call_paths_file_present": stack_call_paths_path.exists(),
                    "python_tracer_state_flag": bool(flags.get("python_tracer_index_built")),
                    "python_tracer_file_present": python_tracer_path.exists(),
                    "python_tracer_status": python_tracer_summary["status"],
                    "python_tracer_total_frame_count": python_tracer_summary["total_frame_count"],
                    "python_tracer_repo_frame_count": python_tracer_summary["repo_frame_count"],
                },
                "must_not_do": [
                    "不得跳过 references/shared/stack_mapping_rules.md 或 references/shared/stream_classification_rules.md 就直接输出 Step4 正式结果。",
                    "不得把 graph replay 内 runtime 包装层冒充为 graph 外精确定位",
                    "不得越权修改 graph 对齐工件或 shared graph scope 工件",
                    "不得仅因命中 torch/torch_npu 外部 wrapper 就将其当成最终 code_location。",
                    "不得只输出候选说明而不提交 external_span_mapping_payload。",
                    "不得省略 quality_signals，或通过手工去重/美化掩盖主定位异常集中到 scheduler、worker 等顶层入口的现象。",
                    "若 precomputed_evidence_summary 指示 stack_call_paths 或 python_tracer 可用，不得在结果里声称对应文件缺失或不可用。",
                    "不得重新触发 Step4A shared bootstrap，也不得把 Step4B 的 prepare 理解成仍会代跑 wrapper 的重命令。",
                    "不得跳过调用栈文件:函数定位，直接从模糊语义猜测最终 code_location。",
                    "不得从 classified_spans.json、timeline_index.json 或其他上下文文件里新增正式 graph 外 mapping target；正式 target 只允许来自 external_mapping_targets.json。",
                    "不得仅因 file_function_candidates 中某个协调层函数 score 更高，就直接把 scheduler、worker、schedule_batch、prefill_delayer、speculative 等高层编排函数当成最终 primary_file_function。",
                    "对 semantic_class=communication 的 span，若 evidence 已明确缺少实现层 repo frame，不得把调度层函数包装成高质量精确 code line。",
                    "若预计算 evidence 已给出 recommended_primary_location_kind=function_entry_fallback 且没有反证，不得仍输出 semantic_line_selection / python_tracer_line_candidate / call_stack_line_candidate 这类精确定位。",
                ],
                "allowed_official_scripts": [],
                "acceptance_checks": [
                    "stack_mapping_result.json.status 必须在 allowed_status 内",
                    "stack_mapping_result.json 至少包含 coverage、external_span_mapping_payload。",
                    "若 python_tracer_index.json 与 stack_call_paths.json 存在，必须显式利用其中的 file_function_candidates / code_line_candidates，而不是退回旧单点调用栈入口。",
                    "graph 外每个不排除 span 都应尽量给出正式 code_location；若证据不足，必须在 payload 中记录 unresolved_reason。",
                    "evidence_inputs 中关于 stack_call_paths / python_tracer 的存在性、状态与 frame 计数，必须与 precomputed_evidence_summary 和真实工件一致。",
                    "stack_mapping_result.json 应包含 quality_signals，并如实暴露 top_repeated_primary_code_location / top_repeated_primary_file_function 这类最小质量信号，供审计 Step4 是否异常塌缩到顶层入口。",
                    "external_span_mapping_payload 必须使用 {'status': ..., 'row_count': N, 'rows': [...]} 包装，而不是裸列表或自由字典。",
                    "external_span_mapping_payload.rows[*].primary_file_function 必须是对象，至少包含 repo_relative_path、symbol、entry_line；禁止写成 'path.py:function' 字符串。",
                    "若存在 file_function_candidates，则 primary_file_function 必须能回溯到其中某个候选，不能与候选集合自相矛盾。",
                    "external_span_mapping_payload.rows[*] 应优先给出 primary_file_function、file_function_candidates、code_line_candidates，并说明最终 code_location 是如何结合 span 语义与左右 span 从候选中选出的。",
                    "external_span_mapping_payload.rows[*].span_id 必须全部属于 external_mapping_targets.json.rows[*].span_id；上下文文件只能用于补证据和选线，不能扩正式映射范围。",
                    "JSON 与报告中的 stream/scope 边界解释、graph replay wrapper 过滤口径必须与 references/shared/stream_classification_rules.md 一致；文件:函数与选线口径必须与 references/shared/stack_mapping_rules.md 一致。",
                    "Step4B 只能消费 Step4A 已批准的 step4_bootstrap_result.json 与 ready artifacts；不得要求主 agent 或你再次观察/等待 Step4A wrapper。",
                    "若 primary_code_location_kind=function_entry_fallback，必须显式说明为什么未能把 span 收敛到更具体的代码行。",
                    "若 external_span_mapping_payload.rows[*].semantic_class=communication 且 evidence 明确缺少实现层 repo frame，则 primary_code_location_kind 应优先为 function_entry_fallback 或 unresolved；若仍选择精确 code line，必须在 mapping_basis / selection_reason 中给出反证。",
                    "若最终选择的 primary_file_function 位于 scheduler、worker、schedule_batch、prefill_delayer、speculative 等协调层路径，而候选集中存在更贴近实现层的函数，必须显式说明为什么未采用实现层候选。",
                    "quality_signals 若暴露 top_repeated_primary_file_function 异常集中到协调层入口，报告必须解释这是共享实现层还是语义塌缩，不能直接合理化成预期模式。",
                    "Step4 只允许消费 shared stage 已冻结的 external_mapping_targets.json；若需要 graph/phase 信息，只能把它当上下文，不得在 Step4 正式 JSON 中重新定义 graph scope。",
                ],
            }
        )
    elif agent_name == "graph_path_analyst":
        skill_dir = Path(state["skill_dir"])
        runtime_constraints = load_json(workspace_dir / "input" / "runtime_constraints.json") if (workspace_dir / "input" / "runtime_constraints.json").exists() else {}
        graph_forward_context = load_json(workspace_dir / "artifacts" / "graph" / "graph_forward_context.json") if (workspace_dir / "artifacts" / "graph" / "graph_forward_context.json").exists() else {}
        graph_execution_plan = load_json(workspace_dir / "artifacts" / "graph" / "graph_execution_plan.json") if (workspace_dir / "artifacts" / "graph" / "graph_execution_plan.json").exists() else {}
        graph_mapping_targets = load_json(workspace_dir / "artifacts" / "graph" / "graph_mapping_targets.json") if (workspace_dir / "artifacts" / "graph" / "graph_mapping_targets.json").exists() else {}
        graph_operator_spans = load_json(workspace_dir / "artifacts" / "graph" / "graph_operator_spans.json") if (workspace_dir / "artifacts" / "graph" / "graph_operator_spans.json").exists() else {}
        repo_divergence_report = load_json(workspace_dir / "artifacts" / "repo" / "repo_divergence_report.json") if (workspace_dir / "artifacts" / "repo" / "repo_divergence_report.json").exists() else {}
        inventory_graph_span_ids = _collect_identified_graph_span_ids(graph_execution_plan)
        phase_window_inventory_span_ids = _collect_phase_window_span_ids(graph_execution_plan)
        frozen_graph_span_ids = _collect_graph_mapping_target_ids(graph_mapping_targets)
        graph_mapping_target_summary = _graph_mapping_target_summary(graph_mapping_targets)
        frozen_graph_operator_span_ids = _collect_frozen_graph_operator_span_ids(graph_operator_spans)
        knowledge_docs = [
            skill_dir / "references" / "knowledge" / "model_config_and_launch_fields.md",
            skill_dir / "references" / "knowledge" / "sglang_path_map.md",
            skill_dir / "references" / "knowledge" / "forward_analysis_rules.md",
        ]
        payload.update(
            {
                "goal": "基于 Step5A 已冻结的 graph bootstrap 结果与 graph inventory / phase windows / graph_mapping_targets / graph_operator_spans，完成 graph 内真实路径重建，并生成逐 span alignment。",
                "substep": "B",
                "graph_bootstrap_result_path": str(workspace_dir / "output" / "graph_bootstrap_result.json"),
                "repo_divergence_report_path": str(workspace_dir / "artifacts" / "repo" / "repo_divergence_report.json"),
                "runtime_constraints_path": str(workspace_dir / "input" / "runtime_constraints.json"),
                "graph_seed_context_path": str(workspace_dir / "input" / "graph_seed_context.json"),
                "graph_phase_stack_evidence_path": str(workspace_dir / "artifacts" / "graph" / "graph_phase_stack_evidence.json"),
                "graph_mapping_targets_path": str(workspace_dir / "artifacts" / "graph" / "graph_mapping_targets.json"),
                "graph_operator_spans_path": str(workspace_dir / "artifacts" / "graph" / "graph_operator_spans.json"),
                "path_reconstruction_readiness": graph_forward_context.get(
                    "path_reconstruction_readiness",
                    runtime_constraints.get("step5_preconditions", {}),
                ),
                "repo_file_existence_facts": graph_forward_context.get(
                    "repo_file_existence_facts",
                    {
                        "fact_source": {
                            "repo_divergence_report_path": str(workspace_dir / "artifacts" / "repo" / "repo_divergence_report.json"),
                            "repo_exists_scan": True,
                        },
                        "existing_files": repo_divergence_report.get("existing_files", []),
                        "missing_files": repo_divergence_report.get("missing_files", []),
                    },
                ),
                "analysis_requirements": {
                    "graph_sequence_analysis_required": True,
                    "sequence_analysis_steps": [
                        "按 phase 和时间顺序构建 ordered operator span sequence",
                        "识别 repetitive pattern segments",
                        "识别 distinctive kernel anchors",
                        "把 sequence anchor 和 pattern 外推回理论 forward 路径与邻近 operator spans",
                    ],
                    "sequence_evidence_checks": [
                        "是否构建了 ordered operator span sequence",
                        "是否识别了 repetitive segments",
                        "是否识别了 distinctive anchors",
                        "是否把 anchor/pattern 结果映射回 operator span groups",
                    ],
                },
                "graph_skeleton_scope": {
                    "semantic_skeleton": {
                        "definition": "graph_mapping_targets.json.rows[*].span_id",
                        "frozen_graph_span_ids": frozen_graph_span_ids,
                        "formal_graph_target_count": graph_mapping_target_summary.get("approved_target_count", 0),
                        "counts_by_phase": graph_mapping_target_summary.get("counts_by_phase", {}),
                        "counts_by_semantic_class": graph_mapping_target_summary.get("counts_by_semantic_class", {}),
                        "inventory_graph_span_ids": inventory_graph_span_ids,
                        "phase_window_inventory_span_ids": phase_window_inventory_span_ids,
                    },
                    "operator_skeleton": {
                        "definition": "graph_operator_spans.json.rows[*].graph_operator_span_id",
                        "frozen_graph_operator_span_ids": frozen_graph_operator_span_ids,
                    },
                },
                "candidate_search_roots": graph_forward_context.get("candidate_search_roots", []),
                "knowledge_reference_files": [str(path) for path in knowledge_docs],
                "must_not_do": [
                    "若 path_reconstruction_readiness.status 不是 ready，不得继续尝试 graph 内真实路径下钻",
                    "在真正开始 graph 内路径下钻前，不得跳过 references/knowledge/model_config_and_launch_fields.md、references/knowledge/sglang_path_map.md、references/knowledge/forward_analysis_rules.md 这 3 份知识文档。",
                    "不得把 replay 或 phase 级位置冒充为逐 span forward 真实代码行",
                    "不得只复用旧版 qwen3_moe 固定模板冒充真实路径重建结果",
                    "不得越权改写已有 graph 输入工件；正式结果必须通过 output/graph_review_result.json 的 artifact_promotion 提交。",
                    "不得再等待、观察或重跑 Step5A wrapper；graph_path_analyst 只消费已冻结的 graph_bootstrap_result.json 与 ready artifacts。",
                    "不得把预置知识文档当成当前仓库事实源；若知识与当前仓库冲突，必须以当前仓库代码为准。",
                    "不得把 graph_forward_context / graph_seed_context 中的 support_file_hints、candidate_search_roots 或其他搜索提示直接当作 communication/cache/operator 最终落点。",
                    "不得把 MODEL_EXECUTE phase marker 直接当成最终 graph span 做 code alignment；正式对齐对象必须来自 graph_operator_spans.json。",
                    "不得从 classified_spans.json、timeline_index.json、graph_execution_plan.json 或其他上下文文件重新扩写 formal graph target；正式 graph target 只能来自 graph_mapping_targets.json，operator spans 只能来自 graph_operator_spans.json。",
                    "不得跳过 ordered operator span sequence 分析，就直接把每个 graph span 当成完全独立的点逐条猜测。",
                    "不得把 sequence pattern / distinctive kernel anchor 当成绕过 repo 下钻的替代物；sequence 证据只能增强 operator_call 确认。",
                    "若启动参数量化方式是 modelslim，不得跳过模型目录下的 quant_model_description.json；不得把其中的 FLOAT 机械解释为 FP32。",
                    "若任何关键定位仍是 line=0 或 code_location=path:0，不得输出 status=passed。",
                    "若输出 status=partial，必须同时给出非空 blocking_issues；partial 只允许保留分析性 graph 工件，不得把正式 graph mapping 混入主链。",
                    "若 status=partial 且提交了 graph_span_alignment_payload，不得只提交代表性 alignment rows 或只提交模板；模板、pattern、segment summary 不能代替对未完成 formal graph alignment 范围的分析性覆盖。",
                    "不得把第二轮证明字段误做成所有 row 全量重展开；重复模板的候选比较与排除说明应主要放在顶层 decision_templates，而不是在每条 row 重复堆砌。",
                    "不得在 review_outcome=blocked 的结论里自行猜测某个 repo 文件缺失；文件存在性结论只能引用 repo_file_existence_facts 中的正式事实，或直接以当前 repo 实际可读文件为准。",
                    "不得把模型文件中的子模块调用边界当成最终 code_location，例如 self.gate(...)、self.topk(...)、self.experts(...)、self.qkv_proj(...)、self.o_proj(...)、self.input_layernorm(...)、self.post_attention_layernorm(...)、self.logits_processor(...)。",
                    "不得把构造行、注册行或 graph runner 的 replay() 入口当成最终 code_location，例如 self.xxx = SomeModule(...)、registry 注册点、self.graphs[...].replay()。",
                    "不得把 self.xxx(...)、module(...)、layer(...) 这类 nn.Module 调用边界直接标成 location_kind=operator_call；它们只能是 module_call_anchor 并继续下钻。",
                    "若某条 graph span 仍只是中间候选，不得省略 location_kind / operator_evidence_kind / requires_further_drilldown 这些结构化字段。",
                    "若输出 status=passed，不得只给最终 code_location 而不提供模板级候选比较；必须通过 decision_templates 解释为什么是这条最终行、为什么不是旁边其他候选。",
                    "若输出 status=partial，不得只给少量 blocker 样本；必须提供 unresolved_template_summary，按模板交代未完成范围与影响 span 数量。",
                ],
                "allowed_official_scripts": [],
                "acceptance_checks": [
                    "output/graph_review_result.json.status 必须在 allowed_status 内",
                    "只有当 path_reconstruction_readiness.status=ready 时，才允许继续做 graph 内真实路径重建；否则必须明确是 `status=partial` 还是 `review_outcome=blocked` 所对应的原因与范围。",
                    "在真正开始 graph 内路径下钻前，必须先阅读 references/knowledge/model_config_and_launch_fields.md、references/knowledge/sglang_path_map.md、references/knowledge/forward_analysis_rules.md，并在 graph_review_result.json.knowledge_reference_check 中显式记录已阅读结果。",
                    "graph_review_result.json 必须包含 knowledge_reference_check 与 rules_conformance_check；其中 knowledge_reference_check 必须显式记录 3 份知识文档已阅读，且 repo/profiling 优先原则已确认。",
                    "graph_review_result.json 必须包含 repo_file_evidence_check；其中必须显式说明已对照 repo_divergence_report.json，existing_files_relied_on / missing_files_relied_on / contradictions 都必须是结构化列表。",
                    "repo_file_evidence_check.contradictions 只用于记录仍未消解的 repo 文件事实冲突；若只是上游任务 JSON、seed context 或 graph plan 描述不一致，应写入 blocking_issues / review_summary / notes，而不是 contradictions。",
                    "若 status=passed，则 review_outcome 必须为 approved，且必须同时提供 artifact_promotion.graph_execution_plan_updates、graph_forward_context_updates、graph_span_candidates_payload、forward_segment_template_payload、graph_span_alignment_payload。",
                    "若 status=partial，则 review_outcome 必须为 blocked，且必须提供非空 blocking_issues。",
                    "reviewed_mapping_granularity 必须反映真实精度，证据不足时不能伪装通过。",
                    "凡是识别出 graph spans 的正式 graph 场景，包括 spec_v2 与 decode_graph，都不能只停留在 replay 或 phase 级位置，必须基于 graph_operator_spans.json 达到逐 span forward 真实代码行。",
                    "若 status=passed，则正式 payload 中不得包含 line<=0 或 code_location 以 :0 结尾的占位定位。",
                    "若 status=passed，则 graph 内最终 code_location 不得停在模型文件中的模块调用边界、构造行或 graph replay 入口；这些位置只能作为中间候选并继续下钻。",
                    "若 status=passed，则 graph_span_alignment 与 artifact_promotion.graph_span_alignment_payload 中的正式 graph span 记录必须显式包含 location_kind、operator_evidence_kind、requires_further_drilldown。",
                    "若 status=passed，则 location_kind 必须为 operator_call，requires_further_drilldown 必须为 false；module_call_anchor、graph_replay_entry、constructor_line 只能作为中间候选，不能混入最终通过结果。",
                    "若 status=passed，则每条正式 row 至少必须带 template_key 与 selected_source_line_text；decision_templates 必须存在，且每个已使用模板都要给出 candidate_code_locations、selected_code_location、rejected_candidates。",
                    "若 status=partial，则不得把 graph_span_alignment_payload 作为 Step6 可直接消费的正式 graph mapping 混入主链；若提交该字段，也只能作为分析性审计信息。",
                    "若 status=partial，则必须提供顶层 unresolved_template_summary；其每条摘要至少要说明 template_key、affected_span_count、stuck_at、why_not_freezable。",
                    "artifact_promotion.graph_span_candidates_payload、forward_segment_template_payload 必须使用 {'status': ..., 'row_count': N, 'rows': [...]} 包装；graph_span_alignment_payload 只有在 status=passed 时才允许作为正式 graph mapping payload。",
                    "graph_span_candidates_payload.rows[*].span_id 必须属于 graph_mapping_targets.json.rows[*].span_id；不得新增 formal graph target。",
                    "若 status=passed，则 graph_span_alignment_payload.rows[*].span_id 必须属于 graph_mapping_targets.json.rows[*].span_id；不得新增 formal graph target。",
                    "若 status=passed，则 graph_span_alignment_payload.rows[*].graph_operator_span_id 必须属于 graph_operator_spans.json，且其 span_id 必须与对应 operator span 一致。",
                    "graph_path_analyst 必须把 graph_operator_spans.json 作为按 phase/time 排列的 ordered operator span sequence 来分析，而不是只按逐 span 独立点下钻。",
                    "path_reconstruction 可选补充 sequence_evidence_summary、distinctive_kernel_anchors、pattern_segments，但这些 sequence 字段只能增强推理，不能替代 repo 事实与 operator_call 下钻。",
                    "graph_skeleton_scope.semantic_skeleton 定义为 graph_mapping_targets.json.rows[*].span_id；graph_execution_plan.json 只负责 graph inventory / phase windows；graph_skeleton_scope.operator_skeleton 定义为 graph_operator_spans.json.rows[*].graph_operator_span_id，子 agent 只能在这两层 skeleton 内做解释和下钻。",
                    "原则上不要在 artifact_promotion.graph_execution_plan_updates 中重写 identified_graph_span_ids 或 phase_windows[*].span_ids；若确有必要，只允许做等价重排、去重或收窄，不允许扩出 graph_mapping_targets.json 已冻结范围之外的新 span。",
                    "若顶层 status=partial，则 artifact_promotion.graph_execution_plan_updates.status 与 graph_forward_context_updates.status 也必须显式写 partial。",
                    "若 repo_divergence_report.json 指示 knowledge_unreliable，报告中必须显式说明知识文档仅作弱参考。",
                    "references/knowledge 下的文件允许为空白；为空时必须继续基于当前 repo 和输入工件完成分析，不得把空白文档当成阻塞理由。",
                    "若 status=passed，则所有正式 graph 条目必须满足 location_kind=operator_call，且其源码行不得是 self.xxx(...) 模块调用、构造行或 .replay() 入口。",
                    "graph/phase 判断应优先参考 graph_phase_stack_evidence.json 提供的 MODEL_EXECUTE phase markers：verify 必须由 npu_graph_runner.py::replay 确认其对应的 MODEL_EXECUTE，后续 MODEL_EXECUTE 再按时间顺序依次作为 draft_prefill、draft_decode 的开始；并在 timeline_index.json.trace_spans 中直接定位 marker 右侧连续 NOTIFY_WAIT / NOTIFY_WAIT_SQE block，用该 block 的结束收敛 phase window 右边界；若缺少可信 MODEL_EXECUTE marker、verify 支撑或 NOTIFY_WAIT 数据源，必须直接报错，不得回退 Step3 phase hint、时间三等分或 group span end fallback。",
                    "graph_forward_context.json 当前只提供模型文件、forward anchors、support/search roots 等候选上下文；communication/cache/operator 最终代码行必须由你在当前 repo 中继续下钻确认，不能直接引用纯关键词提示。",
                    "在形成最终结论前，必须再对照 references/knowledge/forward_analysis_rules.md 做一次显式规则符合性检查，并把结果写入 graph_review_result.json.rules_conformance_check；若与规则不一致但 repo/profiling 证据更强，必须在其中说明原因。",
                    "若启动参数或 runtime_constraints 表明量化方式是 modelslim，则必须检查模型目录下的 quant_model_description.json（仅在该量化方式下要求此文件），并在路径重建中优先使用其模块级量化标注。",
                    "若 status=passed，则 graph_span_alignment_payload 的正式逐 span 条目必须显式包含 span_id，且 code_location 必须是 machine-consumable file:line；不得提交只有 group/template 级信息、无法被 Step6 直接消费的伪 alignment 结构。",
                ],
            }
        )
    elif agent_name == "artifact_validator":
        payload.update(
            {
                "goal": "验证最终交付物契约、覆盖率、annotated trace 结构以及 graph replay 精度门禁。",
                "required_wrapper_script": "scripts/write_validation_outputs.py",
                "graph_span_candidates_path": str(workspace_dir / "artifacts" / "graph" / "graph_span_candidates.json"),
                "forward_segment_template_path": str(workspace_dir / "artifacts" / "graph" / "forward_segment_template.json"),
                "graph_operator_spans_path": str(workspace_dir / "artifacts" / "graph" / "graph_operator_spans.json"),
                "expected_coverage_fields": [
                    "total_span_count",
                    "semantic_span_count",
                    "excluded_span_count",
                    "mapped_span_count",
                    "unresolved_semantic_span_count",
                    "low_confidence_span_count",
                ],
                "required_scope_checks": [
                    "大部分 CAPTURE/EVENT/AscendCL/Runtime Event span 不应进入 semantic 集合",
                    "强制保留的功能性算子不应被误排除",
                    "NOTIFY_RECORD_SQE、NOTIFY_WAIT_SQE 与其他 runtime control span 不应静默混入 semantic 集合",
                ],
                "must_not_do": [
                    "不得为了让 gate 通过而淡化 graph replay 精度问题",
                    "不得修改 span_code_mapping、annotated trace 或 timeline 工件",
                ],
                "allowed_official_scripts": ["scripts/write_validation_outputs.py"],
                "acceptance_checks": [
                    "validation_result.json.status 必须在 allowed_status 内",
                    "status=failed 是合法的正式审计结果；发现阻塞问题时必须如实写 failed，而不是为推进流程伪装 passed。",
                    "checks 中必须包含 mapping_complete、annotated_trace_args_code_location_ok、no_top_level_code_location、timeline_order_stable、graph_precision_satisfied",
                    "required_input_files 必须包含 graph_span_candidates.json 与 forward_segment_template.json，保证 Step7 合同与 final gate 所需工件集合一致。",
                    "graph precision 检查必须显式消费 graph_operator_spans.json，不能只看 graph_span_alignment 的自报结果。",
                    "graph_span_candidates.json 只能作为 formal graph target 候选与范围复核输入，forward_segment_template.json 只能作为辅助解释层输入；两者都不得覆盖 graph_span_alignment.json 的正式结论。",
                    "graph precision 检查必须进一步读取源码行，识别 module_call_anchor、constructor_line 与 replay 入口。",
                ],
            }
        )
    elif agent_name == "artifact_renderer":
        payload.update(
            {
                "goal": "通过单一 wrapper 脚本执行完整 Step 6 渲染流水线，生成正式交付物并避免中间态时序问题。",
                "required_wrapper_script": "scripts/run_step6_render_pipeline.py",
                "graph_operator_spans_path": str(workspace_dir / "artifacts" / "graph" / "graph_operator_spans.json"),
                "allowed_official_scripts": ["scripts/run_step6_render_pipeline.py"],
                "wrapper_lock_path": str(workspace_dir / "logs" / "wrapper_runs" / "step6_wrapper.lock.json"),
                "wrapper_status_path": str(workspace_dir / "audit" / "step6_in_progress.json"),
                "must_not_do": [
                    "不得自行拆开逐条运行 map_spans_to_code.py、annotate_trace_view.py、render_stream_span_timeline.py、write_render_outputs.py。",
                    "不得在 wrapper 失败后临时 debug 分析 state 竞争或自行重排执行顺序。",
                    "不得修改 Step 3-5 的任何输入工件。",
                    "若 graph_span_alignment.json 不是 operator_call 级逐 span 结构，或缺少 graph_operator_spans.json，不得依赖 phase hint / graph hint / template expansion / 全量 stack 扫描做静默补偿。",
                ],
                "allowed_diagnostics": [
                    "允许只读查看 audit/step6_in_progress.json，确认 script_index/total_scripts、current_child_script、stage_phase、heartbeat_count、output_line_count、idle_seconds。",
                    "允许只读查看 logs/wrapper_runs/step6_*.combined.log 与 *.meta.json，确认当前 wrapper 是否仍在运行。",
                    "允许只读检查 span_code_mapping.json、trace_view.annotated.json、stream_span_timeline.json 是否已生成。",
                    "禁止把诊断动作升级为拆脚本补跑、补写交付物或手工改写 state。",
                ],
                "wrapper_log_contract": [
                    "Step 6 wrapper 会为每个子脚本生成 logs/wrapper_runs/step6_<script>.combined.log 与对应 *.meta.json。",
                    "Step 6 wrapper 还会持续维护 audit/step6_in_progress.json，暴露 current_child_script、current_child_log_path、stage_phase、heartbeat_count、output_line_count、idle_seconds 等运行态字段。",
                    "Step 6 wrapper 入口会维护 logs/wrapper_runs/step6_wrapper.lock.json；若检测到已有活跃实例，新的调用会立即失败并提示禁止重跑。",
                    "若终端显示命令结束，但 step6_wrapper.lock.json 仍为 running，应优先以 wrapper lock 与 child 日志判定真实状态，不得据此重跑。",
                ],
                "terminal_strategy": [
                    "只能用单条 blocking 命令直接运行 required_wrapper_script。",
                    "运行 wrapper 的 terminal 在 wrapper 完成前不得再发任何命令。",
                    "若需要观察进度，只能在另一个 terminal 只读查看 audit/step6_in_progress.json、logs/wrapper_runs/step6_*.combined.log 或 *.meta.json。",
                ],
                "forbidden_launch_methods": [
                    "禁止 Start-Process",
                    "禁止 Start-Job",
                    "禁止 cmd /c start",
                    "禁止后台 detached 运行",
                    "禁止生成 .ps1/.cmd 临时包装脚本",
                ],
                "retry_policy": [
                    "若 step6_wrapper.lock.json 显示已有活跃实例，禁止重跑；只能等待或只读查看日志。",
                    "若 wrapper 返回非 0，必须直接视为 Step 6 失败，不得拆脚本补跑。",
                ],
                "completion_signal_priority": [
                    "对 Step 6 wrapper，终端 exit code 不是唯一完成信号；logs/wrapper_runs/step6_wrapper.lock.json 的状态优先级更高。",
                    "若终端显示命令结束或 exit code=0，但 step6_wrapper.lock.json 仍为 running，必须认定 wrapper 仍在运行或刚被外部中断，不得据此判成功或重跑。",
                    "只有当 step6_wrapper.lock.json 明确收口为 passed/failed，或 wrapper lock、step6_in_progress、child 日志与正式渲染工件同时证明流程已结束时，才允许判定 Step 6 完成。",
                    "只要 step6_wrapper.lock.json 仍为 running，就不得仅因长时间无新 stdout、无新交付物或 idle_seconds 偏大而判失败。",
                ],
                "required_post_checks": [
                    "span_code_mapping.json 必须存在且 state.artifacts.span_code_mapping_path 已回写。",
                    "trace_view.annotated.json 必须存在且 state.artifacts.annotated_trace_path 已回写。",
                    "stream_span_timeline.json 必须存在且 state.artifacts.stream_span_timeline_path 已回写。",
                    "render_result.json.status 必须在 allowed_status 内。",
                ],
                "acceptance_checks": [
                    "只能调用 scripts/run_step6_render_pipeline.py --workspace-dir <workspace>。",
                    "若 wrapper 返回非 0，必须直接视为 Step 6 失败，不得继续补跑下游脚本。",
                    "若 wrapper 在前置校验中发现 graph_operator_spans.json 缺失、graph_span_alignment 缺少 span_id / graph_operator_span_id、code_location 非 file:line，或 graph 条目仍非 operator_call，必须立即失败，不得继续静默 fallback。",
                ],
            }
        )
    elif agent_name == "profiling_debugger":
        payload.update(
            {
                "goal": "基于 error_context.json 与最近失败工件给出最小修复动作、验证点和重试检查点。",
                "required_reference_files": [
                    "references/agents/profiling_debugger.md",
                ],
                "must_not_do": [
                    "不得修改 state.json、任何现有分析结果或业务工件。",
                    "不得自行调用 pre_step_check.py、prepare_agent_dispatch.py、finalize_agent_dispatch.py、mark_step_complete.py、check_final_gate.py、post_error_check.py。",
                    "不得把问题扩大成全仓泛化改造，或在证据不足时伪造根因。",
                ],
                "allowed_official_scripts": [],
                "acceptance_checks": [
                    "fix_instructions.json.status 必须在 allowed_status 内。",
                    "fix_instructions.json 必须至少包含 status、diagnosis、actions、verification_points。",
                    "fix_instructions.json.actions 必须是非空列表，且每条动作都应可执行。",
                    "debug_report.md 必须围绕当前失败点给出最小修复动作与明确检查点。",
                ],
            }
        )
    else:
        raise ValueError(f"不支持的 agent: {agent_name}")

    return task_path, payload


def main() -> int:
    args = build_parser().parse_args()
    workspace_dir = Path(args.workspace_dir)
    task_path, payload = build_payload(workspace_dir, args.agent_name)
    dump_json(task_path, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
