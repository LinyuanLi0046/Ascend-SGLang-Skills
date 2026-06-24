# Scripts And Gates

角色标签约定：

- `[main-agent-only]`：只能由主 agent 执行的编排、闭环或门禁脚本
- `[main-agent-deterministic]`：主链上的确定性前置脚本，可由主 agent 或 `prepare_agent_dispatch.py` 触发
- `[subagent-only]`：只能由对应子 agent 依据 task input / dispatch 调用的 wrapper 或正式处理脚本
- `[shared-library]`：被正式脚本导入的公共模块，不作为独立调度边界

## 1. 公共脚本

### `[shared-library]` `scripts/workflow_common.py`

职责：

- JSON 读写
- `state.json` 读写
- 文档哈希
- query 审计状态更新
- finalize 审计记录写出
- `code_location` 校验
- error context 写入

### `[main-agent-only]` `scripts/init_session.py`

职责：

- 初始化 workspace
- 生成 `state.json`
- 生成 `task_plan.md`、`findings.md`、`progress.md`

### `[main-agent-only]` `scripts/pre_step_check.py`

职责：

- step 前硬检查
- 不允许越级执行
- 不允许在 `call_profiling_debugger` 状态下继续推进

### `[main-agent-only]` `scripts/mark_step_complete.py`

职责：

- 检查当前 step 对应工件和 flag
- Step 4 支持 `--substep A|B`
- 检查文档是否更新；至少要求 `findings.md` 或 `progress.md` 有一处变更
- 若 `state.flags.task_plan_refresh_required=true`，还要求 `task_plan.md` 同步更新
- 推进 `state.current_step/current_substep`

## 2. 数据处理脚本

### `[main-agent-deterministic]` `scripts/resolve_step1_inputs.py`

输入：

- `state.inputs.profiling_root_path`
- `state.inputs.code_repo_path`
- `state.inputs.model_root_path`
- `state.inputs.launch_command_file`
- `state.inputs.launch_command_text`
- `state.inputs.window_start_ns`
- `state.inputs.window_end_ns`
- `state.inputs.supplemental_input_paths`

输出：

- `input/input_resolution.json`
- `input/launch_command.normalized.json`
- 回写 `state.inputs.model_root_path`
- 回写 `state.inputs.draft_model_root_path`
- 回写 `state.inputs.launch_command_file` / `state.inputs.launch_command_text`
- 回写 `state.inputs.window_start_ns` / `state.inputs.window_end_ns`

### `[main-agent-deterministic]` `scripts/discover_inputs.py`

输入：

- `state.inputs.*`

输出：

- `input/input_resolution.json`
- `input/input_contract.json`
- `input/source_inventory.json`

补充说明：

- `input/input_contract.json` 中与路径相关的正式字段命名统一跟随 `state.inputs`，使用 `profiling_root_path`、`code_repo_path`、`model_root_path`、`draft_model_root_path`

### `[subagent-only]` `scripts/slice_trace_workspace.py`

用途：

- 基于 workspace 状态调用 `slice_profiling.py`
- 对超大顶层 list 形式的 `trace_view.json` 自动切换到低内存流式切片路径，避免单入口 orchestrator 在大文件上静默退出

输出：

- `artifacts/slices/trace_slice.json`

### `[subagent-only]` `scripts/slice_kernel_workspace.py`

用途：

- 基于 workspace 状态调用 `slice_kernel_csv.py`

输出：

- `artifacts/slices/kernel_details_slice.csv`

### `[subagent-only]` `scripts/slice_operator_details.py`

输出：

- `artifacts/slices/operator_details_slice.csv`

### `[subagent-only]` `scripts/slice_task_time_csv.py`

输出：

- `artifacts/slices/task_time_slice.csv`

### `[subagent-only]` `scripts/slice_op_summary_csv.py`

输出：

- `artifacts/slices/op_summary_slice.csv`

### `[subagent-only]` `scripts/build_timeline_index.py`

输出：

- `artifacts/index/timeline_index.json`

### `[main-agent-deterministic]` `scripts/classify_spans.py`

输出：

- 默认输出 `artifacts/classification/classified_spans.json`
- Step 3 wrapper 内可改写到 `artifacts/classification/classified_spans.base.json`

用途：

- 生成 Step 3 的确定性 classified base 结果
- 默认模式下保持旧 canonical 写盘行为
- 在 wrapper / merge 调用中支持自定义输出路径并禁用 state 回写

