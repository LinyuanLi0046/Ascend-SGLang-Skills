# Forward Analysis Rules

本文件是 Step5 `graph_path_analyst` 的知识附录，用于规定：在已经通过启动命令和模型 config 判断出模型、阶段、backend、量化与路径大类后，如何从模型 `forward` 一路下钻到 graph replay 内最终真实的算子调用行。

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

## 1. Step5 的最终任务

Step5 的目标不是“找到模型文件”或“知道 graph phase 名称”，而是：

1. 从 graph replay 内主模型或 draft 模型的 `forward` 起点出发。
2. 沿着实际调用链逐层下钻。
3. 把每段 span 落到真正触发 device 计算或通信的代码行。

本文件只处理 graph replay 内的路径重建，不展开运行时调度主链。

## 2. 总体下钻顺序

Step5 必须按以下顺序做路径重建：

1. 先确认当前 span 属于：
   - 非 speculative `decode graph`
   - speculative `verify`
   - speculative `draft_prefill`
   - speculative `draft_decode`
2. 再确认当前应使用：
   - 主模型
   - draft 模型
3. 再定位模型文件中的：
   - `ForCausalLM.forward`
   - `Model.forward`
   - `DecoderLayer.forward`
4. 再判断当前子路径属于：
   - attention
   - mlp
   - moe
   - kv cache
   - communication
5. 再继续判断是否存在：
   - quant 分支
   - NPU 分支
   - backend selector
   - `forward_npu`
   - `is_npu()` 分支
   - 更深的 `hardware_backend/npu/*`
6. 直到找到最终真实的 device op 调用行，才允许输出最终 `code_location`。

禁止跳步：

1. 不能只根据 graph phase 直接指向某个 runner。
2. 不能只根据模型文件中的子模块名字直接给出最终定位。
3. 不能在未判断主模型和 draft 模型前直接下钻。

## 3. 允许与禁止的定位层级

### 3.0 `location_kind` 判定规则

正式 graph span 对齐时，`location_kind` 必须按源码语义硬判定：

1. `operator_call`
   - 仅允许用于真实 device / tensor 运算调用行
   - 典型例子：
     - `torch.xxx(...)`
     - `torch.nn.functional.xxx(...)`
     - `torch.ops.npu.xxx(...)`
     - `torch_npu.xxx(...)`
     - Triton kernel 调用
     - 真正的张量计算、collective、cache 读写语句
2. `module_call_anchor`
   - 仅表示“已经找到语义模块入口，但还没到最终 op 行”
   - 典型例子：
     - `self.gate(...)`
     - `self.experts(...)`
     - `self.qkv_proj(...)`
     - `self.o_proj(...)`
     - `self.input_layernorm(...)`
     - `self.post_attention_layernorm(...)`
3. `graph_replay_entry`
   - 仅用于 `replay()` 或 graph runner 外层入口这类 phase / runtime 包装层位置
4. `constructor_line`
   - 仅用于 `self.xxx = SomeModule(...)`、registry 初始化、backend 构造这类注册/构造语句

硬规则：

1. `self.xxx(...)`、`module(...)`、`layer(...)` 这类 `nn.Module.__call__` 边界不得直接标成 `operator_call`
2. `.replay()`、graph runner、模型 `forward` 函数头、构造行都不得作为最终 `operator_call`
3. 若当前只到达 `module_call_anchor`、`graph_replay_entry` 或 `constructor_line`，必须令 `requires_further_drilldown=true`

### 3.1 允许作为最终 `code_location` 的落点

以下类型的代码行可以作为最终 `code_location`：

1. `torch.xxx`
2. `torch.nn.functional.xxx`
3. `torch.ops.npu.xxx`
4. `torch_npu.xxx`
5. Triton kernel 调用
6. 设备张量上的真实计算/重排/索引/聚合语句，例如：
   - `+`
   - `-`
   - `*`
   - `/`
   - `@`
   - `matmul`
   - `split`
   - `cat`
   - `reshape`
   - `view`
   - `gather`
   - `scatter`
   - `index`
