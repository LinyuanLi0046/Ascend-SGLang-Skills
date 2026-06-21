# Stream Classification Rules

本文件是 Step 3 时序语义分析的附录参考，正式输出字段和允许状态以 `references/agents/timeline_analyst.md` 与当次 dispatch 合同为准。

- 优先用 `task_time_*.csv` 和 `op_summary_*.csv` 判断 stream 的 compute / communication / runtime_control 角色。
- `CAPTURE_WAIT`、`EVENT_WAIT`、`EVENT_RESET`、`NOP` 默认视为无代码语义。
- 多 stream 并行必须通过时间重叠和 `parallel_group` 表达，不允许把不同 stream 强行串成单流。
- 证据不足时允许保留 `unknown`，禁止伪精确分类。
