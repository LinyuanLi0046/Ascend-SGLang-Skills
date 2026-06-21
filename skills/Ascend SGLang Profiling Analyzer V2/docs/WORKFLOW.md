# Workflow

## 1. 总体工作流

当前 V2 skill 的完整工作流分为 7 个阶段：

1. 输入发现与切片
2. timeline index 构建
3. 时序语义分析
4. graph 外 stack 映射
5. graph 内路径重建与对齐
6. 最终映射与交付物渲染
7. 验证与最终门禁

主 agent 正式编排入口：

- `SKILL.md`

## 2. 阶段 1 输入发现与切片

### 目标

把原始 profiling 变成后续流程可直接消费的切片工件。
其中 `trace_slice.json` 会保留窗口内非 `X` trace 事件用于兼容渲染与审计，但 Step 2 的正式 span 主索引不会消费这些非 `X` 事件。

### 输入

- `state.inputs.profiling_root_path`
- `state.inputs.window_start_ns`
- `state.inputs.window_end_ns`
- `state.inputs.code_repo_path`
- `state.inputs.model_root_path`
- `state.inputs.draft_model_root_path`（可选，由输入归一化自动补齐）
- `state.inputs.launch_command_file` 或 `state.inputs.launch_command_text`
- `state.inputs.supplemental_input_paths`（可选，用于承接用户 prompt 显式给出的补充目录/文件提示）

### 使用脚本

- `scripts/resolve_step1_inputs.py --workspace-dir <workspace>`
- `scripts/pre_step_check.py --step 1`
- `scripts/prepare_agent_dispatch.py --agent-name profiling_preprocessor`
- `scripts/record_subagent_completion.py --agent-name profiling_preprocessor`
- `scripts/finalize_agent_dispatch.py --agent-name profiling_preprocessor`
- `scripts/mark_step_complete.py --step 1`

### 输出

- `input/input_resolution.json`
- `input/input_contract.json`
- `input/source_inventory.json`
- `artifacts/slices/trace_slice.json`
- `artifacts/slices/kernel_details_slice.csv`
- `artifacts/slices/operator_details_slice.csv`
- `artifacts/slices/task_time_slice.csv`
- `artifacts/slices/op_summary_slice.csv`
- `artifacts/stacks/python_tracer_index.json`

### 完成标准

- `state.flags.input_contract_valid = true`
- `state.flags.raw_profiling_discovered = true`
- `state.flags.slicing_done = true`
- 六类切片/索引工件都存在
- `profiling_preprocessor` 状态满足 `passed`

## 3. 阶段 2 timeline index 构建

### 目标

构建统一的 stream/task/op/span 索引层。
当前正式口径下，`trace_spans` 只来源于 `trace_slice.json` 中的 `ph="X"` 且带 `ts/dur` 的事件；`C/s/f/i/I/M` 等事件只保留在 trace slice 中，不进入 timeline 主索引。
Step 2 还需要保证三件事的一致性：一是优先使用 `trace_slice.json` metadata 中声明的 trace 单位把 `ts/dur` 统一转换成 ns；二是用 `task_compound_id = stream_id::task_id` 聚合 task，避免跨 stream 的同名 `Task ID` 被错误合并；三是 `ops` 的时间边界与 `tasks` 共用同一套 `*_ns/*_us` 解析逻辑。

### 使用脚本

- `scripts/pre_step_check.py --step 2`
- `scripts/prepare_agent_dispatch.py --agent-name profiling_preprocessor`
- `scripts/record_subagent_completion.py --agent-name profiling_preprocessor`
- `scripts/finalize_agent_dispatch.py --agent-name profiling_preprocessor`
- `scripts/mark_step_complete.py --step 2`

### 输出

- `artifacts/index/timeline_index.json`

补充说明：

- `timeline_index.json` 需要显式记录 `trace_time_unit_policy` 与 `task_identity_policy`，方便后续步骤和验收脚本核对。
- stream 候选角色允许使用扩展通信关键词（如 `hccl`、`all_gather`、`reduceScatter`、`send/recv`、`collective`）做启发式分类，但该角色仍只是 Step 3 之前的候选视图。