7. 真正的 collective / communication 调用行

核心标准：

1. 这一行必须是“真正触发 device 计算、设备通信或设备 cache 读写语义”的语句。
2. 这一行必须比 `replay()`、wrapper、子模块调用点更接近实际执行。

### 3.2 不能作为最终 `code_location` 的落点

以下位置默认不能作为最终定位：

1. `ForCausalLM.forward`
2. `Model.forward`
3. `DecoderLayer.forward`
4. `self_attn(...)` / `mlp(...)` / `moe(...)` 的调用点
5. backend registry
6. `MultiPlatformOp.dispatch_forward`
7. `graph_runner/*`
8. speculative worker / runner 外壳
9. 只有“代码区域 + 主行号”的模糊段
10. 只有 phase 名称，没有具体 op 行

这些位置最多只能算：

1. 中间候选
2. 路径锚点
3. 继续下钻的起点

## 4. 模型层下钻规则

### 4.1 起点必须是模型 `forward`

无论是主模型还是 draft 模型，起点都应是其模型文件中的 `forward`。

优先级顺序：

1. `ForCausalLM.forward`
2. `Model.forward`
3. `DecoderLayer.forward`

使用规则：

1. `ForCausalLM.forward` 用于确认顶层调用链。
2. `Model.forward` 用于确认 layer 循环、embedding、norm、残差等整体结构。
3. `DecoderLayer.forward` 用于确认 attention / mlp / moe 的局部顺序。

禁止做法：

1. 停在 `ForCausalLM.forward`
2. 停在 layer 循环
3. 看到 `self_attn` 或 `mlp` 调用后不继续下钻

### 4.2 典型 decoder 下钻模板

对大多数 decoder-only 模型，可按以下模板下钻：

1. `ForCausalLM.forward`
   - 进入 `self.model(...)`
2. `Model.forward`
   - 进入 `embed_tokens`
   - 进入 layer 循环
   - 进入 `DecoderLayer.forward`
3. `DecoderLayer.forward`
   - `input_layernorm`
   - `self_attn`
   - `post_attention_layernorm`
   - `mlp`
4. 子模块 forward
   - 继续进入 attention / mlp / moe 的具体实现

注意：

1. 这只是下钻模板，不是最终定位。
2. 只有继续进入子模块内部，才可能找到最终 device op 行。

### 4.3 何时离开模型文件

出现以下情况时，应立刻离开模型主文件，进入子模块或层实现文件：

1. `self.self_attn(...)`
2. `self.mlp(...)`
3. `self.router(...)`
4. `self.experts(...)`
5. `self.attn(...)`
6. `self.qkv_proj(...)`
7. `self.o_proj(...)`
8. `self.input_layernorm(...)`
9. `self.post_attention_layernorm(...)`

规则：

1. 这些调用点说明语义模块已确定，但最终 op 还未确定。
2. 下一步必须去子模块定义所在文件继续找，不可停在模型文件。

## 5. attention 下钻规则

### 5.1 attention 的标准下钻顺序

当当前 span 语义属于 attention 时，按以下顺序下钻：

1. 从模型 attention 类进入
2. 找到：
   - `qkv_proj`
   - `rotary_emb`
   - `attn`
   - `o_proj`
3. 对每个子路径分别判断：
   - 是否已是最终 op
   - 是否只是新的子模块入口
4. 若只是入口，继续到对应层实现文件下钻

典型判断：

1. `qkv_proj(hidden_states)` 通常仍是入口，需要继续到 linear / quant / NPU 线性实现
2. `rotary_emb(...)` 通常仍是入口，需要继续到 rotary embedding 实现
3. `attn(q, k, v, forward_batch)` 通常仍是入口，需要继续到 attention backend / radix attention / NPU backend
4. `o_proj(attn_output)` 通常仍是入口，需要继续到 linear / quant / NPU 线性实现

### 5.2 attention 中哪些位置可以终止

