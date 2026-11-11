# 事件复盘：vllm#66786 —— 一次误报的两级归因（2026-11-10）

> 这是 agent-evalkit 开发中一段真实的归因弯路，留档价值在于两级错误的成因各不相同，
> 且修第二级靠的是「单变量对照实验」这个普通纪律。上游 issue
> [vllm-project/vllm#66786](https://github.com/vllm-project/vllm/issues/66786) 已自答关闭（not_planned）。

## 第一级：流量归因错误（读数没错，归因错了）

跟踪 prefix-cache 命中率时，单题冒烟读到 `vllm:prefix_cache_{queries,hits}_total` 恒 0，
第一反应是「V1 引擎计数器未接线」——**归因错误**：当时本地服务确实零流量
（此前 40+ 请求全部打到了网关，本地服务从未被请求过），0 是正确读数。
两次读数的差异是流量去向，不是计数器。

## 第二级：「相同 prompt 永不命中」其实是设计行为

fresh 重启服务后逐次请求干净复现，发现更大的问题：**字节相同的连续 prompt 也永不命中**
（queries 增长、hits 恒 0，`/v1/completions` 与 `/v1/chat/completions` 皆然）。

排查时排除了两条路径：

- `FullAttentionSpec.prefix_cacheable` 默认 True（`vllm/v1/kv_cache_interface.py`）；
- fine-grained 禁用告警（`kv_cache_utils.py` 的 "Disabling fine-grained prefix-cache hits"）未触发。

当时怀疑对象指向 0.33 新引入的 LBNHC KV cache layout（`utils.py` 日志行）与
FLASH_ATTN backend，据此提了 issue（含环境、repro、观察值、排除清单）。

**终局（在 venv 内给 `kv_cache_manager.get_computed_blocks` 打临时调试日志后定位）**：
上游 issue 是**误报**。真相是那些复现 prompt 只有 13 tokens——

- `request_block_hasher` 只哈希**完整 block**（`kv_cache_utils.py` 的
  "We only hash full blocks"，逐 token 循环里 `if end_token_idx > num_tokens: break`）；
- `get_computed_blocks` 在 prefill 调度时执行，早于 decode 延长请求——
  不足一个 block（16 tokens）的 prompt 在 lookup 时 `block_hashes` 恒为空，
  永远不可能命中。

这是 block 粒度 prefix caching 的**设计行为**，与 LBNHC layout / FLASH_ATTN 无关。

决定性实验（同一服务同一 layout）：~72 token 的相同 prompt 连发两次，
第二次命中 **64 tokens**（命中率 89%）。**长度就是那个变量。**

## 教训

1. 报 bug 前先做「改变一个变量」的对照实验——这次变量是 prompt 长度，
   13 tokens vs 72 tokens 一次就分开了。
2. 归因要分层：第一层（谁在发流量）错了会污染第二层（机制是什么）的判断。
3. 复盘时撤销比坚持值钱：issue 自答关闭、README 同步改写、作品集页撤下展示，
   「已自证关闭」的复盘比「已报上游」的战绩更能说明评测纪律。

## 与本仓读数的关系

评测 run 的 0.9178 命中率（run-local6）与该现象不冲突：评测任务的系统提示词
远超一个 block，块级复用正常生效；只有不足一个 block 的短 prompt 才会恒 0。