### `[main-agent-deterministic]` `scripts/check_scope_gate.py`

输出：

- 默认输出 `output/scope_gate_result.json`
- Step 3 wrapper / merge 内可改写到：
  - `output/scope_gate_result.base.json`
  - `output/scope_gate_result.reviewed.json`

用途：

- 对当前 classified 结果执行 Step 3 scope gate 检查
- 默认模式下保持旧 canonical 写盘行为
- 在 wrapper / merge 调用中支持自定义 classified 输入路径、自定义输出路径，并禁用 state 回写

### `[main-agent-deterministic]` `scripts/build_stack_evidence.py`

输出：

- `artifacts/mapping/stack_evidence.json`

### `[main-agent-deterministic]` `scripts/classify_graph_groups.py`

输出：

- `artifacts/repo/repo_divergence_report.json`
- `input/runtime_constraints.json`
- `input/launch_command.json`
- `input/model_context.json`
- `artifacts/graph/graph_execution_plan.json`

用途：

- 识别当前有哪些 graph group，以及它们分别属于什么 phase
- 在保留既有 graph skeleton 产出的同时，补充生成知识适用性与运行约束工件
- 同时写出双层 graph 模式字段：`mode={spec_v2, decode_graph, disabled}` 供 Step 7 / final gate 消费，`graph_mode={speculative, decode_only, unknown}` 供 Step 4/5 prepare 合同消费
- 优先消费 `artifacts/mapping/stack_evidence_lite.json` 的摘要字段；若 lite 缺失必要字段，再回退 `stack_evidence.json`
- phase window 右边界优先通过 `MODEL_EXECUTE` marker 结束后的第一个合法 `NOTIFY_WAIT` / `NOTIFY_WAIT_SQE` task 收敛
- 若存在覆盖 `MODEL_EXECUTE` 结束时刻的 wait task，则优先使用该 task；否则直接选择时间上第一个后继 wait task
- 不再先把多个 `NOTIFY_WAIT` task 合并为 block，再用 gap 阈值做匹配；Step 5 phase 边界直接基于原始 wait task 识别
- 不负责 graph 内真实代码路径重建

### `[main-agent-deterministic]` `scripts/build_graph_operator_spans.py`

输出：

- `artifacts/graph/graph_operator_spans.json`

用途：

- 在 `graph_execution_plan.json.phase_windows` 内正式拆分 graph operator spans
- 只围绕 `graph_mapping_targets.json` 中已经冻结的 formal graph targets 生成 operator skeleton
- 为 Step 5/6/7 提供正式的 graph code alignment 对象，禁止再把 `MODEL_EXECUTE` marker、`replay()` 入口或 phase window 直接当成最终 graph span

### `[main-agent-deterministic]` `scripts/build_graph_mapping_targets.py`

输出：

- `artifacts/graph/graph_mapping_targets.json`

用途：

- 在保留 `graph_execution_plan.json` 的 graph inventory / phase windows / graph groups 语义前提下，冻结 Step 5 正式 graph target set
- 只保留 `classified_spans.json` 中未排除、具备 `stream_id` 且 `semantic_class in {compute, communication}`、并落在 graph phase window 内的 device spans
- 不再把 formal target set 回写进 `graph_execution_plan.json`
- `graph_execution_plan.json` 继续只表达 graph inventory / phase windows / graph groups；`graph_mapping_targets.json` 单独表达 formal graph target set

### `[main-agent-deterministic]` `scripts/check_repo_divergence.py`

输出：

- `artifacts/repo/repo_divergence_report.json`

用途：

- 检查预置知识文档与当前仓库热点路径/符号的一致性
- 为 Step 5 提供 `knowledge_applicable` / `knowledge_partially_applicable` / `knowledge_unreliable` 级别提示
- 同时输出 `existing_files` / `missing_files` / `existing_paths`，作为 Step 5 文件存在性判断的正式数据源之一

### `[main-agent-deterministic]` `scripts/build_runtime_constraints.py`

输出：

- `input/runtime_constraints.json`

用途：

- 从启动参数和模型 config 提取 graph/spec/backend/quant 等约束
- 为 graph_path_analyst 提供受限分析边界

### `[main-agent-deterministic]` `scripts/build_graph_forward_context.py`

输出：

- `artifacts/graph/graph_forward_context.json`

用途：

