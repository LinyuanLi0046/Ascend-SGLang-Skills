# Model Config And Launch Fields

本文件是 Step5 `graph_path_analyst` 的知识附录，用于把“启动命令 + 模型 config”映射到 graph replay 内的代码路径缩圈规则。

本文件不是正式门禁定义。若本文件与以下内容冲突，以后者为准：

- `references/agents/graph_path_analyst.md`
- `references/agents/artifact_validator.md`
- `scripts/check_final_gate.py`
- 当前仓库代码事实

## 1. 文档用途

Step5 的目标不是解释完整运行时调度主链，而是：

1. 先基于启动命令和模型 config 判断当前 graph replay 属于哪一类执行分支。
2. 再据此缩圈到正确的模型文件、子模块文件、NPU 特化路径、量化路径、draft 模型路径。
3. 最后配合 `forward_analysis_rules.md` 继续下钻到真实算子调用行。

本文件只回答两个问题：

1. 哪些字段决定了应该去哪里找代码。
2. 每类字段出现后，Step5 下一步应该优先查看哪些目录和文件。

## 2. Step5 输入来源

Step5 需要优先读取以下输入：

1. `launch_command.json`
2. `model_context.json`
3. `runtime_constraints.json`
4. 模型目录中的：
   - `config.json`
   - `generation_config.json`
   - `tokenizer_config.json`
   - `quant_model_description.json`（若存在 ModelSlim 量化）

其中真正决定代码分支的主输入仍是：

1. 启动命令中的 server args
2. `config.json` 中的模型结构字段

`generation_config.json` 和 `tokenizer_config.json` 主要用于辅助，不应替代模型结构字段本身。

若模型目录存在 `quant_model_description.json`，则它是 ModelSlim 量化路径的一级输入，优先级高于仅凭 `quantization` / `quant_method` 的粗粒度判断。

## 3. 总体判断顺序

Step5 应按以下顺序使用字段：

1. 先判断当前是否 graph replay，以及 graph 大类：
   - 主模型 `decode graph`
   - speculative `verify`
   - speculative `draft_prefill`
   - speculative `draft_decode`
2. 再判断模型族与模型入口：
   - `architectures`
   - `model_type`
3. 再判断结构分支：
   - dense / MoE
   - 标准 GQA / MLA / DSA/NSA / 线性注意力
4. 再判断 NPU 路径形态：
   - backend registry
   - `forward_npu`
   - `is_npu()` 分支
   - 更早平台切换
5. 再判断量化分支：
   - 主模型量化
   - draft 模型量化
   - 模块级量化差异
6. 再判断 cache / communication / prefill/decode 差异。

禁止反过来做：

1. 先按模型名猜测具体实现。
2. 先打开某个模型文件硬找 attention 实现。
3. 没区分主模型和 draft 模型就直接下钻。

## 4. 启动命令字段

### 4.1 最优先读取的字段

以下字段直接决定 Step5 的大分支：

1. `--model-path`
2. `--speculative-algorithm`
3. `--speculative-draft-model-path`
4. `--attention-backend`
5. `--decode-attention-backend`
6. `--prefill-attention-backend`
7. `--speculative-draft-attention-backend`
8. `--device`
9. `--quantization`
10. `--speculative-draft-model-quantization`
11. `--tp-size`
12. `--dp-size`
13. `--ep-size`
14. `--moe-dp-size`
15. `--attn-cp-size`
16. `--kv-cache-dtype`
17. `--page-size`
18. `--disable-radix-cache`
19. `--enable-hierarchical-cache`
20. `--chunked-prefill-size`
21. `--max-prefill-tokens`
22. `--num-continuous-decode-steps`
23. `--disaggregation-mode`
24. `--enable-dp-attention`
25. `--enable-multi-layer-eagle`

### 4.2 speculative 相关字段

Step5 必须完整支持：

1. `eagle`
2. `eagle3`
3. `nextn`

因此以下字段是 speculative 判断的主入口：

1. `speculative_algorithm`
2. `speculative_draft_model_path`
3. `speculative_num_steps`
4. `speculative_num_draft_tokens`
5. `speculative_attention_mode`
6. `speculative_draft_attention_backend`
7. `speculative_draft_model_quantization`
8. `enable_multi_layer_eagle`

