# SGLang Path Map

本文件是 Step5 `graph_path_analyst` 的知识附录，用于把“已经由启动命令和模型 config 判断出的分支”映射到 SGLang 仓库中的实际目录和关键文件。

本文件不是正式门禁定义。若本文件与以下内容冲突，以后者为准：

- `references/agents/graph_path_analyst.md`
- `references/agents/artifact_validator.md`
- `scripts/check_final_gate.py`
- 当前仓库代码事实

本文件和另外两份 knowledge 文档的分工是：

1. `model_config_and_launch_fields.md`
   - 回答“字段说明了哪条分支”
2. `sglang_path_map.md`
   - 回答“当前分支应该去哪些目录和文件找”
3. `forward_analysis_rules.md`
   - 回答“找到模型文件后如何继续下钻到最终算子调用行”

## 1. 使用原则

本文件只服务于 Step5 graph replay 内路径重建。

它的作用是帮助 agent 快速缩圈到正确代码区域，而不是直接给出最终 `code_location`。

因此使用顺序必须是：

1. 先用 `model_config_and_launch_fields.md` 判断：
   - 主模型还是 draft 模型
   - decode graph 还是 speculative graph
   - attention / MoE / quant / cache / communication 大分支
2. 再用本文件确定该去哪些目录和关键文件。
3. 最后用 `forward_analysis_rules.md` 继续从模型 `forward` 下钻到真实算子调用行。

本文件的硬规则：

1. 找到模型主文件不等于完成。
2. 找到 `layers/*` 中某个包装类不等于完成。
3. 找到 `graph_runner/*` 不等于完成。
4. 找到 backend registry 只表示确定了分支，不等于找到最终算子调用行。

## 2. Step5 最常用的一级目录

Step5 需要重点关注的一级目录如下：

1. `python/sglang/srt/models`
   - 模型实现主目录
2. `python/sglang/srt/layers`
   - 通用层实现目录
3. `python/sglang/srt/hardware_backend`
   - 硬件后端特化目录
4. `python/sglang/srt/hardware_backend/npu`
   - Ascend NPU 特化目录
5. `python/sglang/srt/speculative`
   - speculative / EAGLE / NEXTN 相关目录
6. `python/sglang/srt/mem_cache`
   - KV cache、radix cache、hicache、offload 相关目录
7. `python/sglang/srt/distributed`
   - 通信、并行、collective 相关目录
8. `python/sglang/srt/disaggregation`
   - prefill/decode 拆分与 cache offload 相关目录

这些目录的职责不同，Step5 不能把它们混为一谈。

## 3. 模型入口路径

### 3.1 从哪里找具体模型文件

当字段已经判断出主模型或 draft 模型后，第一落点应是：

- `python/sglang/srt/models`

这里是 Step5 查找模型文件的主目录。

用途：

1. 根据 `architectures` / `model_type` 找到候选模型文件。
2. 定位顶层模型类、`EntryClass`、`ForCausalLM.forward`、`Model.forward`、decoder/layer 结构。
3. 确认 attention / mlp / moe 子模块的入口调用。

应该优先看的内容：

1. 模型文件末尾的 `EntryClass`
2. `ForCausalLM.forward`
3. `Model.forward`
4. `DecoderLayer.forward`
5. attention / mlp / moe 子模块定义

禁止停留点：

1. 只找到 `EntryClass` 就结束
2. 只找到 `ForCausalLM.forward` 就结束
3. 只把模型文件当最终定位

### 3.2 主模型和 draft 模型的路径隔离

当启动命令存在 draft 模型时：

1. 主模型路径用于：
   - 非 speculative `decode graph`
   - speculative `verify`
2. draft 模型路径用于：
   - `draft_prefill`
   - `draft_decode`

路径规则：

1. 主模型和 draft 模型必须分别到各自的模型目录读取 config 并定位模型文件。
2. 不能因为模型族相近，就直接复用主模型文件。
3. 若 profiling 证据已表明当前 span 属于 draft graph，就应优先从 draft 模型的 `models/*` 与子模块路径开始。

## 4. 通用层路径

### 4.1 `python/sglang/srt/layers`

这个目录是模型主文件继续下钻后的通用层实现区。

当模型文件中的 `forward` 已经下钻到 attention / mlp / moe / layernorm / quant / communicator 等模块时，应优先进入这里继续找。