- 抽取候选模型文件、forward 锚点、decoder layer 锚点和通信热点
- 把 `repo_divergence_report.json` 与实际 repo exists 扫描收敛成 `repo_file_existence_facts`
- 为 `graph_path_analyst` 提供候选上下文，而不是在脚本中直接重建真实路径
- 只负责产出 `artifacts/graph/graph_forward_context.json`；`input/graph_seed_context.json` 改由 `build_graph_seed_context.py` 单独生成

### `[main-agent-deterministic]` `scripts/build_stack_evidence.py`

输出：

- `artifacts/mapping/stack_evidence.json`

用途：

- 从 `operator_details.csv` 的 `Call Stack` 解析原始 repo stack 证据
- 同时整理 graph 外 span 证据层 `external_span_rows`
- 同时整理以 `MODEL_EXECUTE` 为起点的 phase marker 证据层 `graph_phase_marker_rows` / `graph_replay_rows`

### `[main-agent-deterministic]` `scripts/build_stack_call_paths.py`

输出：

- `artifacts/mapping/stack_call_paths.json`

用途：

- 在原始 stack 证据上叠加 `python_tracer_index.json` 补充路径
- 为 graph 外 span 生成合并后的调用路径、最佳候选位置以及实现层证据/风险提示
- 直接产出 Step 4 的 `stack_call_paths.json` 正式中间工件，但只消费既有 `external_mapping_targets.json`，不再自行冻结 target set

### `[main-agent-deterministic]` `scripts/build_graph_phase_stack_evidence.py`

输出：

- `artifacts/graph/graph_phase_stack_evidence.json`

用途：

- 把 `stack_evidence.json` 中的 `MODEL_EXECUTE` / replay 相关 phase 证据冻结成 shared deterministic 工件
- 优先消费 `artifacts/mapping/stack_evidence_lite.json` 中的 `graph_phase_marker_rows` / `graph_replay_rows`；若 lite 不可用，再回退 `stack_evidence.json`
- 为 `classify_graph_groups.py` 提供正式 phase marker 来源
- 该工件不再由 `stack_mapper` 的正式 payload promotion 拥有

### `[main-agent-deterministic]` `scripts/build_external_mapping_targets.py`

输出：

- `artifacts/mapping/external_mapping_targets.json`

用途：

- 从 `classified_spans.json` 中选出 Step 3 已通过 gate 的 device-side semantic spans
- 用 `graph_mapping_targets.json` 做差，冻结 Step 4 唯一允许消费的 external formal target set
- 保证 `external_mapping_targets` 与 `graph_mapping_targets` 来自同一条 shared scope freeze 主链

### `[main-agent-deterministic]` `scripts/build_graph_seed_context.py`

输出：

- `input/graph_seed_context.json`

用途：

- 汇总 repo 偏差检测、运行约束、graph plan、forward context 与 profiling 摘要
- 优先消费 `artifacts/mapping/stack_evidence_lite.json` 的摘要字段；若 lite 不可用，再回退 `stack_evidence.json`
- 为后续 AI 候选路径分析提供受限上下文，而不改变当前 Step 5 既有成功判定
- 该脚本现在是 Step 5A wrapper 顺序执行链中的独立一步，不再由 `build_graph_forward_context.py` 隐式内部调用生成
- `prepare_agent_dispatch.py --agent-name graph_path_analyst` 只会在 Step 5B dispatch 前校验该工件已由 Step 5A 正式冻结，不负责代跑补齐

### `[shared-bootstrap-core]` `scripts/run_step4_bootstrap_pipeline.py`

输出：

- `logs/wrapper_runs/step4_bootstrap.lock.json`
- `logs/wrapper_runs/step4_step4_stack_mapper_<stage>.combined.log`
- `logs/wrapper_runs/step4_step4_stack_mapper_<stage>.meta.json`

用途：

- 作为 Step 4A shared bootstrap 的正式重执行面，统一托管 lock/status、heartbeat、child logs 与 metadata
- 对 `step4_stack_mapper` 顺序执行：`check_repo_divergence.py -> build_runtime_constraints.py -> build_stack_evidence.py -> build_graph_phase_stack_evidence.py -> classify_graph_groups.py -> build_graph_mapping_targets.py -> build_external_mapping_targets.py -> build_stack_call_paths.py`
- Step 4A 中，该 wrapper 由 `scripts/run_step4_bootstrap_runner.py` 在 `step4_bootstrap_runner` 子 agent 内托管
- Step 4A 的完成判定 owner 是 `references/agents/step4_bootstrap_runner.md`