使用规则：

1. 只要出现 `speculative_algorithm`，就必须进入 speculative 决策树。
2. 出现 `speculative_draft_model_path` 时，必须把 draft 模型视为独立模型，不得默认复用主模型文件和主模型 config。
3. `nextn` 不可被当作“未知 speculative”；应按当前服务端的 speculative 归一化语义处理。
4. 对 speculative span，必须优先先判定：
   - 当前是主模型 `verify`
   - 还是 draft 模型 `draft_prefill`
   - 还是 draft 模型 `draft_decode`

### 4.3 backend 相关字段

以下字段用于判断 attention 和线性注意力等后端分支：

1. `attention_backend`
2. `decode_attention_backend`
3. `prefill_attention_backend`
4. `speculative_draft_attention_backend`
5. `linear_attn_backend`
6. `linear_attn_decode_backend`
7. `linear_attn_prefill_backend`
8. `nsa_prefill_backend`
9. `nsa_decode_backend`
10. `mamba_backend`

使用规则：

1. 若 attention backend 已在启动参数中显式指定，优先把它视为比模型名更高的线索。
2. 对 decode/prefill 专用 backend，必须分别判断，不可假设两者共用同一实现。
3. 对 speculative draft 模型，draft attention backend 优先级高于主模型 attention backend。
4. 若字段缺失，再退回到模型结构与平台注册逻辑判断。

### 4.4 并行与通信字段

以下字段用于判断 graph replay 内是否可能出现通信路径或 MoE 并行路径：

1. `tp_size`
2. `dp_size`
3. `ep_size`
4. `moe_dp_size`
5. `attn_cp_size`
6. `moe_a2a_backend`
7. `moe_runner_backend`
8. `speculative_moe_runner_backend`
9. `speculative_moe_a2a_backend`
10. `dist_init_addr`
11. `nccl_port`
12. `enable_dp_attention`
13. `enable_prefill_context_parallel`
14. `enable_nsa_prefill_context_parallel`

使用规则：

1. 只要 `tp_size/dp_size/ep_size/attn_cp_size` 中任一大于 1，就必须把通信路径作为有效候选，而不是只看纯计算层。
2. 对 MoE 模型，只要 `ep_size > 1` 或 `moe_dp_size > 1`，就必须额外查 expert dispatch / collective 路径。
3. speculative 下若存在独立的 draft moe backend，也要单独判断 draft 模型路径。

### 4.5 cache / 调度阶段字段

以下字段帮助判断 prefill / decode / cache 分支：

1. `chunked_prefill_size`
2. `max_prefill_tokens`
3. `prefill_max_requests`
4. `num_continuous_decode_steps`
5. `kv_cache_dtype`
6. `page_size`
7. `disable_radix_cache`
8. `enable_hierarchical_cache`
9. `hicache_ratio`
10. `hicache_size`
11. `hicache_write_policy`
12. `hicache_io_backend`
13. `hicache_mem_layout`
14. `enable_lmcache`
15. `disaggregation_mode`
16. `disaggregation_decode_enable_offload_kvcache`
17. `num_reserved_decode_tokens`

使用规则：

1. 若 graph span 明显属于 cache 管理、cache 更新、cache 读写，不得只停在模型 attention 文件。
2. 若启用了层次化 cache、offload 或 disaggregation，必须把 `mem_cache` / `disaggregation` 相关路径提升为一级候选。
3. `kv_cache_dtype` 也会影响部分 backend 或 kernel 选择，不能忽略。

## 5. 模型 config 主字段

### 5.1 模型身份字段

最优先字段：

1. `architectures`
2. `model_type`

使用规则：

1. `architectures` 是主入口判断字段，优先级高于模型目录名。
2. `model_type` 用于补强和兜底，不应替代 `architectures`。
3. 如果 draft 模型单独存在，draft 模型也必须单独读取它自己的 `architectures` 与 `model_type`。

下一步动作：

1. 先从 `architectures[0]` 缩圈到 `python/sglang/srt/models` 中的候选文件。
2. 再从模型文件的 `EntryClass` 和顶层 `forward` 继续下钻。