该目录下与 Step5 最相关的子区域：

1. `layers/attention`
2. `layers/moe`
3. `layers/quantization`
4. `layers/rotary_embedding`
5. `layers/utils`
6. 根目录下的：
   - `activation.py`
   - `layernorm.py`
   - `linear.py`
   - `communicator.py`
   - `communicator_nsa_cp.py`
   - `dp_attention.py`
   - `elementwise.py`
   - `model_parallel.py`

### 4.2 attention 相关路径

当字段已判断出 attention 是关键路径，优先看：

1. `layers/attention/attention_registry.py`
2. `layers/attention/base_attn_backend.py`
3. `layers/attention/*_backend.py`
4. `layers/attention/linear/*`
5. `layers/attention/nsa/*`
6. `layers/attention/mamba/*`
7. `layers/rotary_embedding/*`

其中：

1. `attention_registry.py`
   - 用于确认 attention backend 的注册和分发点
2. `*_backend.py`
   - 用于确认通用 attention backend 的实现入口
3. `linear/*`
   - 用于线性注意力、GDN、KDA 等路径
4. `nsa/*`
   - 用于 NSA/稀疏注意力相关路径
5. `mamba/*`
   - 用于 mamba/mamba-like 相关路径

禁止停留点：

1. 停在 `self_attn(...)` 调用点
2. 停在 `attention_registry.py`
3. 停在 backend wrapper，而不继续看 backend 内最终 device op

### 4.3 MoE 相关路径

当字段已判断出是 MoE 模型或 graph span 语义像 expert/router/dispatch 时，优先看：

1. `layers/moe/router.py`
2. `layers/moe/topk.py`
3. `layers/moe/fused_moe_native.py`
4. `layers/moe/moe_runner/*`
5. `layers/moe/token_dispatcher/*`
6. `layers/moe/ep_moe/*`

这些路径分别对应：

1. router 决策
2. top-k expert 选择
3. fused moe 本地实现
4. moe runner
5. expert token dispatch
6. expert parallel 相关实现

禁止停留点：

1. 只在模型文件看到 `mlp` / `experts` 命名就结束
2. 只在 router 层结束
3. 只在普通 MLP 文件里找，而不继续看 MoE 子路径

### 4.4 量化相关路径

当字段已表明存在量化，优先看：

1. `layers/quantization/*`
2. 其中重点关注：
   - `modelopt_quant.py`
   - `unquant.py`
   - `w8a8_fp8.py`
   - `w8a8_int8.py`
   - `kv_cache.py`
   - 其他具体 quant scheme 文件

用途：

1. 判断某一层是否走量化线性、量化 attention、量化 KV cache 或量化 MoE。
2. 确认普通 linear/matmul 是否已被 quant backend 替代。

禁止停留点：

1. 看见模型启用量化后，仍只在未量化的 `linear.py` 中找最终算子。
2. 只在 config 里看见量化字段就结束，而不继续找具体 quant 实现文件。

## 5. NPU 特化路径

### 5.1 `python/sglang/srt/hardware_backend/npu`

这个目录是 Ascend NPU 相关特化实现的核心区域。

若当前环境、backend 或代码事实显示最终路径落在 NPU 特化实现，必须把这里视为一级目标目录。

该目录下与 Step5 最相关的子区域：

1. `hardware_backend/npu/attention`
2. `hardware_backend/npu/graph_runner`
3. `hardware_backend/npu/quantization`
4. `hardware_backend/npu/moe`
5. `hardware_backend/npu/modules`
6. 根目录下的：
   - `allocator_npu.py`
   - `memory_pool_npu.py`
   - `cmo.py`
   - `utils.py`

### 5.2 NPU attention 路径

当 attention backend 已经缩圈到 `ascend` 或代码中出现 NPU attention 分支时，优先看：

1. `hardware_backend/npu/attention/ascend_backend.py`
2. `hardware_backend/npu/attention/ascend_torch_native_backend.py`
3. `hardware_backend/npu/attention/ascend_gdn_backend.py`
4. `hardware_backend/npu/attention/ascend_hybrid_linear_attn_backend.py`
5. `hardware_backend/npu/attention/mla_preprocess.py`

用途：

1. 对标准 NPU attention 找最终实现
2. 对 native fallback 找最终实现
3. 对 GDN / 混合线性注意力找 NPU 实现
4. 对 MLA 相关路径找预处理与后续 backend 入口

