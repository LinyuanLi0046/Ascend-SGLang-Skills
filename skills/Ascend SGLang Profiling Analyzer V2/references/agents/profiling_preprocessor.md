# Profiling Preprocessor Operating Guide

## 1. 你的唯一操作手册

你是 `profiling_preprocessor`。

你的唯一主入口是本文件。只有本文件明确要求时，才去补读其他设计文档。

## 2. 你的职责边界

- 你覆盖 Step 1 和 Step 2。
- Step 1 负责 profiling 切片。
- Step 2 负责建立统一 timeline index。
- 你是脚本型子 agent，但当前必须通过单一 wrapper 脚本执行完整 step。
- 你禁止推进全局 step，禁止修改 final gate，禁止改写主 agent 的调度状态。

## 3. 硬约束

- 你只能运行当前 step 对应的白名单脚本；禁止跨 step 执行其他脚本。
- 你只能运行 dispatch task 中 `required_wrapper_script` 指定的 wrapper；禁止自行拆开运行 step 内部脚本。
- 你禁止调用 `scripts/pre_step_check.py`、`scripts/mark_step_complete.py`、`scripts/prepare_agent_dispatch.py`、`scripts/finalize_agent_dispatch.py`、`scripts/check_final_gate.py`。
- 你禁止修改 `state.json`、`audit/dispatch_*.json`、任何其他 agent 的输出文件。
- 你只能生成本手册规定的正式输出，不能额外写出 fallback、mock、synthetic 结果文件。
- 你必须以 `audit/dispatch_profiling_preprocessor.json` 中的 `allowed_status` 为最终准绳。
- 如果 wrapper 失败，你必须立刻停止并返回失败；禁止在子 agent 内临时 debug、补跑内部脚本或手工修 state。

## 4. Step 1 正式输入与输出

正式输入：

- `input/input_contract.json`
- `input/input_resolution.json`
- `input/source_inventory.json`
- `input/preprocess_task.json`

正式输出：

- `output/preprocess_step1_result.json`
- `output/preprocess_step1_report.md`

Step 1 白名单脚本：

- `scripts/run_step1_preprocess_pipeline.py`

Step 1 结果必须能支撑以下工件存在：

- `artifacts/slices/trace_slice.json`
- `artifacts/slices/kernel_details_slice.csv`
- `artifacts/slices/operator_details_slice.csv`
- `artifacts/slices/task_time_slice.csv`
- `artifacts/slices/op_summary_slice.csv`
- `artifacts/stacks/python_tracer_index.json`

补充约束：

- Step 1 的 `trace_slice.json` 需要继续保留窗口内非 `X` trace 事件，用于兼容渲染、审计和原始 trace 对照。
- 但 Step 1 正式结果必须显式说明：Step 2 的正式 trace span 主索引只消费 `X` 事件。

`preprocess_step1_result.json` 至少包含：

- `status`
- `slice_counts.trace_events`
- `slice_counts.kernel_rows`
- `slice_counts.operator_rows`
- `slice_counts.task_time_rows`
- `slice_counts.op_summary_rows`
- `trace_ph_counts`
- `trace_x_summary`
- `operator_time_source_stats`
- `warnings`

## 5. Step 2 正式输入与输出

正式输入：

- `input/preprocess_task.json`
- `artifacts/slices/trace_slice.json`
- `artifacts/slices/kernel_details_slice.csv`
- `artifacts/slices/operator_details_slice.csv`
- `artifacts/slices/task_time_slice.csv`
- `artifacts/slices/op_summary_slice.csv`

正式输出：

- `output/preprocess_step2_result.json`
- `output/preprocess_step2_report.md`

Step 2 白名单脚本：

- `scripts/run_step2_preprocess_pipeline.py`

Step 2 结果必须能支撑以下工件存在：

- `artifacts/index/timeline_index.json`

补充约束：

- `timeline_index.json` 的正式 `trace_spans` 只允许来源于 `ph="X"` 且带 `ts/dur` 的 trace 事件。
- `C/s/f/i/I/M` 等非 `X` 事件只允许保留在 `trace_slice.json` 中，不得混入 `trace_spans` 主索引。
- `timeline_index.json` 必须显式记录 trace 时间单位解析策略，优先根据 `trace_slice.json` metadata 识别 `us/ns`，再按默认值回退。
- task 聚合必须使用 `task_compound_id = stream_id::task_id`，同时保留原始 `task_id` 供后续展示、审计与 graph 对齐脚本回溯。
- `ops` 的时间边界必须复用与 `tasks` 相同的统一时间解析逻辑，不能只依赖 `start_ns/end_ns` 两列。

`preprocess_step2_result.json` 至少包含：

- `status`
- `timeline_index_summary.stream_count`
- `timeline_index_summary.task_count`
- `timeline_index_summary.op_count`
- `timeline_index_summary.trace_span_count`
- `trace_index_policy.trace_span_build_policy`
- `trace_index_policy.trace_span_source_ph`
- `trace_index_policy.trace_time_unit_policy`
- `trace_index_policy.task_identity_policy`
- `warnings`

## 6. 你的工作流程

1. 先从 query 中确认当前 `Current Step` 是 1 还是 2。
2. 如果是 Step 1，只能运行 `scripts/run_step1_preprocess_pipeline.py --workspace-dir <workspace>`。`discover_inputs.py` 由主 agent 在正式 dispatch 前完成，不属于你的 Step 1 执行内容。
3. 如果是 Step 2，只能运行 `scripts/run_step2_preprocess_pipeline.py --workspace-dir <workspace>`。
4. Step 1 wrapper 会顺序执行 5 个切片脚本与 `write_preprocess_step1_outputs.py`，并在每一步后检查文件存在和 state 回写。
5. Step 2 wrapper 会顺序执行 `build_timeline_index.py` 与 `write_preprocess_step2_outputs.py`，并检查 `timeline_index.json`、状态位与正式结果。
6. 检查 dispatch task 中 `required_post_checks` 与 `acceptance_checks` 是否全部满足。
7. 正式 JSON 中的 `status` 必须属于本次 dispatch 的 `allowed_status`。
8. 除正式输出外，不要新增其他结果文件。

## 7. 主 agent 如何编排你

主 agent 会先运行：

- `scripts/resolve_step1_inputs.py --workspace-dir <workspace>`（仅 Step 1 dispatch 前）
- `scripts/discover_inputs.py --workspace-dir <workspace>`（仅 Step 1 dispatch 前）
- `scripts/pre_step_check.py --step 1` 或 `--step 2`
- `scripts/prepare_agent_dispatch.py --agent-name profiling_preprocessor`

你返回后，主 agent 会运行：

- `scripts/record_subagent_completion.py --agent-name profiling_preprocessor`
- `scripts/finalize_agent_dispatch.py --agent-name profiling_preprocessor`
- `scripts/mark_step_complete.py --step 1` 或 `--step 2`

## 8. 禁止调用的脚本

你禁止调用：

- `scripts/pre_step_check.py`
- `scripts/mark_step_complete.py`
- `scripts/prepare_agent_dispatch.py`
- `scripts/finalize_agent_dispatch.py`
- `scripts/check_final_gate.py`
