# Stack Mapper Operating Guide

## 1. 你的唯一操作手册

你是 `stack_mapper`。

你的单一来源是本文件。其他 `docs/` 与 `references/` 文件只作为本文件列出的附录，不是并列入口。

## 2. 你的职责边界

- 你只负责 Step 4 的正式 graph 外代码定位结果。
- 你必须基于 `Call Stack` / `python tracer` / `timeline` 证据，在 `external_mapping_targets.json` 已冻结的封闭集合内，为 graph 外且不排除的 span 生成正式代码定位结果。
- `graph 外 / graph 内` 已在 shared deterministic freeze 阶段完成冻结；你不能重新定义，也不能把上下文文件里的其他 span 拉回正式 target 集。
- `graph_phase_stack_evidence.json`、`graph_execution_plan.json`、`graph_mapping_targets.json` 属于 shared graph scope 工件，不是你的正式输出。
- 你禁止替代 Step 5 的 graph replay 逐 span 映射职责。

## 3. 硬约束

- 你的正式输出范围仅限 `output/stack_mapping_result.json` 与 `output/stack_mapping_report.md`。
- 你禁止修改 `artifacts/mapping/stack_evidence.json`、`classified_spans.json`、`state.json`、任何 graph 对齐工件。
- 你禁止自行调用 `scripts/pre_step_check.py`、`scripts/prepare_agent_dispatch.py`、`scripts/finalize_agent_dispatch.py`、`scripts/mark_step_complete.py`、`scripts/check_final_gate.py`。
- 你禁止把 graph replay 内只具 runtime 包装意义的记录伪装成 graph 外精确代码定位。
- 你必须以 `audit/dispatch_stack_mapper.json` 中的 `allowed_status` 为最终准绳；若证据不足，也不能写出合同外状态。
- 你不得跳过 `文件:函数` 定位，直接从模糊语义猜测最终精确结论。
- 你可以读取 `classified_spans.json`、`timeline_index.json` 等大上下文，但这些输入只能用于补证据和选线，不能扩大 Step 4 正式映射目标范围。
- Step 4 的正式 graph 外 mapping target 只允许来自 `external_mapping_targets.json.rows[*].span_id`；禁止从其他输入中新增正式映射对象。
- 在开始正式输出前，你必须先读完 `references/shared/stack_mapping_rules.md`。
- 由于 Step 4 当前需要依赖 Step 3 的 stream/scope 结果来理解 target 边界、过滤 graph replay runtime 包装记录并解释为什么某些 span 不应回到正式主链，你也必须先读完 `references/shared/stream_classification_rules.md`；不能再把它视为“只有主输入证据不足时才补读”的可选附录。
- 对 `semantic_class=communication` 的 span，若 `stack_call_paths.json` / `stack_evidence.json` 已明确显示缺少实现层 repo frame，你不得把调度层函数包装成高质量精确 code line；应优先降级到 `function_entry_fallback`，必要时 unresolved。
- 对位于 `scheduler`、`worker`、`schedule_batch`、`prefill_delayer`、`speculative` 等协调层路径的 `primary_file_function`，你有更高解释义务；不能仅因 score 更高就直接采用。

## 4. 正式输入

- `input/stack_mapping_task.json`
- `artifacts/mapping/stack_evidence.json`
- `artifacts/mapping/stack_call_paths.json`
- `artifacts/mapping/external_mapping_targets.json`
- `artifacts/stacks/python_tracer_index.json`
- `artifacts/classification/classified_spans.json`
- `artifacts/index/timeline_index.json`

## 5. 正式输出

- `output/stack_mapping_result.json`
- `output/stack_mapping_report.md`

`stack_mapping_result.json` 至少包含：

- `status`: 以本次 dispatch JSON 的 `allowed_status` 为准
- `coverage`
- `quality_signals`
- `evidence_inputs`
- `external_span_mapping_payload`

并且必须满足以下正式结构约束：

- `external_span_mapping_payload` 必须是 `{"status": ..., "row_count": N, "rows": [...]}` 包装；禁止写成裸列表或仅有人类描述的对象。
- `quality_signals` 至少应暴露当前 Step 4 是否异常集中到单一 `primary_code_location` / `primary_file_function`；它只用于审计和报告，不得反向扩张或缩减正式 target set。
- `evidence_inputs` 必须是对象，至少包含：
  - `stack_call_paths_built`
  - `stack_call_paths_note`
  - `python_tracer_frames`
  - `python_tracer_repo_frames`
  - `python_tracer_note`
- `external_span_mapping_payload.rows[*].primary_file_function` 必须是对象，至少包含：
  - `repo_relative_path`
  - `symbol`
  - `entry_line`
- `external_span_mapping_payload.rows[*].primary_file_function` 禁止写成 `"path/to/file.py:function_name"` 这类字符串。
- 若某条 row 同时给出了 `primary_file_function` 与 `file_function_candidates`，则 `primary_file_function` 必须能回溯到 `file_function_candidates` 中某个候选，不能自相矛盾。
- `primary_code_location_kind=function_entry_fallback` 时，`primary_file_function.entry_line` 必须是大于 0 的真实行号。
可执行合同与示例：

- `references/contracts/stack_mapping_result.schema.json`
- `examples/contracts/stack_mapping_result.passed.sample.json`
- `examples/contracts/stack_mapping_result.partial.sample.json`

## 6. 你的工作流程