### 完成标准

- `state.flags.timeline_index_built = true`
- `timeline_index.json` 可解析
- `streams/tasks/trace_spans` 非空
- `profiling_preprocessor` 状态满足 `passed`

## 4. 阶段 3 时序语义分析

### 目标

完成 span 级初版语义分类，并形成可供 `timeline-analyst` 消费的输入；Step 3 只保留粗语义、scope 与并行结构，不再输出 graph candidate / graph phase 判断。

补充口径：

- Step 3 的正式目标之一，是尽早排除 `NOTIFY_RECORD_SQE`、`NOTIFY_WAIT_SQE`、纯 `CAPTURE_` / `EVENT_` / `AscendCL@` / `Runtime@Event` 等控制类 span 对 semantic 集合的污染。
- 若仍有 `runtime_control` span 落入 semantic 集合，必须在 `scope_gate_result.json` 与 `timeline_analysis.json` 中显式暴露，不得等到 Step 7 才发现。

### 使用脚本

- `scripts/pre_step_check.py --step 3`
- `scripts/classify_spans.py`
- `scripts/prepare_agent_dispatch.py --agent-name timeline_analyst`
- `scripts/record_subagent_completion.py --agent-name timeline_analyst`
- `scripts/finalize_agent_dispatch.py --agent-name timeline_analyst`
- `scripts/mark_step_complete.py --step 3`

### 输出

- `artifacts/classification/classified_spans.json`
- `input/timeline_task.json`
- `output/scope_gate_result.json`
- `output/timeline_analysis.json`
- `output/timeline_analysis.md`
- `logs/agent_calls/*timeline_analyst*`

### 当前执行方式

当前正式推荐方式是：

- 主 agent 运行确定性脚本
- 通过 `prepare_agent_dispatch.py` 生成调度文件
- 主 agent 按调度文件真实调用子 agent
- 在子 agent 真正返回后先运行 `record_subagent_completion.py`
- 再通过 `finalize_agent_dispatch.py` 回写状态
- 若 `input/timeline_task.json` 缺失，`prepare_agent_dispatch.py` 会自动补齐
- `output/scope_gate_result.json` 由 `prepare_agent_dispatch.py --agent-name timeline_analyst` 在 dispatch 前自动补齐，作为 Step 3 的正式辅助输入与审计工件

- `state.flags.classification_done = true`
- `timeline_analysis.json` 存在
- `timeline_analyst` 的 query 审计文件存在

## 5. 阶段 4 graph 外 stack 映射

### 目标

利用 `Call Stack`、`python tracer` 与 timeline 证据，在 shared stage 已冻结的 `external_mapping_targets.json` 封闭集合内，为 graph 外且不排除的 span 生成正式代码定位结果。

补充口径：

- Step 4 graph 外定位分两步完成：先锁定 span 所处调用栈的 repo 内 `文件:函数`，再结合 span 语义、左右 span 与 `parallel_group` 在该函数内选最终代码行。
- Step 4 不再显式依赖 `family_classification` / `control_anchor` / `execution_anchor` / `device_anchor_candidates` 这一套旧启发式字段。

### 使用脚本

- `scripts/pre_step_check.py --step 4`
- `scripts/build_stack_evidence.py`
- `scripts/build_graph_phase_stack_evidence.py`
- `scripts/classify_graph_groups.py`
- `scripts/build_graph_mapping_targets.py`
- `scripts/build_external_mapping_targets.py`
- `scripts/build_stack_call_paths.py`
- `scripts/prepare_agent_dispatch.py --agent-name stack_mapper`
- `scripts/record_subagent_completion.py --agent-name stack_mapper`
- `scripts/finalize_agent_dispatch.py --agent-name stack_mapper`
- `scripts/mark_step_complete.py --step 4`

补充说明：

