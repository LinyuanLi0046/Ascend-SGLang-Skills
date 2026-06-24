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
- 如果 wrapper 失败，你必须立刻停止并返回失败；禁止在子 agent 内拆脚本补跑、手工修 state 或伪造正式结果。
- 允许只读诊断当前 wrapper 是否仍在运行，例如查看 `audit/step1_in_progress.json` / `audit/step2_in_progress.json`、`logs/wrapper_runs/*.combined.log`、`logs/wrapper_runs/*.meta.json`、以及目标 artifacts 是否已生成；但这些诊断不能升级为补跑或改写工件。
- wrapper 现在会为每个子脚本生成执行日志和心跳，并持续写出 `*_in_progress.json` 暴露 `current_child_script`、`current_child_log_path`、`stage_phase`、`heartbeat_count`、`output_line_count`、`idle_seconds`；遇到长时间静默时，应优先读取这些状态，而不是仅凭主界面没有新输出就判断失败。
- 对 `scripts/run_step1_preprocess_pipeline.py` 与 `scripts/run_step2_preprocess_pipeline.py`，你只能用单条 blocking 命令直接运行；禁止 `Start-Process`、`Start-Job`、`cmd /c start`、后台 detached 运行，禁止生成 `.ps1/.cmd` 临时包装脚本。
- 若 `logs/wrapper_runs/step1_wrapper.lock.json` 或 `logs/wrapper_runs/step2_wrapper.lock.json` 显示已有活跃实例，禁止再次启动对应 wrapper；只能等待当前实例完成或只读查看执行日志。
- 对 Step 1 / Step 2 这类大文件、长耗时、会拉起子进程的 wrapper，终端 `exit code` 不是最终完成信号，而是明确不可靠的弱信号；你必须假设它可能早于真实结束出现，或与真实状态脱节。
- 对 Step 1 / Step 2，禁止仅凭终端 `exit code`、命令返回、Task 本轮回复、CLI 不再刷屏，就判定 wrapper 已成功、已失败、已结束或可重跑。
- 若终端显示命令结束、CLI 显示 `exit code=0/1`，但对应 wrapper lock 仍为 `running`，必须认定 Step 仍未结束；此时唯一允许动作是继续等待，或在其他 terminal 只读查看 wrapper lock、`*_in_progress.json`、child 日志与工件增长情况。
- 只要 `step1_wrapper.lock.json` / `step2_wrapper.lock.json` 仍为 `running`，就不得仅因长时间无新 stdout、无新产物、`idle_seconds` 偏大或 CLI 长时间不刷屏而判失败。
- 只有当 `step1_wrapper.lock.json` / `step2_wrapper.lock.json` 明确收口为 `passed` / `failed`，或 lock、`*_in_progress.json`、子日志、正式工件共同证明流程已结束时，才允许判定 step 完成。
- 主 agent 在运行 `scripts/record_subagent_completion.py` 记录 completion 前，也会再次检查 `step1_wrapper.lock.json` / `step2_wrapper.lock.json` 是否已收口；若 lock 仍是 `running`，completion 不会被记录。
- 主 agent 在运行 `scripts/finalize_agent_dispatch.py` 前，还会再次核对真实 wrapper lock 与 completion marker 中记录的 lock 状态；若两者不一致、lock 未终态或仍在运行，finalize 必须失败，不允许继续推进。

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
- `logs/wrapper_runs/step1_<script>.combined.log` 与对应 `*.meta.json` 属于执行日志，不属于正式结果文件，但可作为只读诊断证据。
- `logs/wrapper_runs/step1_wrapper.lock.json` 是 Step 1 wrapper 的正式运行锁；`audit/step1_in_progress.json` 是 Step 1 的增强运行态快照。两者都不是正式结果文件，但可用于判定是否已有活跃实例在运行、当前正在执行哪个 child script、以及是否仍在正常推进。
- 在 `Windows + 大 trace` 场景下，wrapper 与 `slice_trace_workspace.py` 会自动启用更细进度粒度与心跳输出，避免把排序/写盘阶段误判为卡住。
- 若 `artifacts/slices/trace_slice.json` 已存在、state 中 `artifacts.trace_slice_path` 指向正确且文件非空，Step 1 wrapper 会直接跳过 `slice_trace_workspace.py`，避免误重跑时再次扫描大 trace。

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
- `logs/wrapper_runs/step2_<script>.combined.log` 与对应 `*.meta.json` 属于执行日志，不属于正式结果文件，但可作为只读诊断证据。
- `logs/wrapper_runs/step2_wrapper.lock.json` 是 Step 2 wrapper 的正式运行锁；`audit/step2_in_progress.json` 是 Step 2 的增强运行态快照。两者都不是正式结果文件，但可用于判定是否已有活跃实例在运行、当前正在执行哪个 child script、以及是否仍在正常推进。

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
   同时 wrapper 会为每个子脚本记录 `logs/wrapper_runs/step1_<script>.combined.log` 与 `logs/wrapper_runs/step1_<script>.meta.json`，并持续维护 `audit/step1_in_progress.json`。
   `step1_in_progress.json` 会暴露 `current_child_script`、`current_child_log_path`、`stage_phase`、`heartbeat_count`、`output_line_count`、`idle_seconds` 等运行态字段。
   Step 1 wrapper 入口还会维护 `logs/wrapper_runs/step1_wrapper.lock.json`；若检测到已有活跃实例，新的调用会立即失败并提示禁止重跑。
   若终端界面先显示 `exit code=0`，但 `step1_wrapper.lock.json` 仍为 `running`，必须继续视为 Step 1 未完成。
   只要 lock 仍为 `running`，就不得仅因长时间无新 stdout、无新产物或 `idle_seconds` 偏大而判失败。
5. Step 2 wrapper 会顺序执行 `build_timeline_index.py` 与 `write_preprocess_step2_outputs.py`，并检查 `timeline_index.json`、状态位与正式结果。
   同时 wrapper 会为每个子脚本记录 `logs/wrapper_runs/step2_<script>.combined.log` 与 `logs/wrapper_runs/step2_<script>.meta.json`，并持续维护 `audit/step2_in_progress.json`。
   `step2_in_progress.json` 会暴露 `current_child_script`、`current_child_log_path`、`stage_phase`、`heartbeat_count`、`output_line_count`、`idle_seconds` 等运行态字段。
   Step 2 wrapper 入口还会维护 `logs/wrapper_runs/step2_wrapper.lock.json`；若检测到已有活跃实例，新的调用会立即失败并提示禁止重跑。
   若终端界面先显示 `exit code=0`，但 `step2_wrapper.lock.json` 仍为 `running`，必须继续视为 Step 2 未完成。
   只要 lock 仍为 `running`，就不得仅因长时间无新 stdout、无新产物或 `idle_seconds` 偏大而判失败。
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

- `scripts/record_subagent_completion.py --agent-name profiling_preprocessor --task-call-id <task_agent_id>`
- `scripts/finalize_agent_dispatch.py --agent-name profiling_preprocessor`
- 至少更新 `findings.md` 或 `progress.md`；若 `state.flags.task_plan_refresh_required=true`，还要同步更新 `task_plan.md`
- `scripts/mark_step_complete.py --step 1` 或 `--step 2`

## 8. 禁止调用的脚本

你禁止调用：

- `scripts/pre_step_check.py`
- `scripts/mark_step_complete.py`
- `scripts/prepare_agent_dispatch.py`
- `scripts/finalize_agent_dispatch.py`
- `scripts/check_final_gate.py`
