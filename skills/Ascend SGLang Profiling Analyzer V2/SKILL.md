# Ascend SGLang Profiling Analyzer V2

本文件是主 agent 的唯一操作手册。

主 agent 不要把 `docs/` 与 `references/` 中的分散文档并列当成入口；这些文件只作为本手册在不同步骤按需引用的附录。

## 1. 目标

在给定 ns 时间窗口内，对 Ascend SGLang profiling 做分阶段分析，并生成两个正式交付物：

1. `output/trace_view.annotated.json`
2. `artifacts/timeline/stream_span_timeline.json`

## 2. 文档组织规则

为了避免主 agent 和子 agent 读到分散入口导致幻觉，当前文档结构按以下原则执行：

- 主 agent 唯一入口：`SKILL.md`
- 每个子 agent 唯一入口：`references/agents/<agent>.md`
- `docs/*.md`：给人读的总览、目录、验收说明，不作为 agent 运行时主入口
- `references/shared/*`、`references/knowledge/*`：附录规则，只能由主入口或子 agent 手册按需引用

## 3. 子 agent 清单

- `timeline_analyst`
- `stack_mapper`
- `graph_path_analyst`
- `artifact_validator`
- `profiling_debugger`
- `profiling_preprocessor`
- `artifact_renderer`

主 agent 必须使用：

- `scripts/prepare_agent_dispatch.py`
- `scripts/finalize_agent_dispatch.py`

来完成正式调度，而不是直接凭印象拼装 query 或跳过输出校验。

## 4. 主 agent 硬规则

- 所有中间产物都写在 workspace 内。
- 每个 step 开始前必须先跑 `scripts/pre_step_check.py`。
- 需要子 agent 的步骤，必须先运行 `prepare_agent_dispatch.py`，再调用 `Task(...)`，在 Task 返回后立即运行 `record_subagent_completion.py`，最后才能运行 `finalize_agent_dispatch.py`。
- 每个 step 完成后必须运行 `scripts/mark_step_complete.py`。
- Step 7 之后必须运行 `scripts/check_final_gate.py`。
- 任意脚本失败时，要走 `profiling_debugger` 回退流，不能直接凭猜测修。
- `prepare_agent_dispatch.py` 成功后，下一动作必须是对应的 `Task(...)` 调用；禁止跳过真实子 agent 直接进入 `finalize_agent_dispatch.py`。
- `finalize_agent_dispatch.py` 现在会强制检查 `audit/subagent_completion_<agent>.json`；没有 completion marker，说明没有完成正式子 agent 返回闭环，禁止 finalize。
- `prepare_agent_dispatch.py` 在 step>1 时会强制检查上一轮 `finalize` 是否已经闭环，并要求存在 `orchestration.last_finalize_agent` 与 `orchestration.last_finalize_record_path`；缺少任一项都禁止继续 prepare。
- `finalize_agent_dispatch.py` 会为每次正式 finalize 写入 `audit/finalize_<agent>_<timestamp>.json` 审计记录，并回写 `last_finalize_record_path`；`mark_step_complete.py` 会继续校验该审计文件是否存在且与当前 step 对应。
- 禁止主 agent 在 workspace 中自行生成任何 `script_fallback`、`mock`、`synthetic`、`manual_stub` 类型的子 agent 正式输出，来替代真实 `Task(...)`。
- 禁止主 agent 仅运行 `build_agent_query.py` 就自认为完成了子 agent 调度；正式调度必须以 `audit/dispatch_<agent>.json` 为准。

## 5. Agent 调用硬模板

每次调用子 agent 之前，主 agent 必须按以下顺序执行：

1. 运行 `scripts/prepare_agent_dispatch.py --agent-name <agent>`
2. 读取 `audit/dispatch_<agent>.json`
3. 原样使用 dispatch 中的 `subagent_type`、`description`、`query_text` 发起 `Task(...)`
4. 等待子 agent 按合同把正式输出写回 workspace
5. 运行 `scripts/record_subagent_completion.py --agent-name <agent>`
6. 运行 `scripts/finalize_agent_dispatch.py --agent-name <agent>`