- 若 `input/stack_mapping_task.json` 缺失，`prepare_agent_dispatch.py` 会自动补齐
- `prepare_agent_dispatch.py --agent-name stack_mapper` 会在 dispatch 前补齐 shared deterministic freeze 链：`build_stack_evidence.py -> build_graph_phase_stack_evidence.py -> classify_graph_groups.py -> build_graph_mapping_targets.py -> build_external_mapping_targets.py -> build_stack_call_paths.py`
- `build_stack_evidence.py` 负责产出原始 stack 证据与 `MODEL_EXECUTE` phase marker 原始证据层
- `build_graph_phase_stack_evidence.py` 负责把 `stack_evidence.json` 中的 phase marker/replay 证据冻结成 shared graph phase 工件；该工件不再由 Step 4 agent 正式输出拥有
- `build_stack_call_paths.py` 负责直接产出 `artifacts/mapping/stack_call_paths.json`，并在原始 stack 证据上补齐 graph 外 formal targets 的 `文件:函数` 候选、代码行候选与实现层证据/风险提示；它会消费既有 `external_mapping_targets.json`，而不是自行冻结 target set
- `stack_mapper` 的正式输出必须满足 `references/contracts/stack_mapping_result.schema.json`；尤其是 `evidence_inputs`、`primary_file_function`、`file_function_candidates` 与 `external_span_mapping_payload.rows[*].span_id` 会被 Step 4 finalize 严格校验
- `stack_mapper` 可以读取 `classified_spans.json`、`timeline_index.json` 等大上下文，但这些上下文只能用于补证据和选线，不能把 `external_mapping_targets.json` 之外的新 span 拉回正式主链
- 对 `semantic_class=communication` 且缺少实现层 repo frame 的 span，Step 4 应优先降级为 `function_entry_fallback` 或 unresolved，不得把调度层函数包装成高质量精确 code line
- Step 4 finalize 会结合 `quality_signals` 与正式 row 统计，拦截异常塌缩到 `scheduler` / `worker` / `schedule_batch` / `prefill_delayer` / `speculative` 等协调层入口的结果

### 输出

- `artifacts/mapping/stack_evidence.json`
- `artifacts/mapping/stack_evidence_lite.json`
- `artifacts/mapping/stack_call_paths.json`
- `artifacts/mapping/external_mapping_targets.json`
- `artifacts/mapping/external_span_mapping.json`
- `artifacts/graph/graph_phase_stack_evidence.json`
- `input/stack_mapping_task.json`
- `output/stack_mapping_result.json`
- `output/stack_mapping_report.md`

### 完成标准

- `state.flags.stack_evidence_built = true`
- `stack_evidence.json` 存在
- `stack_evidence_lite.json` 存在
- `state.flags.stack_call_paths_built = true`
- `state.flags.external_span_mapping_built = true`
- `state.flags.graph_phase_stack_evidence_built = true`
- `external_span_mapping.json` 存在
- `graph_phase_stack_evidence.json` 存在

## 6. 阶段 5 graph 内路径重建与对齐

### 目标

先通过 shared deterministic freeze 识别当前有哪些 graph group，以及它们分别属于什么 phase，例如：

- 非投机 `decode` graph
- 投机 `verify`
- `draft_prefill`
- `draft_decode`

再由 `graph_path_analyst` 基于当前 repo、启动参数、模型 config、候选上下文与 profiling 证据完成：

- graph 内代码路径重建
- graph operator span 对齐
- `forward_segment_template` 生成
- `graph_span_alignment` 生成

### 使用脚本

- `scripts/pre_step_check.py --step 5`
- `scripts/prepare_agent_dispatch.py --agent-name graph_path_analyst`
- `scripts/record_subagent_completion.py --agent-name graph_path_analyst`
- `scripts/normalize_graph_review_result.py --workspace-dir <workspace>`
- `scripts/finalize_agent_dispatch.py --agent-name graph_path_analyst`
- `scripts/mark_step_complete.py --step 5`

补充说明：