### `[subagent-only]` `scripts/run_step4_bootstrap_runner.py`

输出：

- `output/step4_bootstrap_result.json`
- `output/step4_bootstrap_report.md`

用途：

- 作为 Step 4A `step4_bootstrap_runner` 的唯一正式执行面
- 内部托管 `run_step4_bootstrap_pipeline.py --target step4_stack_mapper`
- 在 wrapper 明确收口后，写出 Step 4A 的正式收据工件
- Step 4A wrapper 的详细结束判定、terminal 约束、只读观察与禁止动作，以 `references/agents/step4_bootstrap_runner.md` 为单一来源

### `[shared-bootstrap-core]` `scripts/run_step5_graph_bootstrap_pipeline.py`

输出：

- `logs/wrapper_runs/step5_graph_bootstrap.lock.json`
- `logs/wrapper_runs/step5a_<stage>.combined.log`
- `logs/wrapper_runs/step5a_<stage>.meta.json`

用途：

- 作为 Step 5A graph bootstrap 的正式重执行面，统一托管 lock/status、heartbeat、child logs 与 metadata
- 顺序执行：`build_graph_forward_context.py -> build_graph_seed_context.py -> build_graph_operator_spans.py`
- Step 5A 中，该 wrapper 由 `scripts/run_graph_bootstrap_runner.py` 在子 agent 内托管

### `[subagent-only]` `scripts/run_graph_bootstrap_runner.py`

输出：

- `output/graph_bootstrap_result.json`
- `output/graph_bootstrap_report.md`

用途：

- 作为 Step 5A `graph_bootstrap_runner` 的唯一正式执行面
- 内部托管 `run_step5_graph_bootstrap_pipeline.py`
- 在 wrapper 明确收口后，写出 Step 5A 的正式收据工件

### `[main-agent-only]` `scripts/finalize_agent_dispatch.py`

用途：

- 统一执行子 agent 正式输出的结构化门禁、payload promotion、状态回写和 provenance 更新
- Step 4/5/6/7 的正式结果都必须通过此脚本才能提升回主链工件
- 对于 graph 相关阶段，会继续复核 graph 粒度字段与下游消费契约，防止“看起来有结果、实际仍停在中间态”的工件混入主链

### `[main-agent-only]` `scripts/normalize_graph_review_result.py`

输出：

- 直接规范化 `output/graph_review_result.json`

用途：

- 仅服务 Step 5 `graph_path_analyst`
- 在正式 finalize 前对 `graph_review_result.json` 做轻量 lint / normalize
- 自动补齐少量尾部缺失的 `}` / `]`
- 把 `artifact_promotion.graph_span_candidates_payload`、`forward_segment_template_payload`、`graph_span_alignment_payload` 的 phase-keyed / `items` 结构统一改写为 `{"status": ..., "row_count": N, "rows": [...]}` 包装
- 不再自动补齐 `artifact_promotion.graph_execution_plan_updates` 与 `graph_forward_context_updates` 的关键 `status`；缺失时应由 finalize 直接拒绝，要求 agent 重写
- 禁止伪造分析结论；不会自动补写 `graph_operator_span_id`、不会修改 `location_kind`、不会清空真实 `blocking_issues` / `contradictions`

Step 4 对 `stack_mapper` 的额外行为：

- 校验 `output/stack_mapping_result.json` 的正式 payload
- 强制检查 `external_span_mapping_payload.rows[*].span_id` 必须属于 `external_mapping_targets.json`
- 严格检查 `evidence_inputs`、`primary_file_function`、`file_function_candidates` 与 `code_line_candidates` 的结构一致性
- 结合 `quality_signals` 与正式 row 统计，拦截异常塌缩到 `scheduler` / `worker` / `schedule_batch` / `prefill_delayer` / `speculative` 等协调层入口的 Step 4 结果
- 对 `semantic_class=communication` 且缺少实现层 repo frame 的 span，强制检查其是否按规则降级，而不是继续伪装成高质量精确 code line
- 将批准的结果提升回：
  - `artifacts/mapping/external_span_mapping.json`

Step 5 对 `graph_path_analyst` 的额外行为：