### 5.2 attention 结构字段

必须关注：

1. `num_attention_heads`
2. `num_key_value_heads`
3. `head_dim`
4. `hidden_size`
5. `rope_theta`
6. `rope_scaling`
7. `attention_chunk_size`
8. `sliding_window`
9. `use_sliding_window`
10. `context_length` / `max_position_embeddings`
11. 其他明确指向 MLA、NSA、线性注意力或特殊注意力的字段

使用规则：

1. `num_attention_heads` 与 `num_key_value_heads` 是判断标准 MHA / GQA / MQA 的基础线索。
2. 若 config 中存在 MLA、NSA、线性注意力、混合注意力相关字段，必须把对应特化 backend 和层实现列为一级候选。
3. 不能只因模型名像 Qwen/Llama 就默认按标准 GQA 处理。
4. 对 DSA/NSA/线性注意力，必须先确认字段证据，再决定进入对应后端分支。

下一步动作：

1. 先看模型文件中的 attention 类。
2. 再看其调用的是通用 `layers/attention`、线性注意力、还是 NPU 专用 backend。
3. 若发现 backend selector，再继续按 backend 字段往下钻。

### 5.3 层数与结构规模字段

必须关注：

1. `num_hidden_layers`
2. `intermediate_size`
3. `hidden_size`
4. `vocab_size`
5. `tie_word_embeddings`

这些字段主要用于：

1. 帮助确认当前模型文件是否匹配。
2. 辅助判断是否存在特殊 layer 结构或共享层模式。
3. 辅助 Step5 在 graph span 对齐时验证当前推断是否合理。

这些字段通常不是直接决定 backend 的字段，但能帮助排除错误模型路径。

### 5.4 MoE 字段

必须关注：

1. `num_experts`
2. `num_experts_per_tok`
3. `moe_intermediate_size`
4. 其他 router / gate / shared expert / topk 相关字段

使用规则：

1. 只要出现专家数或专家路由字段，就必须把 MoE 路径视为一级候选。
2. 对 MoE 模型，不可只看 attention 和普通 MLP；必须继续检查：
   - router
   - topk
   - expert dispatch
   - expert compute
   - expert communication
3. 若同时有 `ep_size`、`moe_dp_size`、`moe_a2a_backend` 等并行字段，必须把通信路径提升为强候选。

下一步动作：

1. 从模型文件定位到 MoE block。
2. 再继续下钻到 `layers/moe` 与 `hardware_backend/npu/moe`、`hardware_backend/npu/quantization` 等路径。

### 5.5 量化字段

必须关注：

1. `quantization`
2. `quantize`
3. `quant_method`
4. `quantization_config`
5. 其他模型自身携带的量化字段

对启动命令，还必须联动：

1. `--quantization`
2. `--speculative-draft-model-quantization`
3. `--quantize-and-serve`

使用规则：

1. 量化必须以 config 和显式启动参数为准，不能只看模型名称或目录名猜测。
2. draft 模型量化和主模型量化必须分别判断。
3. 不同模块可能走不同量化实现，不能把“模型启用了量化”简化成“全模型只有一条量化路径”。
4. 对 NPU 场景，要优先检查是否存在 ModelSlim/NPU 特化量化分支，而不是默认走通用量化实现。
5. 若模型目录存在 `quant_model_description.json`，要以该文件的模块级标注为准，而不是只看全局 `quantization` 名称。

下一步动作：

1. 先确认模型/草稿模型是否量化。
2. 再确认量化类型影响的是：
   - attention
   - linear
   - moe
   - lm_head
   - 其他特化模块
3. 最后继续下钻到对应的 quant backend 或 NPU quant 路径。

### 5.6 ModelSlim 量化专用文件

当模型目录中存在：

1. `quant_model_description.json`

Step5 必须把它视为 ModelSlim 的模块级量化真源之一。

这个文件不是普通摘要，而是逐参数、逐模块标注量化类型的映射表。

典型结构特征包括：

1. 顶层全局字段
   - `version`
   - `model_quant_type`