标准调用模板：

```text
=== Agent调用准备 ===
调用目标: <agent_name> (<dispatch.subagent_type>)
输入文件: <dispatch.input_files>
预期输出: <dispatch.output_files>
当前步骤: <dispatch.step>
=== 开始调用 <agent_name> ===

Task(
  subagent_type=<dispatch.subagent_type>,
  description=<dispatch.description>,
  query=<dispatch.query_text>,
  response_language="中文"
)
```

Task 返回后、进入 finalize 之前，主 agent 必须执行：

```text
python scripts/record_subagent_completion.py --workspace-dir <workspace> --agent-name <agent_name>
```

## 6. 主 agent 标准流程

### Step 0 初始化 workspace

运行：

- `scripts/init_session.py`

目的：

- 创建 workspace 目录
- 初始化 `state.json`
- 初始化 `task_plan.md`、`findings.md`、`progress.md`

### Step 1 输入发现与切片

顺序运行：

- `scripts/resolve_step1_inputs.py --workspace-dir <workspace>`
- `scripts/discover_inputs.py --workspace-dir <workspace>`
- `scripts/pre_step_check.py --step 1`
- `scripts/prepare_agent_dispatch.py --agent-name profiling_preprocessor`

随后主 agent 必须：

1. 读取 `audit/dispatch_profiling_preprocessor.json`
2. 原样执行：

```text
Task(
  subagent_type=<dispatch.subagent_type>,
  description=<dispatch.description>,
  query=<dispatch.query_text>,
  response_language="中文"
)
```

子 agent 返回并写完 Step 1 正式结果后，继续运行：

- `scripts/record_subagent_completion.py --agent-name profiling_preprocessor`
- `scripts/finalize_agent_dispatch.py --agent-name profiling_preprocessor`
- `scripts/mark_step_complete.py --step 1`

关键说明：

- `resolve_step1_inputs.py` 是 Step 1 前的正式输入归一化脚本。若用户 prompt 显式给出“启动参数/模型 config/时间窗口获取目录”，主 agent 必须先把这些路径写入 `state.inputs.supplemental_input_paths`，再运行该脚本。
- 若用户没有显式指定 `model_root_path`，但 `launch_command_file`/`launch_command_text` 可解析到 `--model-path`，`resolve_step1_inputs.py` 允许基于该路径名在 `supplemental_input_paths` 或 launch 文件所在目录中回填本地模型目录；这是通用回填规则，不针对任何测试目录硬编码。
- `discover_inputs.py` 是 Step 1 正式调度前的 bootstrap，不属于子 agent 的 Step 1 白名单脚本。
- `profiling_preprocessor` 是脚本型子 agent，负责运行 Step 1 白名单切片脚本。
- Step 1 子 agent 结束前必须运行 `scripts/write_preprocess_step1_outputs.py --workspace-dir <workspace>`，统一生成正式结果并回写 `state.flags.slicing_done=true`。
- Step 1 的 `trace_slice.json` 仍保留窗口内非 `X` trace 事件用于兼容渲染与审计；但这些事件不进入 Step 2 的正式 span 主索引。
- `slice_trace_workspace.py` 已支持大 `trace_view.json` 的低内存流式切片。
- 子 agent 唯一入口：`references/agents/profiling_preprocessor.md`

### Step 2 建立统一时间索引

顺序运行：

- `scripts/pre_step_check.py --step 2`
- `scripts/prepare_agent_dispatch.py --agent-name profiling_preprocessor`

关键说明：