- 在 `json.loads()` 前允许先调用 `scripts/normalize_graph_review_result.py`，处理纯结构层问题，避免主链反复卡在可机械修复的 rows/status/闭合符问题
- 校验 `output/graph_review_result.json` 的 review schema
- 明确 Step 5 的 `semantic skeleton = graph_mapping_targets.json.rows[*].span_id`
- 明确 Step 5 的 `operator skeleton = graph_operator_spans.json.rows[*].graph_operator_span_id`
- 对 `artifact_promotion.graph_execution_plan_updates` / `graph_forward_context_updates` 强制检查 `status`
- 对 `artifact_promotion.graph_span_candidates_payload` / `forward_segment_template_payload` / `graph_span_alignment_payload` 强制检查 `rows` 包装
- 强制检查 `graph_operator_spans.json.rows[*].span_id` 必须全部属于 `graph_mapping_targets.json.rows[*].span_id`
- 强制检查 `graph_span_candidates_payload.rows[*].span_id` 只能来自 `graph_mapping_targets.json` 已冻结 formal graph target 范围
- 若 `graph_execution_plan_updates.phase_windows[*].span_ids` 存在，也必须全部属于 `graph_mapping_targets.json` 已冻结 formal graph target 范围；只允许等价重排、去重或收窄，不允许扩写新 span
- 强制检查 `graph_span_alignment_payload.rows[*].graph_operator_span_id` 必须回溯到 `graph_operator_spans.json`，且 row 内 `span_id` 必须与对应 operator span 一致
- 强制检查 `graph_span_alignment_payload.rows[*].span_id` 只能来自 `graph_mapping_targets.json` 已冻结 formal graph target 范围
- 若 `graph_execution_plan_updates.identified_graph_span_ids` 存在，只允许等价重排、去重或收窄，不允许扩出 `graph_mapping_targets.json` 已冻结 formal graph target 范围
- 强制校验 `repo_file_evidence_check`，禁止 graph_path_analyst 在结论层自行猜测 repo 文件缺失
- `repo_file_evidence_check.contradictions` 只允许用于保留仍未消解的 repo 文件事实冲突；若只是上游任务输入之间的描述不一致，必须写入 `blocking_issues` / `review_summary` / `notes`
- 当 `status=passed` 时，额外拒绝任何 `line<=0` 或 `code_location` 以 `:0` 结尾的占位定位，防止未核实行号被提升回主链
- 当 `status=passed` 时，额外拒绝任何缺少 `graph_operator_span_id`、`location_kind`、`operator_evidence_kind`、`requires_further_drilldown` 的正式 graph span 记录；`graph_operator_span_id` 必须能回溯到 `graph_operator_spans.json`，`location_kind` 必须为 `operator_call`，`requires_further_drilldown` 必须为 `false`
- 当 `status=passed` 时，会再次读取 repo 源码行，拒绝把 `self.xxx(...)`、构造行或 `.replay()` 入口提升为正式 `operator_call`
- 当 `status=partial` 时，不再 promotion 正式 `graph_span_alignment.json`；只保留分析性 graph 工件，避免 Step6 消费半成品 formal mapping
- 当 `status=passed` 且 `review_outcome=approved` 时，将 `artifact_promotion` 中的批准更新安全提升回：
  - `artifacts/graph/graph_execution_plan.json`
  - `artifacts/graph/graph_forward_context.json`
  - `artifacts/graph/graph_operator_spans.json`
  - `artifacts/graph/graph_span_candidates.json`
  - `artifacts/graph/forward_segment_template.json`
  - `artifacts/graph/graph_span_alignment.json`
- 保持 Step 6 / Step 7 继续只消费 `artifacts/graph/*.json`
- `scripts/write_validation_outputs.py` 与 `scripts/check_final_gate.py` 会再次直接读取 `graph_operator_spans.json` 与 `graph_span_alignment.json`，用结构化粒度字段和 operator span 回溯关系校验 graph replay 是否真的到达最终 operator/device 调用行
- `artifact_validator` 的正式合同需要允许 `validation_result.json` 在失败时写出 `status=failed`，否则 Step 7 无法形成“正式失败但可审计”的收口结果
- 统一硬门禁：没有 `audit/subagent_completion_<agent>.json`，或 completion marker 与当前 dispatch 不匹配时，禁止 finalize；这一步用于阻止主 agent 跳过真实 Task(...) 返回直接自写输出再 finalize
- finalize 成功或失败都会写入 `audit/finalize_<agent>_<timestamp>.json`，并由 `prepare_agent_dispatch.py` / `mark_step_complete.py` 用于闭环校验
- `graph_path_analyst` 的任务与 query 还会显式要求 sequence-analysis：先构建 ordered operator span sequence，再识别 repetitive pattern 与 distinctive kernel anchors，然后把 sequence 证据映射回理论 forward 路径与逐 span 对齐；但这些 sequence 字段只增强推理，不会放宽最终 `operator_call` 门禁