- 若 `input/graph_path_task.json` 缺失，`prepare_agent_dispatch.py` 会自动补齐
- `input/launch_command.json`、`input/model_context.json` 由 `scripts/classify_graph_groups.py` 在阶段内生成，不属于阶段开始前的 pre-step 依赖
- `prepare_agent_dispatch.py --agent-name graph_path_analyst` 会先补齐 shared deterministic freeze 链，再补齐 `graph_forward_context.json`、`graph_seed_context.json` 与 `graph_operator_spans.json`
- `scripts/classify_graph_groups.py` 只负责 graph inventory 与 phase 分类，不负责 graph 内真实代码路径重建
- `scripts/classify_graph_groups.py` 会优先消费 `graph_phase_stack_evidence.json` 中的高/中置信度 Step 4 `MODEL_EXECUTE` phase marker 证据：verify 对应的 `MODEL_EXECUTE` 必须由 `npu_graph_runner.py::replay` 在同一 phase segment 内确认，后续 marker 再按时间顺序依次作为 `draft_prefill`、`draft_decode` 的开始；随后脚本会直接在 `timeline_index.json.trace_spans` 中寻找该 marker 结束后的第一个合法 `NOTIFY_WAIT` / `NOTIFY_WAIT_SQE` task，并用该 task 的结束收敛 phase window 右边界。若证据不足或 NOTIFY_WAIT 数据源异常，会直接报错，而不是再回退到 Step 3 phase hint、时间三等分或 group span fallback
- `scripts/build_graph_mapping_targets.py` 会在保留 `graph_execution_plan.json` 的 graph inventory / phase windows / graph groups 语义前提下，冻结 `artifacts/graph/graph_mapping_targets.json`，作为 Step 5 唯一允许正式输出 `span_id` 的 formal graph target set
- `scripts/build_graph_forward_context.py` 直接产出 `artifacts/graph/graph_forward_context.json`，并在脚本内部调用 `build_graph_seed_context_for_workspace()` 生成 `input/graph_seed_context.json`
- `scripts/build_graph_operator_spans.py` 会只围绕 `graph_mapping_targets.json` 中已冻结的 formal graph targets，生成一一对应的 `artifacts/graph/graph_operator_spans.json`
- `prepare_agent_dispatch.py --agent-name graph_path_analyst` 会在 dispatch 前再次校验并补齐 `input/graph_seed_context.json`，确保 Step 5 合同输入完整
- `graph_path_analyst` 在真正开始 graph 内路径下钻前，必须先读 `references/knowledge/model_config_and_launch_fields.md`、`references/knowledge/sglang_path_map.md`、`references/knowledge/forward_analysis_rules.md`；最终仍以当前仓库代码和 profiling 证据为准，并在输出中显式给出 knowledge 阅读记录与 rules 符合性检查结果
- `artifacts/repo/repo_divergence_report.json`、`input/runtime_constraints.json`、`input/graph_seed_context.json` 是 Step 5 的补充工件，用于路径重建与知识适用性判断；当前不会改变既有 Step 5 完成标准
- `references/knowledge/*.md` 当前作为预留参考知识层；即使存在，也不能替代当前仓库代码事实
- `graph_path_analyst` 负责基于当前 repo 完成路径重建，并通过 `artifact_promotion` 生成最终 `graph_span_candidates.json`、`forward_segment_template.json` 与 `graph_span_alignment.json`；这些结果必须收敛在 `graph_execution_plan.json` 提供的 graph inventory、`graph_mapping_targets.json` 提供的 formal graph target set 与 `graph_operator_spans.json` 提供的 operator skeleton 范围内，且 graph alignment 条目必须显式绑定 `graph_operator_span_id`
- `graph_path_analyst` 的正式输出必须满足 `references/contracts/graph_review_result.schema.json`；`artifact_promotion.*` 的 rows 包装、`status` 字段和 `repo_file_evidence_check.contradictions` 的语义都会被 Step 5 finalize 严格校验
- `record_subagent_completion.py` 之后、`finalize_agent_dispatch.py` 之前，会先运行 `scripts/normalize_graph_review_result.py` 对 `output/graph_review_result.json` 做轻量 lint / normalize，自动处理尾部闭合符缺失、rows 包装与 `status` 补齐等纯结构问题