- Step 2 复用同一个 `profiling_preprocessor`，但输入输出与 Step 1 不同。
- 主 agent 必须按 `Current Step: 2` 的 dispatch 内容再次调用该子 agent。
- 主 agent 禁止跳过真实 `Task(...)` 直接 finalize。
- Step 2 的 `timeline_index.json` 当前正式只把 `ph="X"` 且带 `ts/dur` 的 trace 事件写入 `trace_spans`；`C/s/f/i/I/M` 等非 `X` 事件只保留在 `trace_slice.json` 中，不进入主索引。
- Step 2 必须优先依据 `trace_slice.json` metadata 中的 trace 单位把 `X.ts/dur` 统一转换到 ns；若 metadata 缺失，再按 `us` 默认值回退。
- Step 2 的 task 聚合需要使用复合键 `task_compound_id = stream_id::task_id` 避免跨 stream 的同名 `Task ID` 被错误合并，同时保留原始 `task_id` 供后续阶段展示与回溯。
- Step 2 的 `ops` 时间边界必须复用与 `tasks` 相同的统一时间解析逻辑，避免只存在 `*_us` 字段时退化成 0。
- Step 2 的 stream 通信角色判断允许使用扩展关键词（如 `hccl`、`all_gather`、`reduceScatter`、`send/recv`、`collective`）做启发式分类，但仍只作为候选角色，不是最终语义裁决。
- Step 2 子 agent 结束前必须运行 `scripts/write_preprocess_step2_outputs.py --workspace-dir <workspace>`，统一生成正式结果。
- 子 agent 唯一入口：`references/agents/profiling_preprocessor.md`

主 agent 对 Step 2 的子 agent 调用也必须原样执行标准 `Task(...)` 模板。

子 agent 返回并写完 Step 2 正式结果后，再运行：

- `scripts/record_subagent_completion.py --agent-name profiling_preprocessor`
- `scripts/finalize_agent_dispatch.py --agent-name profiling_preprocessor`
- `scripts/mark_step_complete.py --step 2`

### Step 3 时序语义分析

顺序运行：

- `scripts/pre_step_check.py --step 3`
- `scripts/classify_spans.py`
- `scripts/prepare_agent_dispatch.py --agent-name timeline_analyst`

关键说明：

- 若 `input/timeline_task.json` 缺失，`prepare_agent_dispatch.py` 会在正式 dispatch 前自动生成。
- `output/scope_gate_result.json` 由 `prepare_agent_dispatch.py --agent-name timeline_analyst` 在正式 dispatch 前通过 `check_scope_gate.py` 自动补齐；它是 Step 3 的正式辅助输入与审计工件，但不是 `timeline_analyst` 自己写出的正式输出。
- Step 3 只负责 span 粗语义、hardware scope、排除标记与并行结构；不再输出 graph candidate / graph phase 判断。
- Step 3 必须尽早排除 `NOTIFY_RECORD_SQE`、`NOTIFY_WAIT_SQE`、纯 `CAPTURE_` / `EVENT_` / `AscendCL@` / `Runtime@Event` 等控制类 span 对 semantic 集合的污染；若仍有 `runtime_control` span 落入 semantic 集合，必须在 `scope_gate_result.json` 中显式暴露。

随后主 agent 必须：

1. 读取 `audit/dispatch_timeline_analyst.json`
2. 原样执行标准 `Task(...)` 模板

子 agent 返回并写完输出后，继续运行：

- `scripts/record_subagent_completion.py --agent-name timeline_analyst`
- `scripts/finalize_agent_dispatch.py --agent-name timeline_analyst`
- `scripts/mark_step_complete.py --step 3`

子 agent 唯一入口：

- `references/agents/timeline_analyst.md`

### Step 4 graph 外 stack 映射

顺序运行：

- `scripts/pre_step_check.py --step 4`
- `scripts/build_stack_evidence.py`
- `scripts/build_graph_phase_stack_evidence.py`
- `scripts/classify_graph_groups.py`
- `scripts/build_graph_mapping_targets.py`
- `scripts/build_external_mapping_targets.py`
- `scripts/build_stack_call_paths.py`
- `scripts/prepare_agent_dispatch.py --agent-name stack_mapper`

