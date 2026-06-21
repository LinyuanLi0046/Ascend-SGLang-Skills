# Docs Overview

## 1. 先看这里

本目录现在不承担 agent 运行时的主入口职责。

为了避免主 agent / 子 agent 因为在 `docs/` 与 `references/` 之间来回跳转而产生混乱，当前入口已经收敛为：

- 主 agent 唯一入口：`../SKILL.md`
- 子 agent 唯一入口：`../references/agents/<agent>.md`

## 2. 本目录的用途

`docs/` 只保留总览和补充说明，不承担运行时主入口职责：

- `WORKFLOW.md`：分阶段总体流程说明
- `SUBAGENTS.md`：子 agent 入口索引
- `SCRIPTS_AND_GATES.md`：脚本与门禁总览
- `EXECUTION_AND_ACCEPTANCE.md`：执行方式、正式工件检查点与验收口径说明

## 3. 建议阅读顺序

若你是主 agent：

1. `../SKILL.md`
2. 必要时再回看本目录中的总览文档

若你是某个子 agent：

1. `../references/agents/<agent>.md`
2. 仅在该手册要求时再补读附录
