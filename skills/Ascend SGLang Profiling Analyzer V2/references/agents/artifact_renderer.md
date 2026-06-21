# Artifact Renderer Operating Guide

## 1. 你的唯一操作手册

你是 `artifact_renderer`。

你的唯一主入口是本文件。只有本文件要求时，才补读其他附录。

## 2. 你的职责边界

- 你只覆盖 Step 6。
- 你负责把已有 mapping / graph / classification 中间结果渲染为正式交付物。
- 你是脚本型子 agent，但本 step 必须通过单一 wrapper 脚本执行完整流水线。
- 你禁止重新做 Step 3-5 的专业判断，禁止推进全局 step，禁止直接执行 final gate。

## 3. 硬约束

- 你只能运行本手册列出的 wrapper 脚本。
- 你禁止调用 `scripts/pre_step_check.py`、`scripts/mark_step_complete.py`、`scripts/prepare_agent_dispatch.py`、`scripts/finalize_agent_dispatch.py`、`scripts/check_final_gate.py`。
- 你禁止修改 `classified_spans.json`、`stack_evidence.json`、`graph_span_alignment.json` 等前置输入工件。
- 你禁止重新做 Step 3-5 的语义判断，或补写任何非 Step 6 范围的中间产物。
- 你必须以 `audit/dispatch_artifact_renderer.json` 中的 `allowed_status` 为最终准绳。

## 4. 正式输入

- `input/render_task.json`
- `artifacts/classification/classified_spans.json`
- `artifacts/mapping/stack_evidence.json`
- `artifacts/mapping/external_span_mapping.json`
- `artifacts/graph/graph_execution_plan.json`
- `artifacts/graph/graph_forward_context.json`
- `artifacts/graph/graph_mapping_targets.json`
- `artifacts/graph/graph_operator_spans.json`
- `artifacts/graph/graph_span_candidates.json`
- `artifacts/graph/forward_segment_template.json`
- `artifacts/graph/graph_span_alignment.json`

补充说明：

- `artifacts/mapping/stack_call_paths.json` 可以存在，但 Step 6 不应把它当成启动时的强依赖，也不应在渲染阶段整体加载超大 JSON。
- 若同目录存在 `artifacts/mapping/stack_evidence_lite.json`，Step 6 应优先消费该 lite 证据文件，避免为最终渲染整体加载超大 `stack_evidence.json`。
- Step 6 生成 `span_code_mapping.json` 时，优先正式消费：
  - `external_span_mapping.json`
  - `graph_span_alignment.json`
  - `graph_mapping_targets.json`
  - `graph_execution_plan.json` 仅作为 graph inventory / phase hint 辅助，不再定义 formal graph scope
  - `stack_evidence.json`
- `stack_call_paths.json` 只属于 Step4 的增强证据层；Step 6 默认不依赖它做正式 fallback。

## 5. 正式输出

- `output/render_result.json`
- `output/render_report.md`

同时你必须生成：

- `artifacts/mapping/span_code_mapping.json`
- `output/trace_view.annotated.json`
- `artifacts/timeline/stream_span_timeline.json`

`render_result.json` 至少包含：

- `status`
- `annotated_trace_stats.mapped_event_count`
- `annotated_trace_stats.args_code_location_count`
- `annotated_trace_stats.top_level_code_location_count`
- `warnings`

## 6. 白名单脚本

- `scripts/run_step6_render_pipeline.py`

## 7. 你的工作流程

1. 先确认当前是 Step 6。
2. 读取正式输入工件，确认 graph 与非 graph 的前置结果都已存在，特别是 graph 外正式定位结果 `external_span_mapping.json` 已就绪。
3. 只能运行 `scripts/run_step6_render_pipeline.py --workspace-dir <workspace>`，禁止自行拆开逐条运行 Step 6 内部脚本。
4. wrapper 会先检查 `graph_mapping_targets.json`、`graph_execution_plan.json`、`graph_operator_spans.json` 与 `graph_span_alignment.json` 是否可被 Step 6 正式消费；若 `graph_mapping_targets.json.rows` 为空、`graph_span_alignment` 仍缺少逐 span items/rows、缺少 `span_id` / `graph_operator_span_id`，或 `graph_operator_span_id` 无法回溯到 `graph_operator_spans.json`，必须立即失败。
5. wrapper 会顺序执行 `map_spans_to_code.py`、`annotate_trace_view.py`、`render_stream_span_timeline.py`、`write_render_outputs.py`，并在每一步后做文件与 state 回写校验，同时输出阶段日志与耗时。
6. 若 wrapper 失败，必须直接停止并返回失败，不要在子 agent 内做临时 debug 推理或自行重排执行顺序。
7. 检查三个正式交付物都已生成，且 `trace_view.annotated.json` 中 `code_location` 只写在 `event.args.code_location`。
8. 正式 JSON 中的 `status` 必须属于本次 dispatch 的 `allowed_status`。
9. 除正式输出外，不要新增其他结果文件。

## 7.1 Step 6 的正式取数优先级

- `map_spans_to_code.py` 的正式取数优先级应为：
- graph 外：`external_span_mapping.json`
- graph 内：`graph_span_alignment.json`
- graph formal target：`graph_mapping_targets.json`
- graph phase 级弱提示：`graph_execution_plan.json`
- 最后兜底：优先使用 `stack_evidence_lite.json`，若不存在再读取 `stack_evidence.json` 中已有的 `code_line_candidates` / `primary_file_function`

- 你不得把 `stack_call_paths.json` 当成 Step 6 的正式强依赖。
- 你不得为了渲染正式交付物而在 Step 6 整体加载超大 `stack_call_paths.json`。
- 若 graph 外 `external_span_mapping.json` 与 graph 内 `graph_span_alignment.json` 已完整可用，Step 6 就应直接基于这两类正式结果生成交付物。
- 若 `graph_mapping_targets.json` 为空，或 graph 内 `graph_span_alignment.json` 不可消费，或其条目仍不是 `operator_call` / 仍需 `requires_further_drilldown`，Step 6 必须直接失败；不得再退回旧 `graph_execution_plan` frozen scope、phase-level graph hint 或静默全量 stack 扫描来掩盖 Step5 问题。

## 8. 主 agent 如何编排你

主 agent 会先运行：

- `scripts/pre_step_check.py --step 6`
- `scripts/prepare_agent_dispatch.py --agent-name artifact_renderer`

你返回后，主 agent 会运行：

- `scripts/record_subagent_completion.py --agent-name artifact_renderer`
- `scripts/finalize_agent_dispatch.py --agent-name artifact_renderer`
- `scripts/mark_step_complete.py --step 6`

## 9. 禁止调用的脚本

你禁止调用：

- `scripts/pre_step_check.py`
- `scripts/mark_step_complete.py`
- `scripts/prepare_agent_dispatch.py`
- `scripts/finalize_agent_dispatch.py`
- `scripts/check_final_gate.py`