2. 模块/参数粒度字段
   - `model.layers.0.self_attn.q_proj.weight`
   - `model.layers.0.self_attn.q_proj.weight_scale`
   - `model.layers.0.self_attn.q_proj.weight_offset`
   - `model.layers.0.self_attn.q_proj.input_scale`
   - `model.layers.0.self_attn.q_proj.input_offset`
   - `model.layers.0.self_attn.q_proj.quant_bias`
   - `model.layers.0.self_attn.q_proj.deq_scale`
   - 以及 MoE expert、norm、embedding、lm head 等其他参数项

使用规则：

1. `model_quant_type` 只能作为全局概览，不能替代逐参数判断。
2. 真正决定某个模块是否量化、量化到什么程度的，是模块级键值对本身。
3. 必须按“当前 span 对应的模块前缀”去读相关键，而不是只看文件开头几项。

#### 5.6.1 如何理解 `FLOAT`

在 ModelSlim 的 `quant_model_description.json` 中：

1. `FLOAT` 不应被机械理解为 `FP32`
2. 它表示“该参数未按 ModelSlim 量化格式存储”
3. 在实际 NPU 推理里，这类参数通常更接近“非量化权重/参数”，常见实际 dtype 往往是 `BF16`，而不是必须是 `FP32`

因此 Step5 的规则是：

1. 看到 `FLOAT` 时，应理解为“未量化路径”或“普通精度路径”。
2. 不得因为字段值是 `FLOAT`，就直接把代码路径推断为 FP32 专用分支。
3. 最终实际 dtype 仍要结合：
   - 模型权重加载逻辑
   - NPU kernel 实现
   - 当前模块实际 params dtype
   来判断。

#### 5.6.2 ModelSlim 文件里哪些字段最有用

对 Step5 最有价值的不是所有键，而是以下几类：

1. 全局级：
   - `version`
   - `model_quant_type`
2. 模块主权重级：
   - `<prefix>.weight`
3. 量化辅助参数级：
   - `<prefix>.weight_scale`
   - `<prefix>.weight_offset`
   - `<prefix>.input_scale`
   - `<prefix>.input_offset`
   - `<prefix>.quant_bias`
   - `<prefix>.deq_scale`

这些字段的意义：

1. `<prefix>.weight`
   - 决定当前模块主权重采用哪类 ModelSlim scheme
2. `weight_scale` / `weight_offset`
   - 说明该层不是普通未量化线性，而是带量化缩放/偏移的线性路径
3. `input_scale` / `input_offset`
   - 常见于静态 W8A8 路径
4. `quant_bias` / `deq_scale`
   - 常见于需要量化偏置/反量化缩放的实现

Step5 的判断原则：

1. 若某个模块只有 `<prefix>.weight = FLOAT`，则优先视为未量化模块。
2. 若某个模块同时存在 `weight_scale/weight_offset/input_scale/input_offset/quant_bias/deq_scale` 等项，优先视为 ModelSlim 量化模块。
3. 同一层内不同模块可能量化类型不同，必须逐模块判断。

#### 5.6.3 ModelSlim 代码里可直接参考的关键字段与规则

从 `python/sglang/srt/layers/quantization/modelslim/modelslim.py` 可以直接提炼出以下规则：

1. 正式配置文件名：
   - `quant_model_description.json`
2. 配置对象名：
   - `ModelSlimConfig`
3. 关键内部字段：
   - `quant_description`
   - `ignore`
   - `packed_modules_mapping`

这些字段的含义：

1. `quant_description`
   - 即 `quant_model_description.json` 解析后的主映射表
2. `ignore`
   - 显式声明应忽略或跳过量化判定的模块
3. `packed_modules_mapping`
   - 处理融合模块与真实 shard 名称的映射，例如打包后的 `qkv_proj`、`gate_up_proj` 与实际 shard 名称的对应关系

Step5 必须知道：

1. 不能只按模型文件里的融合模块名去查 `quant_model_description.json`
2. 某些融合模块需要通过 `packed_modules_mapping` 回映射到真实量化键名
3. 尤其是 fused qkv / fused gate_up 这类结构，必须考虑 shard 名称映射

#### 5.6.4 ModelSlim 里有哪些量化 scheme 名称可直接参考