禁止停留点：

1. 停在 `attention_registry.py` 里看到 `"ascend"` 就结束
2. 停在模型文件的 attention 调用点
3. 停在 NPU backend 初始化入口而不继续看内部 forward 和最终 op

### 5.3 NPU 量化与 MoE 路径

当字段显示量化或 MoE 且当前平台为 NPU，优先看：

1. `hardware_backend/npu/quantization/*`
2. `hardware_backend/npu/moe/*`

重点文件：

1. `hardware_backend/npu/quantization/fused_moe_method_npu.py`
2. `hardware_backend/npu/quantization/linear_method_npu.py`
3. `hardware_backend/npu/moe/topk.py`

用途：

1. 判断 NPU 是否接管了通用 quant/MoE 路径
2. 判断最终算子是普通层实现触发还是 NPU 特化实现触发

### 5.4 NPU graph runner 路径

`hardware_backend/npu/graph_runner` 的作用是 graph replay 运行壳层，而不是最终精确定位的默认终点。

该目录主要用于：

1. 确认 graph 组的大类
2. 确认 verify / draft graph 的组织方式
3. 确认 NPU graph 对哪些子路径进行了包裹

重点文件：

1. `npu_graph_runner.py`
2. `eagle_draft_npu_graph_runner.py`
3. `eagle_draft_extend_npu_graph_runner.py`（文件名保留历史命名，但语义上对应 `draft_prefill`）
4. `vit_npu_graph_runner.py`

硬规则：

1. 找到 graph runner 只能说明“已经定位到 graph replay 外壳”。
2. 默认不能把 graph runner 文件当最终 `code_location`。
3. 必须继续回到模型文件、层实现、NPU backend、quant/MoE/cache/communication 真实落点。

## 6. speculative 与 draft 模型路径

### 6.1 `python/sglang/srt/speculative`

当字段已表明是 `eagle` / `eagle3` / `nextn` 等 speculative 路径时，必须优先检查：

- `python/sglang/srt/speculative`

这里主要负责：

1. 主模型 verify 路径
2. draft 模型 prefill/decode 路径
3. speculative 算法组织方式

Step5 重点看的文件类型：

1. worker 类
2. info/config 类
3. 算法切换与阶段切换逻辑

用途：

1. 判断某个 graph phase 属于主模型还是 draft 模型
2. 判断 `draft_prefill` 和 `draft_decode` 的模型归属
3. 判断 speculative 算法对 graph 分组和 backend 选择的影响

硬规则：

1. `speculative` 目录主要用于判定 graph phase 与模型归属。
2. 它通常不是最终算子定位终点。
3. 一旦确认当前是 draft 模型 graph，必须切回 draft 模型文件与 draft 子模块路径继续找。

### 6.2 `nextn` 的处理

当字段显示 `nextn` 时：

1. 不要把它视为未知算法。
2. 应按当前服务端对 speculative 算法的归一化规则，进入 EAGLE 系列分析语境。
3. 但最终仍要以当前仓库代码的实际 algorithm dispatch 和 worker 路径为准。

## 7. cache 路径

### 7.1 `python/sglang/srt/mem_cache`

当 graph span 语义指向 KV cache 分配、更新、读写、offload、radix/hicache 时，优先看：

- `python/sglang/srt/mem_cache`

重点子区域：

1. `memory_pool.py`
2. `radix_cache.py`
3. `unified_radix_cache.py`
4. `chunk_cache.py`
5. `hicache_storage.py`
6. `storage/*`
7. `allocator.py`
8. `memory_pool_host.py`

用途：

1. 找 cache 分配和写入路径
2. 找 cache 读取和更新路径
3. 找 radix/hicache/offload 路径

禁止停留点：

1. 只在 attention 文件里看到 kv cache 参数就结束
2. 只在 cache manager 包装层结束
3. 只在 graph runner 或 scheduler 侧看到 cache 字样就结束

### 7.2 disaggregation 相关路径

若启动命令显示：

1. `disaggregation_mode`
2. decode KV cache offload
3. 其他 prefill/decode 拆分字段

则必须额外检查：

- `python/sglang/srt/disaggregation`

重点文件：

1. `prefill.py`
2. `decode.py`
3. `decode_kvcache_offload_manager.py` 对应的关联路径
4. `ascend/*`、`base/*`、`common/*`

