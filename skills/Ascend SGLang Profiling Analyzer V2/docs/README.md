# Ascend SGLang Profiling Analyzer V2

当用户要求 从 profiling 文件夹 完成 profiling 到代码行的映射时使用

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

当前总览口径同步遵循以下主规则：

- Step 3 现在同时维护 `timeline_review_patch` 与 `timeline_analysis_result` 两份正式合同；子 agent 先产 base 工件，再由 finalize promotion 成 canonical Step 3 结果。
- Step 5 负责完成 graph 内 formal spans 的正式逐 span code mapping；只有 `status=passed` 才代表 graph 正式 mapping 完成。
- Step 6 只消费 Step4/Step5 的正式 mapping 结果并渲染交付物，不负责 graph drilldown、repair 或 fallback。
- Step 7 是正式验收层；`check_final_gate.py` 负责最终重复收口，而不是第一次发现基础 graph 精度问题。

## 3. 建议阅读顺序

若你是主 agent：

1. `../SKILL.md`
2. 必要时再回看本目录中的总览文档

若你是某个子 agent：

1. `../references/agents/<agent>.md`
2. 仅在该手册要求时再补读附录