从 `ModelSlimConfig.get_linear_scheme()` 和 `get_moe_scheme()` 可直接提炼出当前仓库识别的关键 scheme：

1. Linear:
   - `W4A4_DYNAMIC`
   - `W8A8`
   - `W8A8_DYNAMIC`
2. MoE:
   - `W4A4_DYNAMIC`
   - `W4A8_DYNAMIC`
   - `W8A8_DYNAMIC`

使用规则：

1. `W8A8`
   - 通常表示静态 W8A8 linear 路径
2. `W8A8_DYNAMIC`
   - 通常表示动态 W8A8 linear 或 MoE 路径
3. MoE expert 常见是 `W8A8_DYNAMIC`
4. 同一模型中 attention 投影与 MoE expert 可以走不同 scheme

因此 Step5 不得把：

1. `model_quant_type = W8A8`

直接简化成：

1. “全模型所有模块都走同一个 W8A8 kernel”

#### 5.6.5 如何根据 ModelSlim 文件判断模块是否跳过量化

从 `ModelSlimConfig.is_layer_skipped()` 可直接提炼出规则：

1. 对普通线性层，若 `<prefix>.weight == FLOAT`，则视为该层跳过 ModelSlim 量化
2. 对 fused 模块，若 shard 中有的量化、有的 `FLOAT`，这是异常情况；正常要求 fused shards 精度一致

Step5 的使用规则：

1. 若 `<prefix>.weight == FLOAT`，优先把当前模块视为未量化实现
2. 但不能因此推断它一定是 FP32；通常只是“非量化”，实际可能仍是 BF16
3. 对 fused 模块，若多个 shard 的量化状态不一致，应提高警惕，不要草率给出最终定位

#### 5.6.6 ModelSlim 对路径下钻的影响

当 `quant_model_description.json` 显示当前模块走 ModelSlim 量化时，Step5 的下一步不是停在 config，而是要继续优先检查：

1. `python/sglang/srt/layers/quantization/modelslim/modelslim.py`
2. `python/sglang/srt/layers/quantization/modelslim/schemes/modelslim_scheme.py`
3. `python/sglang/srt/layers/quantization/modelslim/schemes/modelslim_w8a8_int8.py`
4. `python/sglang/srt/layers/quantization/modelslim/schemes/modelslim_w8a8_int8_moe.py`
5. `python/sglang/srt/layers/quantization/modelslim/schemes/modelslim_w4a8_int8_moe.py`
6. `python/sglang/srt/layers/quantization/modelslim/schemes/modelslim_w4a4_int4.py`
7. `python/sglang/srt/layers/quantization/modelslim/schemes/modelslim_w4a4_int4_moe.py`
8. `python/sglang/srt/hardware_backend/npu/quantization/linear_method_npu.py`

重点判断：

1. 当前模块是普通 linear 还是 FusedMoE
2. 当前 scheme 是静态还是动态
3. 最终调用落在通用 ModelSlim scheme，还是进一步落到 NPU quant kernel

#### 5.6.7 ModelSlim 的特殊补丁行为

从 `modelslim.py` 还能提炼出一条重要规则：

1. 若量化描述中出现 `norm.bias` 相关项，`ModelSlimConfig` 会对 `RMSNorm` 施加补丁
2. 对 NPU 路径，这会影响 `RMSNorm.forward_npu`，并引入带 bias 的量化相关实现

这意味着：

1. norm 是否量化、是否带 bias，不应只靠模型主文件判断
2. 若 `quant_model_description.json` 出现 `norm.bias` 相关项，应额外检查：
   - `layers/layernorm.py`
   - `layers/quantization/modelslim/modelslim.py`
   - 相关 NPU norm kernel

#### 5.6.8 Step5 读取 ModelSlim 文件的实操顺序

当发现 `quant_model_description.json` 存在时，建议按以下顺序读取：

1. 先看 `model_quant_type`
   - 获得全局概览
2. 再看当前目标模块的 `<prefix>.weight`
   - 判断该模块是否量化、属于哪种 scheme
3. 再看该模块是否存在：
   - `weight_scale`
   - `weight_offset`
   - `input_scale`
   - `input_offset`
   - `quant_bias`
   - `deq_scale`