仅当继续下钻后遇到以下类型位置，才允许终止：

1. 最终 attention backend 中真实调用的 device op
2. `torch_npu` / `torch.ops.npu` / Triton / torch 函数的真实调用行
3. 真正的 q/k/v 张量计算、变换、拼接、缩放、softmax、输出投影调用行

以下位置不能终止：

1. `self.attn(...)`
2. `RadixAttention(...)` 的构造点
3. backend registry 中 `"ascend"`、`"triton"`、`"flashinfer"` 的注册点
4. `attn_backend_wrapper(...)` 的 wrapper 层

### 5.3 MLA / NSA / 线性注意力

当字段已判断 attention 不是标准 GQA 路径时，必须：

1. 先进入对应 attention 子目录或 backend
2. 再按各自的内部模块继续下钻

具体规则：

1. MLA
   - 优先检查 MLA preprocess、MLA backend、MLA attention 特化实现
2. NSA / 稀疏注意力
   - 优先检查 `layers/attention/nsa/*` 或对应 backend
3. 线性注意力 / GDN / KDA / mamba-like
   - 优先检查 `layers/attention/linear/*`、`layers/attention/mamba/*`
   - 若 NPU 场景，则继续检查 `hardware_backend/npu/attention/*`

禁止做法：

1. 把所有 attention 都硬套标准 GQA 路径
2. 只因为类名里有 attention 就默认走通用 backend

## 6. MLP 下钻规则

### 6.1 标准 MLP 下钻顺序

当当前 span 语义属于 MLP 时，按以下顺序下钻：

1. 从模型文件中的 `mlp(...)` 调用进入 MLP 类
2. 定位：
   - `gate_proj`
   - `up_proj`
   - `down_proj`
   - 或合并的 `gate_up_proj`
   - activation / act_and_mul
3. 对每个子路径判断：
   - 是否普通 linear
   - 是否 quant linear
   - 是否 NPU linear
   - 是否 fused activation

### 6.2 激活与融合算子规则

对激活相关逻辑：

1. 若只看到 `self.act_fn(...)` 或类似包装点，不可结束。
2. 必须继续到激活实现文件中，检查最终是否触发：
   - `torch_npu.xxx`
   - `torch.xxx`
   - Triton kernel
   - fused kernel

典型模式：

1. `MultiPlatformOp.forward_npu`
2. `torch_npu.npu_swiglu`
3. `torch_npu.npu_geglu`
4. 其他 fused activation kernel

### 6.3 MLP 中哪些位置可以终止

以下位置可作为最终候选：

1. 最终 linear kernel 调用行
2. 最终 activation kernel 调用行
3. 张量融合运算真正发生的语句行

以下位置不能终止：

1. `self.mlp(...)`
2. `self.gate_up_proj(...)`
3. `self.down_proj(...)`
4. `self.act_fn(...)`

## 7. MoE 下钻规则

### 7.1 MoE 不能按普通 MLP 处理

只要字段或模型结构表明当前是 MoE，就必须单独按 MoE 路径下钻。

最少要区分：

1. router
2. top-k 选择
3. token dispatch
4. expert compute
5. expert communication
6. fused MoE / quantized MoE / NPU MoE

### 7.2 MoE 下钻顺序

1. 从模型文件中的 MoE block 进入
2. 先判断当前 span 更像：
   - router/topk
   - token dispatch
   - expert compute
   - expert communication
3. 再分别进入：
   - `layers/moe/router.py`
   - `layers/moe/topk.py`
   - `layers/moe/token_dispatcher/*`
   - `layers/moe/moe_runner/*`
   - `layers/moe/fused_moe_native.py`
   - `hardware_backend/npu/moe/*`
   - `hardware_backend/npu/quantization/*`

### 7.3 MoE 中哪些位置可以终止

1. 真正的 router/topk device op 行
2. 真正的 expert matmul / fused moe / quant moe 调用行
3. 真正的 expert dispatch / collective 调用行

以下位置不能终止：

