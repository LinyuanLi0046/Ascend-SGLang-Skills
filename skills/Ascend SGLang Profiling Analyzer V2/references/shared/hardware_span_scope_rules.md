# Hardware Span Scope Rules

本文件定义 Step 3 `classify_spans.py` 的第一层作用域裁剪规则，是附录参考而不是完整的正式合同。

若本文件与 `references/agents/timeline_analyst.md`、`SKILL.md` 或最终 gate 冲突，以后者为准。

## 1. 目标

- 先确定哪些 span 属于需要代码定位的 Ascend Hardware 候选。
- 再在候选集合内做语义分类，而不是把所有 trace span 一视同仁送去后续映射。

## 2. 第一层准入

- 若 trace span 已解析出可用 `stream_id`，或 trace event `args` 中存在可用 `streamId` / `stream_id` / `Physic Stream id`，则可进入硬件候选判定。
- 若不存在可用 `streamId/stream_id`，默认标记为 `non_hardware_span`，不进入正式代码映射主链。

## 3. 强排除模式

以下模式即使带可用 `streamId/stream_id`，也默认标记为 `hardware_excluded`：

- `CAPTURE_*`
- `NOTIFY_*`
- `EVENT_*`
- `AscendCL@*`
- `Runtime@Event*`
- `Enqueue@record`
- `Dequeue@record`
- `Free`

## 4. 强保留模式

以下模式即使名称较小或容易被误判，也默认保留为 `hardware_semantic_candidate`：

- `fill_new_verified_id`
- `assign_req_to_token_pool`
- `assign_draft_cache_locs*`
- `cache_loc_assign`
- `cache_loc_update`
- `build_tree_efficient`
- `compute_position_kernel`

## 5. 输出字段

`classified_spans.json` 的每条 span 需至少包含：

- `has_stream_id`
- `stream_id`
- `scope_class`
- `matched_scope_rule_id`
- `matched_scope_rule_source`
- `exclude_from_code_mapping`