4. 若是 MoE，再重点看：
   - `experts.*.gate_proj.weight`
   - `experts.*.up_proj.weight`
   - `experts.*.down_proj.weight`
5. 若模块值为 `FLOAT`，优先按未量化路径继续，但保留 BF16 常见事实，不要误判成 FP32 专用实现
6. 最后再转到 `sglang_path_map.md` 与 `forward_analysis_rules.md` 做真实代码行下钻

## 6. 字段到代码路径的缩圈规则

### 6.1 先判定模型与 draft 模型

当存在以下情况时，必须把主模型和 draft 模型拆开分析：

1. `speculative_algorithm` 非空
2. `speculative_draft_model_path` 非空
3. `model_config.is_draft_model` 相关逻辑被触发

缩圈原则：

1. 主模型 path 用于 `verify` 或非 speculative `decode graph`。
2. draft 模型 path 用于 `draft_prefill` 与 `draft_decode`。
3. 若当前 graph span 已从 profiling 证据确定属于 draft graph，不得再回主模型路径找代码。

### 6.2 先判定 attention 形态，再找 backend

缩圈顺序：

1. 先用 config 判断 attention 形态。
2. 再用启动参数判断具体 backend。
3. 再判断是否存在 NPU 特化实现。

禁止顺序：

1. 先看某个通用 attention 文件就直接定结论。
2. 没看 backend 字段就默认走标准实现。

### 6.3 先判定量化，再找具体算子路径

缩圈顺序：

1. 先确定是否量化。
2. 再确定量化作用于哪一层或哪类模块。
3. 再继续找最终调用的 quant / NPU / Triton 实现。

禁止顺序：

1. 先在未量化代码路径里找算子。
2. 找到普通 linear/matmul 就认为结束。

### 6.4 只要并行字段开启，就要检查通信路径

以下场景必须检查通信候选：

1. `tp_size > 1`
2. `dp_size > 1`
3. `ep_size > 1`
4. `moe_dp_size > 1`
5. `attn_cp_size > 1`
6. `enable_dp_attention = true`

这时 Step5 在 graph replay 内看到相关 span，必须优先把通信/collective/parallel state 路径列入候选，而不是硬套纯计算层。

## 7. 字段缺失时的保守推断

如果字段不完整，按以下顺序保守推断：

1. 先看 `architectures`
2. 再看 `model_type`
3. 再看显式启动参数
4. 再看模型文件中的类命名、子模块命名和 import 路径
5. 最后才允许根据通用模式作弱推断

禁止做法：

1. 仅因模型目录名像某一模型就直接套模板。
2. 仅因 graph span 名称出现 `decode` 或 `verify` 就完全跳过 config 判断。
3. 仅因看到某个 attention 类名就忽略 speculative / draft / quant / NPU 分支。

## 8. Step5 需要记住的硬规则

1. 启动命令与模型 config 只负责决定“去哪里找”，不直接等于最终 `code_location`。
2. 主模型和 draft 模型必须分开。
3. `eagle`、`eagle3`、`nextn` 都必须进入 speculative 决策树。
4. 对 `draft_prefill`、`draft_decode`，必须优先走 draft 模型路径。
5. attention、MoE、KV cache、communication 都可能是 graph replay 内合法定位对象。
6. NPU 场景不能只停留在模型文件或通用层文件，必须继续检查是否存在更深的 `hardware_backend/npu` 落点。
7. 若字段只能支持“代码区域 + 主行号”级别的判断，只能作为中间候选，不得当作最终通过结论。

## 9. 与其他知识文档的配合

本文件和另外两份知识文档的分工是：

1. `model_config_and_launch_fields.md`
   - 回答“字段说明了哪条分支”
2. `sglang_path_map.md`
   - 回答“当前分支去哪些目录和文件找”
3. `forward_analysis_rules.md`
   - 回答“找到模型文件后如何继续下钻到最终算子调用行”

因此本文件结束后，下一步动作应是：

1. 用字段判断出模型/阶段/backend/量化/cache/通信大分支。
2. 再转到 `sglang_path_map.md` 缩圈具体路径。
3. 最后转到 `forward_analysis_rules.md` 完成逐层下钻与最终落点判断。