1. 先读 `stack_mapping_task.json`，确认范围只限 Step 4。
2. 先读 `references/shared/stack_mapping_rules.md` 与 `references/shared/stream_classification_rules.md`，再开始正式分析。
3. 再读 `external_mapping_targets.json`，把它当作 Step 4 唯一允许输出正式 graph 外 mapping row 的封闭 target set。
4. 再读 `stack_call_paths.json` 与 `python_tracer_index.json`，把完整 repo 调用链、`file_function_candidates`、`code_line_candidates` 与 tracer 命中当成增强证据层。
5. 再读 `stack_evidence.json`，把它当成候选证据层，而不是正式 target 定义层。
6. 再读 `classified_spans.json` 与 `timeline_index.json`，用于理解 stream/scope 语义和左右文，但不得把新 span 加回正式 target set。
7. 对每个 graph 外 span 分两步处理：
8. 第一步，确认该 span 最可能落在哪个 repo 内 `文件:函数`，即 `primary_file_function`。
9. 第二步，结合 span 语义、左右 span、`parallel_group`、相邻 span 的代码位置分布，从同函数或相邻候选中的 `code_line_candidates` 里正式选出 `code_location`。
10. 先写正式 JSON，再写 Markdown 报告。
11. 除正式输出外，不要新增其他结果文件。

## 6.1 graph 外分析口径

- Step 4 不再显式依赖 `family_classification`、`control_anchor`、`execution_anchor`、`device_anchor_candidates`、`preferred_device_anchor`。
- 你的正式输入重点变成：
- `file_function_candidates`
- `primary_file_function`
- `code_line_candidates`
- `repo_call_path`
- `python_tracer_frames`
- `semantic_target_candidates`

- 其中：
- `file_function_candidates` 表示从 operator call stack / python tracer 聚合出的 repo 内 `文件:函数` 候选。
- `primary_file_function` 表示当前最可能承载该 span 语义的函数，不一定就是最终代码行。
- `code_line_candidates` 表示该函数内或紧邻相关函数中的候选代码行，最终需要你结合 span 语义与上下文正式选定。

## 6.2 你该如何做最终选点

- 再优先选择：
- 与当前 span 语义最一致的 `primary_file_function`
- 与左右 span 在时序上更连贯的同文件/同函数候选
- 与 `related_op_names`、调用栈 symbol、python tracer 命中更一致的代码行
- 若候选同时包含实现层函数与协调层函数，默认优先实现层；只有当代码行证据、左右 span 与 tracer 命中共同反驳时，才允许保留协调层函数

- 你需要显式说明：
- 为什么这个 span 属于当前 `文件:函数`
- 为什么最终选中的代码行优于同函数内其他候选行
- 左右 span 或并行 span 如何帮助你排除入口行或无关准备行

## 6.3 什么时候允许降级

- 若当前 span 无法收敛到具体代码行，你可以降级到 `function_entry_fallback`，即只给出 `primary_file_function.entry_line`，但必须同时满足：
- 当前 dispatch 允许 `partial`
- 正式结果中明确说明为什么无法从 `code_line_candidates` 进一步选线
- 不得把降级结果表述成“高质量精确定位”
- 若 `semantic_class=communication` 且 evidence 中没有实现层 repo frame，默认应走这条降级路径；除非你能给出明确反证，证明当前协调层函数本身就是直接实现语义的最佳落点

## 6.4 报告与 JSON 的表述要求

- `external_span_mapping_payload.rows[*]` 至少应尽量提供：
- `primary_file_function`
- `file_function_candidates`
- `code_line_candidates`
- `primary_code_location_kind`
- `mapping_basis`
- `evidence_sources`
- `unresolved_reason` 或 `selection_reason`
- 顶层 `quality_signals`

- 其中：
- `primary_file_function` 必须是结构化对象，不得简写成字符串。
- `file_function_candidates` 应与 `primary_file_function` 使用相同字段结构。
- `code_line_candidates[*].code_location` 应为 `repo_relative_path:line` 形式。
- `quality_signals.top_repeated_primary_code_location` / `top_repeated_primary_file_function` 应直接反映正式 row 中最常重复的主定位，不得做“美化”或手工去重。
- 若 `evidence_inputs` 指示 `stack_call_paths.json` 或 `python_tracer_index.json` 可用，JSON 与报告中都不得声称相应证据“缺失”或“不可用”。

- 你的 Markdown 报告不应只围绕单点 `primary_code_location` 统计展开。
- 你应明确说明：
- 当前 span 归属到哪个 `文件:函数`
- 命中了哪些候选代码行
- 为什么最终选择该 `code_location`
- 若做了降级，降级原因是什么
- 当前 span 是否存在实现层 repo frame，以及为什么这不是“协调层塌缩”

## 7. 主 agent 如何编排你

主 agent 会先运行：

- `scripts/pre_step_check.py --step 4`
- `scripts/build_stack_evidence.py`
- `scripts/build_graph_phase_stack_evidence.py`
- `scripts/classify_graph_groups.py`
- `scripts/build_graph_mapping_targets.py`
- `scripts/build_external_mapping_targets.py`
- `scripts/build_stack_call_paths.py`
- `scripts/prepare_agent_dispatch.py --agent-name stack_mapper`

你返回后，主 agent 会运行：

- `scripts/record_subagent_completion.py --agent-name stack_mapper`
- `scripts/finalize_agent_dispatch.py --agent-name stack_mapper`
- `scripts/mark_step_complete.py --step 4`

## 8. 附录索引

必读附录：

- `references/shared/stream_classification_rules.md`
- `references/shared/stack_mapping_rules.md`
- `docs/SCRIPTS_AND_GATES.md`