关键说明：

- 若 `input/stack_mapping_task.json` 缺失，`prepare_agent_dispatch.py` 会在正式 dispatch 前自动生成。
- `scripts/build_stack_evidence.py` 负责构建原始 stack 证据与 `MODEL_EXECUTE` phase marker 原始证据层。
- `scripts/build_graph_phase_stack_evidence.py -> classify_graph_groups.py -> build_graph_mapping_targets.py -> build_external_mapping_targets.py` 构成 Step 4/5 共用的 shared deterministic freeze 链。
- `prepare_agent_dispatch.py --agent-name stack_mapper` 会先补齐 shared deterministic freeze 链：`build_stack_evidence.py -> build_graph_phase_stack_evidence.py -> classify_graph_groups.py -> build_graph_mapping_targets.py -> build_external_mapping_targets.py -> build_stack_call_paths.py`。
- `scripts/build_stack_call_paths.py` 负责在原始 stack 证据上补齐 graph 外 formal targets 的 `文件:函数` 候选、代码行候选与实现层证据；`stack_call_paths.json` 由该脚本直接产出，但它只消费既有 `external_mapping_targets.json`，因此必须位于 `build_external_mapping_targets.py` 之后。
- `stack_mapper` 现在只需要产出一类正式 payload：graph 外逐 span 定位结果。
- `stack_mapper` 的正式 JSON 必须满足 `references/contracts/stack_mapping_result.schema.json`；尤其是 `evidence_inputs`、`external_span_mapping_payload.rows[*].primary_file_function`、`file_function_candidates` 与 `external_span_mapping_payload.rows[*].span_id` 会被 `finalize_agent_dispatch.py` 严格校验。
- `scripts/finalize_agent_dispatch.py --agent-name stack_mapper` 会把批准的 Step 4 正式结果提升回：
  - `artifacts/mapping/external_span_mapping.json`

随后主 agent：

1. 读取 `audit/dispatch_stack_mapper.json`
2. 原样执行标准 `Task(...)` 模板

子 agent 返回后运行：

- `scripts/record_subagent_completion.py --agent-name stack_mapper`
- `scripts/finalize_agent_dispatch.py --agent-name stack_mapper`
- `scripts/mark_step_complete.py --step 4`

子 agent 唯一入口：

- `references/agents/stack_mapper.md`

### Step 5 graph 内路径重建与对齐

顺序运行：

- `scripts/pre_step_check.py --step 5`
- `scripts/classify_graph_groups.py`
- `scripts/build_graph_mapping_targets.py`
- `scripts/build_graph_forward_context.py`
- `scripts/build_graph_operator_spans.py`
- `scripts/prepare_agent_dispatch.py --agent-name graph_path_analyst`

关键说明：