### 输出

- `input/launch_command.json`
- `input/model_context.json`
- `input/graph_path_task.json`
- `artifacts/repo/repo_divergence_report.json`
- `input/runtime_constraints.json`
- `input/graph_seed_context.json`
- `artifacts/graph/graph_phase_stack_evidence.json`
- `artifacts/graph/graph_execution_plan.json`
- `artifacts/graph/graph_mapping_targets.json`
- `artifacts/graph/graph_forward_context.json`
- `artifacts/graph/graph_operator_spans.json`
- `artifacts/graph/graph_span_candidates.json`
- `artifacts/graph/forward_segment_template.json`
- `artifacts/graph/graph_span_alignment.json`
- `output/graph_review_result.json`
- `output/graph_path_report.md`

### 完成标准

- `state.flags.graph_path_built = true`
- `state.flags.graph_forward_context_built = true`
- `state.flags.graph_span_identified = true`
- `state.flags.forward_segment_template_built = true`
- `state.flags.graph_span_alignment_built = true`
- `graph_execution_plan.json` 存在
- `graph_mapping_targets.json` 存在
- `graph_forward_context.json` 存在
- `graph_operator_spans.json` 存在
- `graph_span_candidates.json` 存在
- `forward_segment_template.json` 存在
- `graph_span_alignment.json` 存在
- `finalize_agent_dispatch.py` 会在 `graph_path_analyst` 输出 `graph_review_result.json` 后，将批准的 review 结果安全提升回 `artifacts/graph/*.json`
- `status=partial` 仍是合法正式审计状态，但不再允许 `mark_step_complete.py --step 5` 推进到 Step 6；它必须留下完整、可审计的 promoted graph artifacts，并显式列出 `blocking_issues`
- 只有当 Step 5 promoted artifacts 已满足 Step 6 readiness gate，即 `graph_span_alignment.json` 全量覆盖 formal graph targets、每条都具备可回溯 `graph_operator_span_id`、`location_kind=operator_call`、`operator_evidence_kind` 合法且 `requires_further_drilldown=false` 时，Step 5 才允许被 mark complete 并进入 Step 6
- 对于 `status=passed` 的 Step 5 结果，`graph_span_alignment.json` 中每条正式 graph span 还必须显式包含 `graph_operator_span_id`、`location_kind`、`operator_evidence_kind`、`requires_further_drilldown`
- `repo_file_evidence_check.contradictions` 只允许保留仍未消解的 repo 文件事实冲突；若只是上游 task/seed/plan 输入之间的描述不一致，应写入 `blocking_issues`、`review_summary` 或 `notes`
- Step 6、Step 7 与 final gate 会直接复核 `graph_operator_spans.json` 与 `graph_span_alignment.json`，只有 `graph_operator_span_id` 可回溯、`location_kind=operator_call`、`operator_evidence_kind` 非空且 `requires_further_drilldown=false` 才视为真正达到 `per_span_forward_code`

## 7. 阶段 6 最终映射与交付物渲染

### 目标

生成唯一映射汇总层，并产出两个正式交付物。

### 使用脚本

- `scripts/pre_step_check.py --step 6`
- `scripts/prepare_agent_dispatch.py --agent-name artifact_renderer`
- `scripts/record_subagent_completion.py --agent-name artifact_renderer`
- `scripts/finalize_agent_dispatch.py --agent-name artifact_renderer`
- `scripts/mark_step_complete.py --step 6`

### 输出

- `artifacts/mapping/span_code_mapping.json`
- `output/trace_view.annotated.json`
- `artifacts/timeline/stream_span_timeline.json`
- `output/render_result.json`
- `output/render_report.md`

### 完成标准