1. 只看到 `experts(...)`
2. 只看到 router 包装层
3. 只看到 token dispatcher 的上层接口

## 8. LayerNorm / Elementwise / 基础算子下钻规则

### 8.1 规范化相关

当 span 语义属于 layernorm / rmsnorm / add+rmsnorm 等基础算子时：

1. 先从模型文件中的 `input_layernorm` / `post_attention_layernorm` 进入
2. 再进入 `layers/layernorm.py`
3. 判断最终实际走的是：
   - native
   - aiter
   - hip
   - NPU
4. 若是 NPU，继续定位到 `torch_npu.npu_add_rms_norm` 或 `torch_npu.npu_rms_norm` 这样的最终调用行

禁止停留点：

1. `self.input_layernorm(...)`
2. `self.post_attention_layernorm(...)`
3. 仅 `RMSNorm.forward_npu` 的函数头

### 8.2 elementwise / residual 路径

当 span 语义更像 residual add、简单 elementwise、tensor 变换时：

1. 优先进入基础层实现文件
2. 找真正的张量计算语句
3. 若只是高层 wrapper，继续往下找

可终止位置：

1. 实际的张量 `+ - * /` 行
2. 真正的 `torch.xxx` / `torch_npu.xxx` 计算行

## 9. NPU 分支下钻规则

### 9.1 NPU 三类分支识别顺序

当当前路径处于 NPU 场景时，按以下顺序判断：

1. 当前模块是否继承 `MultiPlatformOp`
2. 当前模块是否实现了 `forward_npu`
3. 当前文件是否有 `is_npu()` 条件分支
4. 当前模块是否通过 backend registry 切到 NPU backend
5. 是否存在更深的 `hardware_backend/npu/*` 特化实现

### 9.2 `forward_npu` 的处理规则

若模块定义了 `forward_npu`：

1. 默认把 `forward_npu` 视为比 `forward_native` 更高优先级的 NPU 路径
2. 进入 `forward_npu` 后，继续找其内部的真实 op 调用行
3. 若 `forward_npu` 只调用了另一个 helper 或 backend，不可结束
4. 仅当 `forward_npu` 内部出现真实 device op 调用，才允许结束

### 9.3 registry / wrapper 的处理规则

若通过 registry 或 wrapper 切到了 NPU backend：

1. registry 只说明“方向对了”
2. wrapper 只说明“包裹层确定了”
3. 必须继续进入：
   - NPU backend 的实际 forward
   - backend 内部调用的 helper
   - 最终的 `torch_npu` / `torch.ops.npu` / Triton / torch 行

## 10. 量化分支下钻规则

### 10.1 量化分支优先于普通实现

若字段和代码都表明当前模块走量化路径：

1. 先进入 quant 实现
2. 不要先在普通 linear / attention 里停留
3. 若 quant 实现继续转到 NPU quant，则继续进入 NPU quant

### 10.2 模块级量化

必须允许以下情况：

1. attention 量化
2. linear 量化
3. MoE 量化
4. KV cache 量化
5. draft 模型量化和主模型量化不同

因此规则是：

1. 每个子模块都单独判断量化落点
2. 不允许用“模型启用了量化”替代“这个子模块一定走同一量化实现”

## 11. speculative 与 draft 模型下钻规则

### 11.1 主模型 verify

当 span 属于 speculative `verify`：

1. 优先走主模型路径
2. 从主模型 `forward` 开始下钻
3. 继续按 attention / mlp / moe / cache / communication 规则下钻

### 11.2 draft_prefill 与 draft_decode

当 span 属于：

1. `draft_prefill`
2. `draft_decode`

规则必须是：

1. 切到 draft 模型路径
2. 重新从 draft 模型的 `ForCausalLM.forward` / `Model.forward` 开始
3. 再按 draft 模型自己的 attention / mlp / moe / quant / NPU 路径下钻

硬规则：

1. 不允许把 draft graph 错映到主模型文件
2. 不允许因为主模型和 draft 模型结构相似就直接复用主模型最终定位