- 若 `input/graph_path_task.json` 缺失，`prepare_agent_dispatch.py` 会在正式 dispatch 前自动生成。
- `input/launch_command.json`、`input/model_context.json` 由 `scripts/classify_graph_groups.py` 在 Step 5 内生成，不属于 Step 5 开始前必须已存在的前置输入。
- `scripts/classify_graph_groups.py` 当前负责 graph inventory 与 phase 分类，并补充生成 `artifacts/repo/repo_divergence_report.json` 与 `input/runtime_constraints.json`。
- `scripts/classify_graph_groups.py` 在 phase 分组时会优先参考 `artifacts/graph/graph_phase_stack_evidence.json` 的 `MODEL_EXECUTE` phase markers：verify 对应的 marker 必须由 `npu_graph_runner.py::replay` 确认，后续 marker 再按时间顺序依次作为 `draft_prefill`、`draft_decode` 的开始；随后脚本会直接在 `artifacts/index/timeline_index.json.trace_spans` 中寻找该 marker 结束后的第一个合法 `NOTIFY_WAIT` / `NOTIFY_WAIT_SQE` task，并用该 task 的结束收敛 graph window 右边界。若缺少可信的后继 wait task，才视为 NOTIFY_WAIT 数据源异常并直接报错，而不是再回退 Step 3 phase hint、时间三等分或 group span fallback。
- `scripts/build_graph_mapping_targets.py` 会在保留 `graph_execution_plan.json` 的 graph inventory / phase windows / graph groups 语义前提下，冻结 `artifacts/graph/graph_mapping_targets.json`，作为 Step 5 唯一允许正式输出 `span_id` 的 formal graph target set。
- `scripts/build_graph_forward_context.py` 直接产出 `artifacts/graph/graph_forward_context.json`，并在脚本内部调用 `build_graph_seed_context_for_workspace()` 生成 `input/graph_seed_context.json`；`prepare_agent_dispatch.py --agent-name graph_path_analyst` 也会在 dispatch 前再次校验并补齐该工件，确保 Step 5 合同输入完整。
- `scripts/build_graph_forward_context.py` 还会把 `repo_divergence_report.json` 与实际 repo exists 扫描收敛成 `repo_file_existence_facts`；Step 5 文件存在性判断只能引用这类正式事实源或当前 repo 实际文件，不得自行猜测文件缺失。
- `scripts/build_graph_operator_spans.py` 会只围绕 `graph_mapping_targets.json` 中已冻结的 formal graph targets，生成一一对应的 `artifacts/graph/graph_operator_spans.json`；Step 5 后续 graph code alignment 必须以这些 operator spans 为正式对齐对象，禁止再把 `MODEL_EXECUTE` marker、`replay()` 入口或 phase window 本身当成最终 graph span。
- `graph_path_analyst` 在真正开始 graph 内路径下钻前，必须先读 `references/knowledge/model_config_and_launch_fields.md`、`references/knowledge/sglang_path_map.md`、`references/knowledge/forward_analysis_rules.md`，并在正式输出中显式记录 knowledge 阅读情况与 `forward_analysis_rules.md` 的规则符合性检查；若知识与当前仓库冲突，仍必须以当前仓库代码和 profiling 证据为准。
- `references/knowledge/*.md` 当前允许为空白占位文件；它们只能作为参考地图和规则索引，不能替代当前仓库代码事实。
- `graph_path_analyst` 当前负责两件事：基于当前 repo + 参考文档 + 启动参数 + 模型 config 做 graph 内路径重建；再基于 `graph_execution_plan.json` 提供的 graph inventory、`graph_mapping_targets.json` 提供的 formal graph target set 与 `graph_operator_spans.json` 提供的 operator skeleton，生成 `graph_span_candidates.json`、`forward_segment_template.json` 与 `graph_span_alignment.json`。
- `graph_path_analyst` 的正式 JSON 必须满足 `references/contracts/graph_review_result.schema.json`；尤其是 `artifact_promotion.*` 的 `rows` 包装、`status` 字段、`graph_span_alignment_payload` 的逐 span 结构化字段，以及 `repo_file_evidence_check.contradictions` 的语义都会被 `finalize_agent_dispatch.py` 严格校验。

随后主 agent：

1. 读取 `audit/dispatch_graph_path_analyst.json`
2. 原样执行标准 `Task(...)` 模板

子 agent 返回后运行：

- `scripts/record_subagent_completion.py --agent-name graph_path_analyst`
- `scripts/normalize_graph_review_result.py --workspace-dir <workspace>`
- `scripts/finalize_agent_dispatch.py --agent-name graph_path_analyst`
- `scripts/mark_step_complete.py --step 5`

补充说明：