- `state.flags.span_mapping_done = true`
- `state.flags.annotated_trace_generated = true`
- `state.flags.timeline_generated = true`
- Step 6 wrapper 成功消费 `graph_mapping_targets.json`、`graph_operator_spans.json` 与 `graph_span_alignment.json`
- `artifact_renderer` 状态满足 `passed`

## 8. 阶段 7 验证与最终门禁

### 目标

验证正式交付物，并通过结构化 final gate。

### 使用脚本

- `scripts/pre_step_check.py --step 7`
- `scripts/prepare_agent_dispatch.py --agent-name artifact_validator`
- `scripts/record_subagent_completion.py --agent-name artifact_validator`
- `scripts/finalize_agent_dispatch.py --agent-name artifact_validator`
- `scripts/mark_step_complete.py --step 7`
- `scripts/check_final_gate.py`

补充说明：

- 若 `input/validation_task.json` 缺失，`prepare_agent_dispatch.py` 会自动补齐
- Step 7 的正式输入除验证三件套外，还必须包含 `graph_execution_plan.json`、`graph_forward_context.json`、`graph_mapping_targets.json`、`graph_operator_spans.json` 与 `graph_span_alignment.json`
- `artifact_validator` 允许正式输出 `status=failed` 作为可审计结果；真正的最终阻塞统一由 `check_final_gate.py` 收口

### 输出

- `input/validation_task.json`
- `output/validation_result.json`
- `output/validation_report.md`

### 完成标准

- `output/validation_result.json` 已生成，且 `status` 满足 `passed/failed`
- `state.flags.validation_passed` 与 `validation_result.json.status` 保持一致
- `mark_step_complete.py --step 7` 执行后进入 `state.status = awaiting_final_gate`
- 最终是否进入 `completed` 由 `scripts/check_final_gate.py` 统一收口

## 9. 失败回退流

任意脚本或门禁失败后，统一进入：

1. `input/error_context.json`
2. `scripts/prepare_agent_dispatch.py --agent-name profiling_debugger`
3. `Task(...)`
4. `scripts/record_subagent_completion.py --agent-name profiling_debugger`
5. `scripts/finalize_agent_dispatch.py --agent-name profiling_debugger`
6. `output/fix_instructions.json`
7. `scripts/post_error_check.py`

### 相关脚本

- `scripts/prepare_agent_dispatch.py`
- `scripts/record_subagent_completion.py`
- `scripts/finalize_agent_dispatch.py`
- `scripts/post_error_check.py`

### 完成标准

- 若失败点是普通 step / finalize：
  - `state.status = ready_to_retry`
  - `state.next_action = retry_<failed_step>`
- 若失败点是 `final_gate`：
  - `state.status = awaiting_final_gate`
  - `state.next_action = run_final_gate`
  - 修复完成后直接重新运行 `scripts/check_final_gate.py`

## 10. 当前正式入口与历史归档

当前唯一正式入口：

- `SKILL.md`

历史上的脚本化单入口 orchestrator 已废弃，不再属于当前 skill 的正式执行面，主 agent 禁止用它替代真实的多 agent 调度。

## 11. 当前 graph replay 精度口径

- `graph_execution_plan.json` 当前先由脚本生成 graph inventory / phase skeleton，再由 `graph_path_analyst` promotion 到更高精度
- `graph_forward_context.json` 当前只承载候选上下文，不在脚本内宣称真实执行路径
- `graph_span_candidates.json`、`forward_segment_template.json`、`graph_span_alignment.json` 当前由 `graph_path_analyst` 基于路径重建结果生成
- `map_spans_to_code.py` 会优先消费 `graph_span_alignment.json`，避免 graph span 回落到 runtime 包装层代码
- final gate 仍要求：凡是识别出 graph spans 的正式 graph 场景，包括 `spec_v2` 与 `decode_graph`，都必须达到 `per_span_forward_code`；若 `graph_path_analyst` 未给出足够证据，则不会被判定为最终通过