### `[shared-library]` `scripts/validate_graph_candidate_graph.py`

输出：

- `<candidate_graph>.validation.json`

用途：

- 校验未来 graph 候选路径图工件的基础 schema 和节点/边数量
- 当前作为补充工具预留，不纳入既有主流程门禁

### `[subagent-only]` `scripts/map_spans_to_code.py`

输出：

- `artifacts/mapping/span_code_mapping.json`

用途：

- 汇总 graph 外 stack 证据与 graph 内对齐结果
- graph 外定位优先消费 `external_span_mapping.json`
- 对 graph span 优先消费 `graph_span_alignment.json`
- 避免 graph replay span 回落到 runtime 包装层代码位置
- 不再对 graph spans 执行 template expansion、phase hint fallback 或 graph code_location 清洗补洞

### `[subagent-only]` `scripts/annotate_trace_view.py`

输出：

- `output/trace_view.annotated.json`

### `[subagent-only]` `scripts/render_stream_span_timeline.py`

输出：

- `artifacts/timeline/stream_span_timeline.json`

## 3. agent 相关脚本

### `[main-agent-only]` `scripts/build_agent_query.py`

职责：

- 读取 prompt
- 读取子 agent 的唯一操作手册
- 拼装 PREAMBLE
- 写入 `input/query_<agent>.txt`
- 写入 query 快照
- 更新 `logs/agent_calls/index.jsonl`

### `[main-agent-only]` `scripts/prepare_agent_dispatch.py`

职责：

- 在主 agent 调用子 agent 之前做前置检查
- 生成 query 与调度说明
- 写入 `audit/dispatch_<agent>.json`
- 为本次 dispatch 生成 `dispatch_id` 与 `completion_marker_path`
- Step 4 dispatch 额外写入 `substep`
- 在 dispatch 中显式写入 `main_agent_role`、`subagent_role`、`allowed_official_scripts`、`task_required`、`task_receipt_required`
- step>1 时强制检查上一轮 finalize 审计闭环
- Step 4A 只为 `step4_bootstrap_runner` 生成 light dispatch；真正的 shared bootstrap 等待由子 agent 自己承担
- Step 4B 只校验 Step 4A 的 `step4_bootstrap_result.json` 和 ready set，不再托管 Step4 bootstrap wrapper
- Step 5A 只负责补齐 `graph_forward_context.json`、`graph_seed_context.json`、`graph_operator_spans.json`
- `repo_divergence_report.json` 与 `runtime_constraints.json` 仍属于 Step 4A shared freeze ready set，Step 5B 只消费，不再重新生成
- 回写当前激活的调度状态

### `[main-agent-only]` `scripts/record_subagent_completion.py`

职责：

- 在真实 `Task(...)` 返回后记录一次 completion marker
- 写入 `audit/subagent_completion_<agent>.json`
- 将 completion marker 和当前 dispatch 绑定，供 `finalize_agent_dispatch.py` 做硬校验
- 记录 `task_call_id/subagent_id`、`query_snapshot_sha256`、`allowed_official_scripts`、`substep`
- 阻止主 agent 省略子 agent 调用、直接伪造输出进入 finalize

### `[main-agent-only]` `scripts/finalize_agent_dispatch.py`

职责：

- 在子 agent 输出写回 workspace 之后做正式校验
- 强制校验当前 dispatch 对应的 completion marker 已存在且匹配
- 强制校验 `query_snapshot_sha256`、`task_call_id/subagent_id` 与 `allowed_official_scripts` 等 dispatch 契约字段
- 校验 JSON `status` 是否在允许集合中
- 写 finalize 审计记录并回写 `last_finalize_record_path`
- 回写 `state.agents.<agent>` 的正式状态

### 当前对应关系

- `profiling_preprocessor`
- `timeline_analyst`
- `step4_bootstrap_runner`
- `stack_mapper`
- `graph_path_analyst`
- `artifact_validator`
- `profiling_debugger`
- `artifact_renderer`

## 4. 门禁和回退脚本

### `[main-agent-only]` `scripts/check_final_gate.py`

硬校验：