这类路径通常和 cache / 传输 / decode 阶段有关，不应强行归入普通 attention 或普通模型层实现。

## 8. 通信路径

### 8.1 `python/sglang/srt/distributed`

当字段显示并行规模大于 1，或者 graph span 明显指向 collective / dispatch / sync / parallel 通信时，优先看：

- `python/sglang/srt/distributed`

重点文件：

1. `communication_op.py`
2. `parallel_state.py`
3. `utils.py`

用途：

1. 确认 TP/DP/EP/CP 通信组织方式
2. 找 collective 或设备通信调用的入口
3. 判断当前 span 更像通信还是纯计算

### 8.2 其他通信相关路径

除了 `distributed`，还要检查：

1. `layers/communicator.py`
2. `layers/communicator_nsa_cp.py`
3. `layers/dp_attention.py`
4. `layers/model_parallel.py`
5. `layers/moe/token_dispatcher/*`
6. `layers/moe/moe_runner/*`

原因：

1. 有些通信逻辑并不直接落在 `distributed/*`
2. MoE、DP attention、NSA context parallel 往往在层级实现里就已经有通信调用

禁止停留点：

1. 只在 manager/scheduler 层看到通信逻辑就结束
2. 只看 `parallel_state.py` 而不看实际 communication op

## 9. NPU 分支的路径判断口诀

当已经确定当前阶段是 NPU 场景时，路径缩圈按以下顺序：

1. 先找模型文件中的子模块调用点
2. 再找 `layers/*` 中对应通用实现
3. 再检查是否有：
   - `attention_registry.py` / backend selector
   - `MultiPlatformOp.forward_npu`
   - `if is_npu()`
   - `hardware_backend/npu/*`
4. 若存在更深的 NPU 特化实现，优先继续下钻到 NPU 文件
5. 若没有更深特化实现，才停留在通用层的 NPU/native 分支

其中与平台分发最相关的关键文件：

1. `layers/utils/multi_platform.py`
2. `layers/attention/attention_registry.py`

它们的意义是：

1. `multi_platform.py`
   - 用于判断某个 op 是否通过 `forward_npu` 分发
2. `attention_registry.py`
   - 用于判断 attention backend 是否已切到 `ascend` 或其他 backend

但注意：

1. 这两个文件本身通常不是最终定位终点。
2. 它们只用于告诉 Step5“下一步应该下钻到哪类文件”。

## 10. Step5 常见搜索起点

当 agent 需要快速缩圈时，优先从以下起点开始：

1. 模型相关
   - `python/sglang/srt/models`
2. 通用 attention
   - `python/sglang/srt/layers/attention`
3. 通用 MoE
   - `python/sglang/srt/layers/moe`
4. 通用 quant
   - `python/sglang/srt/layers/quantization`
5. NPU attention
   - `python/sglang/srt/hardware_backend/npu/attention`
6. NPU quant / MoE
   - `python/sglang/srt/hardware_backend/npu/quantization`
   - `python/sglang/srt/hardware_backend/npu/moe`
7. speculative
   - `python/sglang/srt/speculative`
8. cache
   - `python/sglang/srt/mem_cache`
9. distributed
   - `python/sglang/srt/distributed`

如果字段和 span 语义都不完整，建议按以下保守顺序找：

1. 先模型文件
2. 再 attention/mlp/moe 子模块
3. 再 quant/NPU/backend 分支
4. 再 cache/communication 分支
5. 最后才回看 graph runner 或 speculative worker 作归属校正

## 11. 常见误区

1. 误把 `models/*` 当最终定位
2. 误把 `graph_runner/*` 当最终定位
3. 误把 `attention_registry.py` 当最终定位
4. 误把 `MultiPlatformOp` 分发层当最终定位
5. 误把主模型路径复用到 draft graph
6. 误把通用 quant 路径当成 NPU quant 路径
7. 误把 cache 或 communication span 强行映射到 attention / mlp

## 12. 与下一份文档的衔接

使用本文件缩圈目录和关键文件后，下一步必须转到 `forward_analysis_rules.md`，继续完成：

1. 从模型 `forward` 逐层下钻
2. 判断何时从模型文件进入子模块文件
3. 判断何时从通用层进入 NPU / quant / communication / cache 特化实现
4. 判断什么位置才算最终有效 `code_location`
