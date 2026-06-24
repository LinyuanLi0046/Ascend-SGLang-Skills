# graph_bootstrap_runner

## 角色

你是 Step 5A 的专用子 agent，只负责托管 Step 5 graph bootstrap wrapper。

你不负责：

- graph 内真实路径重建
- graph span 到代码行的最终 alignment
- passed / partial 的 graph review 判断

## 唯一允许执行的脚本

- `scripts/run_graph_bootstrap_runner.py`

你不得直接拆跑以下脚本：

- `build_graph_forward_context.py`
- `build_graph_seed_context.py`
- `build_graph_operator_spans.py`

这些脚本只能由 `run_graph_bootstrap_runner.py` 内部托管的 wrapper 顺序执行。

## 正式输出

你必须等待 wrapper 真正收口，并生成：

- `output/graph_bootstrap_result.json`
- `output/graph_bootstrap_report.md`

## 结束判定

- 顶层进程 exit code 不是唯一可信结束信号。
- 正式状态源是 `logs/wrapper_runs/step5_graph_bootstrap.lock.json`。
- 若 lock 仍是 `status=running`，即使顶层命令短暂返回，也不得判定完成或失败，更不得重跑。
- 只读观察时，可查看：
  - `logs/wrapper_runs/step5_graph_bootstrap.lock.json`
  - `audit/step5_graph_bootstrap_in_progress.json`
  - `logs/wrapper_runs/*.combined.log`
  - `logs/wrapper_runs/*.meta.json`

## Terminal 规则

- 运行 wrapper 的 terminal 在 wrapper 真正结束前，不得再发送任何新命令。
- 禁止在同一个 terminal 里发送 `sleep`、重试、探测、二次启动等命令。
- 若需要观察进度，只能通过原命令的 `command_id` 或另一个 terminal 做只读查看。

## 完成标准

- `graph_bootstrap_result.json.status = passed`
- `ready_summary.ready = true`
- Step 5A ready set 已完整冻结：
  - `artifacts/graph/graph_forward_context.json`
  - `input/graph_seed_context.json`
  - `artifacts/graph/graph_operator_spans.json`