### 11.3 `nextn` / `eagle` / `eagle3`

对三类 speculative 算法都必须适用同一总原则：

1. 先判断主模型还是 draft 模型
2. 再判断 graph phase
3. 再回到模型 forward 下钻

不能因为算法名不同就跳过这套决策框架。

## 12. KV cache 下钻规则

### 12.1 何时进入 cache 路径

当 span 语义像以下内容时，必须进入 cache 路径，而不是继续停留在 attention/模型文件：

1. cache allocate
2. cache append
3. cache write
4. cache read
5. radix cache
6. hicache
7. offload
8. page / block 管理

### 12.2 cache 下钻顺序

1. 先确认当前是主模型还是 draft 模型上下文
2. 再进入 `mem_cache/*` 或 `disaggregation/*`
3. 再找实际的：
   - tensor 读写
   - 内存块更新
   - cache backend 调用
   - offload 调用

以下位置不能终止：

1. cache manager 外壳
2. scheduler / worker 侧的 cache 调用入口
3. attention 模块中对 cache 的高层引用

## 13. communication 下钻规则

### 13.1 何时进入 communication 路径

当满足任一条件时，应把 communication 作为一级候选：

1. `tp_size > 1`
2. `dp_size > 1`
3. `ep_size > 1`
4. `moe_dp_size > 1`
5. `attn_cp_size > 1`
6. span 名称或语义像 collective / dispatch / sync / alltoall / allreduce

### 13.2 communication 下钻顺序

1. 先判断通信属于：
   - 通用 distributed
   - MoE token dispatch
   - DP/CP/NSA attention 通信
2. 再进入：
   - `distributed/*`
   - `layers/communicator*.py`
   - `layers/dp_attention.py`
   - `layers/moe/token_dispatcher/*`
   - `layers/moe/moe_runner/*`
3. 最终定位到真实 communication op 或 device communicator 调用行

以下位置不能终止：

1. 只在 `parallel_state.py` 看到组建逻辑
2. 只在 manager 层看到并行调度逻辑
3. 只在 token dispatcher 的外层接口处停止

## 14. 证据冲突时的裁决顺序

当出现多个候选路径时，按以下优先级裁决：

1. 与 graph phase 一致的模型归属
   - 主模型 / draft 模型
2. 与 config / launch 字段一致的 backend 与量化分支
3. 与当前 span 语义最一致的子模块
   - attention / mlp / moe / cache / communication
4. 更接近真实 device op 的代码行
5. 更少 wrapper、更少跳转、更少中间包装的路径

若多个候选都只能定位到上层包装：

1. 只能保留为中间候选
2. 不得冒充最终通过结果

## 15. 新架构 fallback 规则

遇到陌生新架构时，按以下顺序处理：

1. 先用 `architectures / model_type` 找到最可能的模型文件
2. 再确认该模型 `forward` 的层级结构
3. 再把子路径归类为：
   - attention
   - mlp
   - moe
   - cache
   - communication
4. 再按字段与代码事实判断：
   - quant
   - NPU
   - backend
   - speculative
5. 最后继续下钻到真实 op 行

不允许的 fallback：

1. 因为不认识模型就停在模型文件
2. 因为不认识 backend 就停在 registry
3. 因为没立刻找到最终 op 就退回 `replay()` 或 phase 描述

## 16. Step5 最终输出规则

Step5 最终给出的 `code_location` 必须满足：

1. 属于正确的模型归属
   - 主模型或 draft 模型
2. 属于正确的 graph phase 语境
3. 属于正确的子模块语义
   - attention / mlp / moe / cache / communication
4. 落在真实 device op 调用行

如果只能达到以下程度：

1. 只到模型文件
2. 只到 decoder layer
3. 只到子模块调用点
4. 只到 backend wrapper
5. 只到 graph runner
6. 只到“代码区域 + 主行号”

则必须视为中间候选，而不是最终有效 `code_location`。