- 最终三个核心工件是否存在
- Step 6/7 与 final gate 所需的 graph 相关正式工件是否齐全，包括 `graph_execution_plan.json`、`graph_forward_context.json`、`graph_mapping_targets.json`、`graph_operator_spans.json`、`graph_span_candidates.json`、`forward_segment_template.json`、`graph_span_alignment.json`
- `code_location` 是否合法
- 语义 span 是否都有映射
- 排除 span 是否没有 `code_location`
- `global_order` 是否有序
- `state.flags` 是否一致
- 所有已识别 graph spans 的正式 graph 场景是否达到 `per_span_forward_code`
- Step 7 已先做源码行级 graph 精度检查；final gate 仍会再次直接检查 graph row 是否残留 `module_call_anchor` / `constructor_line` / `replay` 入口等问题，确保最终收口结果未回退

### `[main-agent-only]` `scripts/post_error_check.py`

职责：

- 读取 `error_context.json`
- 读取 `fix_instructions.json`
- 控制重试次数
- 清理 prepare/finalize 残留的 `orchestration.active_*` 锁状态，确保可再次调度
- 若失败点是普通 step / finalize，则推进到 `ready_to_retry`
- 若失败点是 `final_gate`，则把状态回流到 `awaiting_final_gate + run_final_gate`，允许修复后直接重跑 final gate

## 5. 已移除的历史入口

历史上的脚本化单入口 orchestrator 已经从当前 skill 中移除，不再作为当前仓库中的可执行入口或文档依赖。

移除原因：

- 它会误导主 agent 走 script fallback 路径
- 它不是真实的 subagent 调度执行器
- 当前 skill 的正式执行面只保留 `SKILL.md + prepare_agent_dispatch.py + Task(...) + record_subagent_completion.py + finalize_agent_dispatch.py`

## 6. step 与脚本映射

### Step 1

- `pre_step_check.py`
- `prepare_agent_dispatch.py`
- `record_subagent_completion.py`
- `finalize_agent_dispatch.py`
- `mark_step_complete.py`

### Step 2

- `pre_step_check.py`
- `prepare_agent_dispatch.py`
- `record_subagent_completion.py`
- `finalize_agent_dispatch.py`
- `mark_step_complete.py`

### Step 3

- `pre_step_check.py`
- `prepare_agent_dispatch.py`
- `run_step3_analysis_pipeline.py`
- `merge_timeline_review_patch.py`
- `record_subagent_completion.py`
- `finalize_agent_dispatch.py`
- `mark_step_complete.py`

### Step 4

- `pre_step_check.py`
- `prepare_agent_dispatch.py --agent-name step4_bootstrap_runner`
- `record_subagent_completion.py --agent-name step4_bootstrap_runner`
- `finalize_agent_dispatch.py --agent-name step4_bootstrap_runner`
- `mark_step_complete.py --step 4 --substep A`
- `pre_step_check.py --step 4 --substep B`
- `prepare_agent_dispatch.py`
- `record_subagent_completion.py`
- `finalize_agent_dispatch.py`
- `mark_step_complete.py --step 4 --substep B`

### Step 5

- `pre_step_check.py --step 5 --substep A`
- `prepare_agent_dispatch.py --agent-name graph_bootstrap_runner`
- `record_subagent_completion.py --agent-name graph_bootstrap_runner`
- `finalize_agent_dispatch.py --agent-name graph_bootstrap_runner`
- `mark_step_complete.py --step 5 --substep A`
- `pre_step_check.py --step 5 --substep B`
- `prepare_agent_dispatch.py --agent-name graph_path_analyst`
- `record_subagent_completion.py`
- `normalize_graph_review_result.py`
- `finalize_agent_dispatch.py`
- `mark_step_complete.py --step 5 --substep B`

### Step 6

- `pre_step_check.py`
- `prepare_agent_dispatch.py`
- `record_subagent_completion.py`
- `finalize_agent_dispatch.py`
- `mark_step_complete.py`

### Step 7

- `pre_step_check.py`
- `prepare_agent_dispatch.py`
- `record_subagent_completion.py`
- `finalize_agent_dispatch.py`
- `mark_step_complete.py`
- `check_final_gate.py`

## 7. 当前门禁实际口径

当前 V2 的门禁分三层：

- `pre_step_check.py`
- `mark_step_complete.py`
- `check_final_gate.py`

并额外有：

- `post_error_check.py`

用于失败回退后的重试控制。
