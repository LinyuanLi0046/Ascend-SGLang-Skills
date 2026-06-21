# Execution And Acceptance

本文件用于说明当前 skill 的推荐执行方式、正式工件检查点与验收口径。

- 它说明当前推荐怎么运行这套 skill。
- 它说明一次完整运行后应该看到哪些正式工件。
- 它说明当前验收时最关键的检查口径是什么。
- 它不是主 agent 或子 agent 的运行时主入口；运行时仍以 `SKILL.md` 和 `references/agents/<agent>.md` 为准。

## 1. 推荐执行方式

当前唯一正式执行入口：

- `SKILL.md`

主 agent 必须按 `SKILL.md` 执行真实多 agent 编排：

- `scripts/prepare_agent_dispatch.py`
- `Task(...)`
- `scripts/record_subagent_completion.py`
- `scripts/finalize_agent_dispatch.py`

## 2. 样例输入文件

样例输入模板位于：

- `examples/run_input.sample.json`

建议复制成你自己的运行文件，再修改：

- `workspace_dir`
- `profiling_root_path`
- `window_start_ns`
- `window_end_ns`
- `model_root_path`
- `launch_command_text`
- `supplemental_input_paths`

补充约束：

- 样例中的 `REPLACE_WITH_*` 仅表示待替换占位路径，不能原样照抄执行。
- `workspace_dir` 必须是你本次运行要写入产物的真实 workspace。
- `model_root_path` 默认应填写真实模型目录；只有当主 agent 明确具备从启动命令回填该路径的上下文时，才可依赖后续解析补齐。

## 3. 推荐执行约束

- 使用 `examples/run_input.sample.json` 作为输入模板来源
- 由主 agent 负责把这些输入写入 workspace 并初始化 `state.json`
- 若用户 prompt 显式给出“启动参数/模型 config/时间窗口获取目录”，主 agent 应将这些路径写入 `supplemental_input_paths`，再运行 `scripts/resolve_step1_inputs.py`
- 若未显式给出 `model_root_path`，允许 `resolve_step1_inputs.py` 从 `run.sh`/启动命令中的 `--model-path` 做本地目录回填
- 不再从 skill 内直接执行任何单脚本 orchestrator

## 4. 执行前检查

运行前应确认：

- profiling 根目录存在
- 样例窗口确实有事件
- `code_repo_path` 指向当前工作区中实际使用的 `sglang-prof/sglang-main`
- `model_root_path` 与代码版本匹配
- `launch_command_text` 尽量接近真实启动命令

## 5. 一次完整运行后应该看到什么

### Workspace 基础文件

- `state.json`
- `task_plan.md`
- `findings.md`
- `progress.md`

### 切片工件

- `artifacts/slices/*`

### 中间工件

- `artifacts/index/timeline_index.json`
- `artifacts/classification/classified_spans.json`
- `artifacts/mapping/stack_evidence.json`
- `artifacts/mapping/stack_evidence_lite.json`
- `artifacts/mapping/stack_call_paths.json`
- `artifacts/mapping/external_span_mapping.json`
- `artifacts/graph/graph_phase_stack_evidence.json`
- `artifacts/graph/graph_execution_plan.json`
- `artifacts/graph/graph_mapping_targets.json`
- `artifacts/graph/graph_forward_context.json`
- `artifacts/graph/graph_operator_spans.json`
- `artifacts/graph/graph_span_candidates.json`
- `artifacts/graph/forward_segment_template.json`
- `artifacts/graph/graph_span_alignment.json`
- `artifacts/mapping/span_code_mapping.json`

### 正式交付物

- `output/trace_view.annotated.json`
- `artifacts/timeline/stream_span_timeline.json`

### 验证工件

- `output/validation_result.json`
- `output/validation_report.md`
- `logs/agent_calls/index.jsonl`

## 6. 当前验收口径

P0 当前验收看以下几项：

- Step 4 已生成 `external_span_mapping.json`，且 shared scope freeze 已生成 `graph_phase_stack_evidence.json`
- `span_code_mapping.json.coverage.mapped_span_count > 0`
- `span_code_mapping.json.coverage.unresolved_semantic_span_count = 0`
- 所有已识别 graph spans 的正式 graph 场景，其 `mapping_granularity = per_span_forward_code`
- Step 5 若宣称 `passed`，其正式 payload 中不得再出现 `line<=0` 或 `code_location=path:0` 这类未核实行号
- `trace_view.annotated.json` 存在
- `stream_span_timeline.json` 存在
- `check_final_gate.py` 通过

## 7. 文档边界

本文件只回答 3 个问题：

- 应该从哪里开始执行这套 skill
- 一次完整运行后应该看到哪些正式工件
- 当前验收时最关键的检查口径是什么

它不负责：

- 充当主 agent 或子 agent 的运行时操作手册
- 记录某个临时 workspace 的历史验证纪要
- 承载已经下线的旧单脚本入口说明
