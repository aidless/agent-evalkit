# 失败驱动的修复闭环（2026-11-10）

> agent-evalkit 的核心卖点不是「全绿」，是「红灯可以解释」。本文记录两个闭环：
> 一个成功的（harness 真 bug 修复）和一个诚实的负结果（模型能力边界，修不动）。
> 三臂证据落盘 `results/failure-loop/`，复现：`ARMS="baseline armA armB" bash run_failure_arms.sh`
> （需本地 vLLM 服务；脚本尾部断言会在任一臂通过时报错，防止负结果陈述过期）。

## 闭环 1（成功）：跨后端实测 → 两个 harness 真 bug → RED→GREEN 修复 → 复评

跨后端对照的价值在跑第二个后端的那一刻兑现——两个 bug 都是本地模型先踩出来的：

1. **SyntaxError 逃出 dispatch**：本地模型会发括号不平衡的表达式 `"(1+2"`，
   `ast.parse` 抛 `SyntaxError`，旧 `except` 列表没接住，异常逃出工具层把整个任务
   打成 `orchestration_error`。修法：包进 `ToolError` 回灌给模型自纠
   （`evalkit/tools.py` 的 except 列表 + `TestDispatchSyntaxError`）。
2. **截断空转被当最终答案**：思考被 `max_tokens` 截断、无工具调用且剥掉
   `<think>` 后内容为空的轮次，旧逻辑拿空串当最终答案收尾。修法：新守卫判
   `orchestration_error` 并注明 finish_reason=length（`TestAgentTruncatedTurn`）。

修复后全量复评无回归（网关 0.9666±0.0272 不变）。

## 闭环 2（负结果，如实报告）：本地 `adv-05` 确定性失败，两臂修复均未奏效

失败链（trace 逐事件，`traces/run-20261110T092217Z.jsonl`）：模型在思考里心算嵌套
表达式（违反系统提示规则 1）→ 发出括号不平衡的 `calc("( (((12+8)*5)-17)*3+9)/6"` →
工具报错回灌 → 模型继续在思考里重推整个表达式 → 2048 token 想完未闭合，
被截断守卫判 `orchestration_error`（错误原文带 finish_reason=length）。

| 臂 | 改动 | 结果 |
|---|---|---|
| 基线（max_tokens 2048） | — | 4/4 次全量同一处失败（run-local2/3/4/6） |
| arm-B | system prompt 加「报错后一句反思立即重试」 | 0/3，失败链相同 |
| arm-A | max_tokens 4096 放宽预算 | 0/3，且更糟——思考第一轮就不收敛，连工具都没调用（turns=1, finish=length） |

**结论**：这是 4B 模型对「禁心算」指令遵循的能力边界，不是 harness 缺陷。
门禁的归因与守卫把失败定位到了确切一步（哪一轮、什么 finish_reason、工具报错
文本是什么）——修复闭环的价值在这里是**把「跑不过」变成可解释、可复现、
带证据链的失败**，而不是硬把它变绿。网关后端同一题 ×3 全过
（`results/hard-x3/`），对照进一步支持是模型能力而非任务缺陷。

## 闭环 3（本轮新增，评审驱动）：trace 截断破坏 replay 确定性承诺

外部评审（Carmack 视角）命中：trace 落盘时 `content[:300]` 截断，回放后端
（`evalkit/replay.py`）拿截断 content 重放——最终答案超过 300 字符的任务，
replay 评分与原 run 必然不一致，而 replay 的承诺恰恰是「确定性回归」。
修法：trace 新增 `content_full`（全量原文），replay 优先取 full，旧 trace 降级
截断并在 docstring 注明失真条件（`TestContentFullReplay` 两条：落盘保全量、
长答案 replay 与原 run 逐字一致）。
