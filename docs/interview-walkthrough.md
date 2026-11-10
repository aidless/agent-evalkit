# agent-evalkit · 15 分钟面试 walk-through

目标读者：面 AI 应用/Agent 工程岗的面试官。全部数字可在仓库里离线重算（命令见 README「复现」节）。

## 0. 一句话

> 我给自己的 agent 建了一条「评测 → 门禁 → trace → 失败归因 → 修复 → 复评」的闭环，
> 36 题工具调用评测集、fail-closed 回放门禁、逐事件 JSONL trace，
> 用它抓出过自己 harness 的 2 个真 bug，并把本地 4B 模型的评测成本压到 ¥0.0051/次。

## 1. 问题（30 秒）

Agent 上线后你怎么知道它没退化？多数答案是"人工看几条"。这套东西的答案是：
把「工具调用是否正确、失败属于谁」变成机器可判定的门禁，回归时一次命令出裁决。

## 2. 架构（2 分钟）

```
tasks.jsonl (36 题)──► run_agent ──► results/*.json ──► run_eval --gate ──► PASS/BLOCK
      │                  │    ▲              │
      │                  ▼    │              ▼
      │            LLM API（网关 / 本机 vLLM）   aggregate_runs（跨 run 对比）
      │                  │
      │                  ▼
      └──────► traces/run-*.jsonl（逐事件：llm_call/tool_call/tool_result/final/error）
                            │
                            ▼
                      trace_summary（延迟分位/token/冗余调用 dup_calls）
```

- 工具集：`calc`（AST 白名单，不是 eval）/ `convert`（跨量纲拒算）/ `lookup`（语料查询）；
  另有 `audit` 工具集（tmaudit C1–C10 规则语义查询，10 题独立任务集，网关 ×2 全对）。
- 评分**不请 LLM 当裁判**：期望值比对 + 整数答案精确相等（4717 和 4716 不许容差放过）。

## 3. 三个关键决策（每个 1 分钟）

**① 门禁 fail-closed。** 报告缺字段/非数值/越界/total=0 一律判 FAIL（exit 1）。
理由：门禁的价值在「能红」；一个会静默放行的门禁是装饰。自检含 8 条断言，
其中 5 条专门注入坏报告验证必红。

**② 失败按固定优先级归因。** api_error/orchestration_error > loop_exceeded >
no_answer > tool_missing > wrong_answer。归因让「0/24」变成「24 个 loop_exceeded」——
max-turns=1 反证实验里，门禁 BLOCK 的同时归因直接指出是轮数预算被砍。

**③ trace 是逐事件落盘的。** 每条 flush。pass_rate 看不见的浪费在这里显形：
对照表里两个后端 pass_rate 接近，但 trace 显示网关后端有 19 次冗余工具调用
（同工具同参数重复调用）分布 5 个任务，本地只有 1 次。

## 4. 数字速查（面试现场可直接引用）

| 项 | 数字 | 载体 |
|---|---|---|
| 评测集 | 36 题 = 24 基础 + 12 对抗（除零/溢出/跨量纲/歧义键/抗干扰） | evals/tasks.jsonl |
| 网关后端 ×3 | pass_rate 0.9629 ± 0.0321（非确定抖动，每次红灯的题不同） | results/run-gw3..5.json |
| 本地 vLLM ×3 | 0.9722 ± 0.0000（失败确定性复现，同一题同一归因） | results/run-local2..4.json |
| 失败画像差异 | 同分数区间、两种稳定性画像——只有 trace 能拆开 | traces/*.jsonl |
| harness 真 bug | 2 个（SyntaxError 逃出 dispatch；截断轮空串当答案），TDD 修复 | tests/ 42 条 |
| 单次全量评测成本 | ¥0.0051（51.9W × 592s × ¥0.6/kWh，只算 GPU 电） | results/eval-cost-local4.json |
| 门禁反证 | --max-turns 1 → 0/24，BLOCK，exit 1 | results/v1-24q/regression-*.json |

## 5. 三个必追问（预演）

**Q1：为什么不用 LLM 当裁判？**
裁判本身会抖动，你就需要评测裁判的评测——回归到无穷。我的任务是工具调用
（算术/换算/查键），期望值可枚举，确定性评分免费且可复核。LLM 裁判适合开放
生成场景，那是另一个工程，这里刻意不做（边界声明在 README）。

**Q2：怎么证明评测集本身有区分度，不是随便都满分？**
v1 是 24 题、两个后端都 24/24——我自己先承认它没有区分度，归档后扩到 36 题
（对抗/多步类）。v2 里两个后端都 <1.0 且失败模式不同：网关是抖动（同一题这次过
下次不过），本地是确定性失败（同一题每次同样卡死）。这正是"分数接近、trace 分开"。

**Q3：修复了什么真问题？（失败→修复→复评的实例）**
跨后端实测抓出两个 harness bug：本地模型会发括号不平衡的表达式 `"(1+2"`，
`ast.parse` 抛 SyntaxError 逃出 dispatch，任务被误判 orchestration_error——修法是
把 SyntaxError 包成给模型可读的 ToolError；另一个是 max_tokens 截断轮的空串被当
成最终答案——修法是截断守卫。都是 RED→GREEN（先写会红的测试再修），修复后
复评全量并和旧 run 对比确认无回归。

## 6. 边界（主动交代，防追问失分）

单轮工具调用 + 门禁 + trace；**不含**多 agent 编排、记忆、沙箱（L3 大件），
不含 SFT/RL 自训（L5）。每格做完的事都有数字，没做的事写明没做。
