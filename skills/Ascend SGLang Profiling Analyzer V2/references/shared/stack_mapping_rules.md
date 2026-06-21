# Stack Mapping Rules

本文件是 Step 4 的附录参考，正式输入输出边界和允许状态以 `references/agents/stack_mapper.md` 与当次 dispatch 合同为准。

- graph 外优先使用 `operator_details.csv` 的 `Call Stack`。
- 若存在 `stack_call_paths.json` 与 `python_tracer_index.json`，必须把完整 repo 调用链、`file_function_candidates` 与 tracer 命中当作增强证据层，而不是只回退到单个调用栈入口点。
- Step 4 的正式分析拆成两步：先确定 span 所在的 repo 内 `文件:函数`，再在该函数内结合语义与左右 span 选代码行。
- 仅选择 repo 内且最贴近当前 span 语义的 Python frame / function 作为候选。
- `file_function_candidates` 的排序必须同时考虑调用栈结构与当前 span 语义，不能只按通用路径/深度启发式选最高分候选。
- 命中 `speculative/`、`scheduler`、`worker`、`schedule_batch`、`prefill_delayer` 等协调层路径，不能单独作为“更贴近实现层”的理由。
- 若 stack 只提供 `replay()` 锚点，则不得假装已经完成 graph 内精确定位。
- 若 tracer 只命中外部 `torch` / `torch_npu` wrapper，不得直接把外部 wrapper 当成最终 `code_location`。
- 对 `communication` span，若 repo 调用栈里只有调度/协调层 Python frame、缺少实现层 repo frame，则不得把调度层函数伪装成实现层 code line；应优先降级到 `function_entry_fallback` 或 unresolved。
- `code_location` 必须是相对当前 `code_repo_path` 的路径加行号。
- 证据不足时只可在合同允许的前提下返回 `partial`，不得伪造精确代码行，也不得输出合同外状态。