- `graph_path_analyst` 的正式 JSON 输出已收敛为 `output/graph_review_result.json`；其中同时承载路径重建结果、逐 span 对齐结果以及可提升回主链的 `artifact_promotion`。
- `scripts/normalize_graph_review_result.py` 只允许做 Step 5 的轻量 lint / normalize：自动补齐少量尾部闭合符、补全 `artifact_promotion.*.status`、把 phase-keyed / items 结构统一转成 `rows` 包装；它禁止伪造事实结论、补写缺失的 `graph_operator_span_id` 或清空真实冲突。
- `scripts/finalize_agent_dispatch.py --agent-name graph_path_analyst` 会在审阅通过时，将批准的 review 更新安全提升回 `artifacts/graph/graph_execution_plan.json`、`graph_forward_context.json`、`graph_span_candidates.json`、`forward_segment_template.json` 与 `graph_span_alignment.json`，因此 Step 6/7 仍只消费 `artifacts/graph/*.json`。
- `scripts/finalize_agent_dispatch.py --agent-name graph_path_analyst` 会保留并校验 `graph_operator_spans.json`，并要求 `graph_span_alignment` 中的每条正式 graph span 记录都能通过 `graph_operator_span_id` 回溯到该文件中的正式 operator span。
- 若 `graph_path_analyst` 输出 `status=passed`，则 `graph_span_alignment` 的正式 graph span 记录必须显式携带 `graph_operator_span_id`、`location_kind`、`operator_evidence_kind`、`requires_further_drilldown`；只有 `graph_operator_span_id` 可回溯、`location_kind=operator_call`、`operator_evidence_kind` 合法且 `requires_further_drilldown=false` 才允许提升回主链。
- `graph_path_analyst` 的正式 JSON 输出现在还必须包含 `repo_file_evidence_check`；`scripts/finalize_agent_dispatch.py` 会拒绝任何与 `repo_divergence_report.json` 或 repo 实际存在性冲突的文件存在性结论，也会拒绝把 `self.xxx(...)`、构造行或 `.replay()` 入口提升为最终 `operator_call`。`repo_file_evidence_check.contradictions` 只用于保留“仍未消解的 repo 文件事实冲突”，不能拿来记录上游输入之间的描述不一致。
- 若 `graph_path_analyst` 输出 `status=partial`，也不能只是轻量兜底结论；仍必须提交完整、可审计的 promoted graph artifacts，并显式列出非空 `blocking_issues`。
- `status=partial` 允许 Step 5 正式 finalize 并保留 promoted graph artifacts，但不再代表 Step 5 已完成；只有当 promoted graph alignment 已达到 Step 6 readiness gate 时，`mark_step_complete.py --step 5` 才允许推进到 Step 6。
- Step 7 与最终门禁会重新读取 `artifacts/graph/graph_span_alignment.json` 检查上述结构化粒度字段，不再只信任 `mapping_granularity=per_span_forward_code` 的自报结果。

子 agent 唯一入口：

- `references/agents/graph_path_analyst.md`

### Step 6 最终映射与交付物渲染

顺序运行：

- `scripts/pre_step_check.py --step 6`
- `scripts/prepare_agent_dispatch.py --agent-name artifact_renderer`

关键说明：

- `artifact_renderer` 是脚本型子 agent，负责运行正式渲染与汇总脚本。
- 主 agent 必须先读取 `audit/dispatch_artifact_renderer.json`，再调用该子 agent。
- 主 agent 禁止直接自己运行 Step 6 渲染脚本来替代 `artifact_renderer`。
- Step 6 子 agent 结束前必须运行 `scripts/write_render_outputs.py --workspace-dir <workspace>`，统一生成正式结果。
- `trace_view.annotated.json` 中 `code_location` 必须写到 `event.args.code_location`，不得与 `args` 并列。
- 若 `artifacts/mapping/stack_evidence_lite.json` 已存在，Step 6 应优先消费 lite 证据，避免整体加载超大 `stack_evidence.json` 导致渲染阶段 OOM。
- Step 6 现在还必须显式消费 `artifacts/graph/graph_operator_spans.json`；若 `graph_span_alignment.json` 缺少 `graph_operator_span_id`、存在无法回溯的 id、`location_kind != operator_call` 或 `requires_further_drilldown != false`，`run_step6_render_pipeline.py` 必须立即失败，不能再静默 fallback 到 phase hint 或全量 stack 扫描。
- 子 agent 唯一入口：`references/agents/artifact_renderer.md`

