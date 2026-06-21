# Sub Agents

## 1. 说明

本文件不再重复承载每个子 agent 的完整操作细则。

为了避免子 agent 在 `docs/` 与 `references/` 之间来回跳转，每个子 agent 现在都有自己的唯一入口文档。

## 2. 子 agent 唯一入口索引

- `timeline_analyst` -> `references/agents/timeline_analyst.md`
- `stack_mapper` -> `references/agents/stack_mapper.md`
- `graph_path_analyst` -> `references/agents/graph_path_analyst.md`
- `artifact_validator` -> `references/agents/artifact_validator.md`
- `profiling_debugger` -> `references/agents/profiling_debugger.md`
- `profiling_preprocessor` -> `references/agents/profiling_preprocessor.md`
- `artifact_renderer` -> `references/agents/artifact_renderer.md`

## 3. 主 agent 如何调度

主 agent 不得直接凭经验拼装 query，而必须按以下顺序执行：

1. 运行 `scripts/prepare_agent_dispatch.py --agent-name <agent>`
2. 读取 `audit/dispatch_<agent>.json`
3. 用 dispatch 文件中的 `subagent_type`、`description`、`query_text` 发起正式 `Task(...)`
4. 子 agent 真正返回后，先运行 `scripts/record_subagent_completion.py --agent-name <agent>`
5. 再运行 `scripts/finalize_agent_dispatch.py --agent-name <agent>`

## 4. prompt 与手册的关系

- `prompts/*.md`：极薄的引导层，只负责声明 agent 身份、唯一操作手册路径、以及“以手册为准”
- `references/agents/*.md`：唯一完整操作手册，承载职责、流程、输入输出、脚本权限与主 agent 调度方式
- 各 agent 的正式 `allowed_status`、脚本白名单/黑名单、输出边界，以各自手册和当次 `audit/dispatch_<agent>.json` 为准；禁止主 agent 或子 agent 自行扩展解释

实际调度时，`build_agent_query.py` 会把“唯一操作手册路径 + 正式输入输出文件 + 薄 prompt”组合成最终 query。
