# Ascend SGLang Profiling Analyzer V2

## 这是什么

这是一个面向 **Ascend + SGLang profiling** 的多阶段分析 skill。

它的目标不是生成一份泛泛的分析说明，而是把 profiling 证据逐步收敛成正式工件，最终交付：

- `output/trace_view.annotated.json`
- `artifacts/timeline/stream_span_timeline.json`

## 唯一正式入口

当前运行时入口已经收敛为：

- 主 agent：`SKILL.md`
- 子 agent：`references/agents/<agent>.md`

根目录 `README.md`、`docs/` 下的文档、以及其他说明文件都不是运行时主入口。

## 流程概览

当前主链是一个 7 步流程：

1. Step 1：发现输入并切片 profiling 数据
2. Step 2：构建统一时间索引 `timeline_index.json`
3. Step 3：由 `timeline_analyst` 通过 wrapper 生成 base classified/scope gate，再输出 review patch，并由 finalize promotion 成 canonical `classified_spans.json`
4. Step 4：完成 graph 外 stack 映射，并生成 graph phase stack 证据
5. Step 5：完成 graph 内 formal spans 的正式逐 span code mapping
6. Step 6：只消费正式 mapping 并渲染正式交付物
7. Step 7：执行正式验收，final gate 只做重复收口

更详细的分步说明见：

- `docs/WORKFLOW.md`
- `docs/SCRIPTS_AND_GATES.md`

## 目录分工

```text
Ascend SGLang Profiling Analyzer V2/
  README.md
  SKILL.md
  docs/
  examples/
  prompts/
  references/
  scripts/
```

各目录职责：

- `SKILL.md`
  - 主 agent 的唯一正式入口
- `references/agents/`
  - 各子 agent 的唯一正式操作手册
- `references/contracts/`
  - Step 3 / Step 4 / Step 5 等正式 JSON 合同与结构约束
- `references/knowledge/`
  - graph path / launch / model config 等知识文档
- `references/shared/`
  - 当前保留的共享规则附录
- `scripts/`
  - 正式脚本、门禁、promotion、render、validation 逻辑
- `docs/`
  - 非运行时主入口的总览说明，如流程、脚本门禁、执行与验收说明
- `examples/`
  - 输入模板与样例文件

## 当前关键约束

- 必须走真实主链：
  - `scripts/prepare_agent_dispatch.py`
  - `Task(...)`
  - `scripts/record_subagent_completion.py --task-call-id <task_agent_id>`
  - `scripts/finalize_agent_dispatch.py`
  - `scripts/mark_step_complete.py`
- 不允许伪造子 agent 输出，也不允许跳过 completion/finalize 闭环。
- dispatch 现在会显式携带 `main_agent_role`、`subagent_role`、`allowed_official_scripts`、`task_required`、`task_receipt_required`；completion/finalize 会强制校验这些字段的一致性。
- Step 4 / Step 5 的正式 JSON 输出必须满足对应合同：
  - `references/contracts/timeline_review_patch.schema.json`
  - `references/contracts/timeline_analysis_result.schema.json`
  - `references/contracts/stack_mapping_result.schema.json`
  - `references/contracts/graph_review_result.schema.json`
- Step 5 的 `graph_review_result.json` 在 finalize 前允许通过 `scripts/normalize_graph_review_result.py` 做纯结构层 normalize，但不会放宽语义精度门禁。
- Step 5 只有在 `status=passed` 时才代表 graph 正式 mapping 已完成；`status=partial` 只允许保留分析性工件，不能被 Step6 当成正式 graph mapping 消费。
- Step 6 / Step 7 仍要求 graph replay 场景达到 `per_span_forward_code` 精度，不能停留在 replay 入口、phase 级提示或 module call anchor。
- Step 6 不负责 graph drilldown、graph repair 或 fallback；若 graph mapping 仍不满足正式消费条件，主链会直接失败而不是静默补洞。

## 推荐阅读顺序

建议按下面顺序理解当前 skill：

1. `SKILL.md`
2. `docs/WORKFLOW.md`
3. `docs/SCRIPTS_AND_GATES.md`
4. `docs/EXECUTION_AND_ACCEPTANCE.md`
5. `references/agents/stack_mapper.md`
6. `references/agents/graph_path_analyst.md`

## 样例输入

当前样例输入模板位于：

- `examples/run_input.sample.json`

建议复制该文件后，按你的实际环境填写：

- `workspace_dir`
- `profiling_root_path`
- `window_start_ns`
- `window_end_ns`
- `code_repo_path`
- `model_root_path`
- `launch_command_text`
- `supplemental_input_paths`

注意：

- `examples/run_input.sample.json` 只是字段模板，里面的 `REPLACE_WITH_*` 路径必须先替换成真实本地路径后才能运行。
- `model_root_path` 不能留空；若希望由主 agent 从启动命令补推，也应先提供与当前任务匹配的真实启动命令或 `launch_command_file`。

## 执行说明

如果你要运行这套 skill：

- 从 `SKILL.md` 开始
- 不要把 `README.md` 或 `docs/` 文档当成运行时指令脚本
- 不要从 skill 内自行挑单个 orchestrator 脚本当作主入口

执行与验收的总览说明见：

- `docs/EXECUTION_AND_ACCEPTANCE.md`