主 agent 对 Step 6 的子 agent 调用也必须原样执行标准 `Task(...)` 模板。

子 agent 返回并写完 Step 6 正式结果后，再运行：

- `scripts/record_subagent_completion.py --agent-name artifact_renderer`
- `scripts/finalize_agent_dispatch.py --agent-name artifact_renderer`
- `scripts/mark_step_complete.py --step 6`

### Step 7 验证与最终门禁

顺序运行：

- `scripts/pre_step_check.py --step 7`
- `scripts/prepare_agent_dispatch.py --agent-name artifact_validator`

随后主 agent：

1. 读取 `audit/dispatch_artifact_validator.json`
2. 原样执行标准 `Task(...)` 模板

子 agent 返回后运行：

- `scripts/record_subagent_completion.py --agent-name artifact_validator`
- `scripts/finalize_agent_dispatch.py --agent-name artifact_validator`
- `scripts/mark_step_complete.py --step 7`
- `scripts/check_final_gate.py`

关键说明：

- `artifact_validator` 的正式输入除校验工件外，还必须包含 `graph_execution_plan.json` 与 `graph_forward_context.json`，否则无法判断 graph replay 精度门禁。
- `artifact_validator` 的正式输入还必须包含 `graph_operator_spans.json` 与 `graph_span_alignment.json`，因为 Step 7 会直接复核每条正式 graph span 的结构化粒度字段，以及 `graph_operator_span_id` 是否能回溯到正式 operator span。
- 若 `input/validation_task.json` 缺失，`prepare_agent_dispatch.py` 会在正式 dispatch 前自动生成。
- Step 7 子 agent 结束前必须运行 `scripts/write_validation_outputs.py --workspace-dir <workspace>`，统一生成正式结果并回写 `state.artifacts.validation_result_path` 与 `state.flags.validation_passed`。

子 agent 唯一入口：

- `references/agents/artifact_validator.md`

## 7. profiling_debugger 回退流

当任一步骤出现脚本失败、缺少工件、子 agent 输出状态非法、final gate 不通过时：

1. 主 agent 先写 `input/error_context.json`
2. 运行 `scripts/prepare_agent_dispatch.py --agent-name profiling_debugger`
3. 读取 `audit/dispatch_profiling_debugger.json`
4. 原样执行标准 `Task(...)` 模板
5. 子 agent 返回后运行：
   - `scripts/record_subagent_completion.py --agent-name profiling_debugger`
   - `scripts/finalize_agent_dispatch.py --agent-name profiling_debugger`
   - `scripts/post_error_check.py`

`profiling_debugger` 的唯一入口：

- `references/agents/profiling_debugger.md`

## 8. Task 调用格式

主 agent 调子 agent 时，统一从对应 dispatch JSON 中取值：

- `subagent_type`
- `description`
- `query_text`

调用格式参考：

```text
Task(
  subagent_type=<dispatch.subagent_type>,
  description=<dispatch.description>,
  query=<dispatch.query_text>,
  response_language="中文"
)
```

不要自己重写 query；query 只能来自 `prepare_agent_dispatch.py` 生成的 dispatch 文件。

## 9. 你可以按需补读的总览文档

只有在需要人类说明或目录导航时，再读：

- `docs/README.md`
- `docs/WORKFLOW.md`
- `docs/SUBAGENTS.md`
- `docs/SCRIPTS_AND_GATES.md`
- `docs/EXECUTION_AND_ACCEPTANCE.md`
