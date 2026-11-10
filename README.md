# agent-evalkit — 带评测门禁与全链路 trace 的 Tool-Calling Agent

一个**零依赖**（Python 3.10+ 标准库）的 agent 演示项目：

```
用户 Prompt
   │
   ▼
┌────────────────────────────────────────────────┐
│ Agent Loop (agent.py)                           │
│  ├─ OpenAI 兼容 API (llm.py: 超时/重试/退避)     │
│  ├─ 工具注册表 (tools.py: calc/lookup/convert)  │
│  ├─ 轮次上限 / 错误兜底                          │
│  └─ JSONL Trace (trace.py: 每步延迟/用量/结果)   │
└────────────────────────────────────────────────┘
   │                                    │
   ▼                                    ▼
evals/tasks.jsonl (36 题)          traces/run-*.jsonl (可回放)
   │                                    │
   ▼                                    ▼
run_eval.py ── 评分 + 失败归因 + 门禁 (gate 0.80, 退出码≠0 即阻断)
```

## 快速开始

```bash
# 1. 跑全部评测任务（需要任意 OpenAI 兼容 API）
export OPENAI_BASE_URL=http://127.0.0.1:3425/v1
export OPENAI_API_KEY=sk-local
export AGENT_MODEL=opencode-go/deepseek-v4.1-flash

python3 run_agent.py --all                # 产出 traces/run-*.jsonl + results/latest.json

# 2. 评分 + 门禁（对已录 trace 回放评分，不调 LLM，CI 可用）
python3 run_eval.py --replay results/latest.json --gate 0.80
#    门禁不过 → exit 1

# 3. 观测摘要（吞吐/延迟分位/失败归因）
python3 trace_summary.py "$TRACE_FILE"   # 或指定某一 run 的 trace 文件
```

## 门禁自检（RED→GREEN）

门禁必须能失败，才算门禁：

```bash
python3 run_eval.py --selftest
# [PASS] GREEN 合规报告放行 pass_rate 0.9167 ≥ 门禁 0.80
# [PASS] RED 违规报告阻断 pass_rate 0.5000 < 门禁 0.80
# [PASS] GREEN 压线 0.80 放行 pass_rate 0.8000 ≥ 门禁 0.80
# [PASS] RED 坏报告阻断（缺 pass_rate） 报告缺少 pass_rate/passed/total 字段
# [PASS] RED 坏报告阻断（pass_rate 非数值） pass_rate 不是数值
# [PASS] RED 坏报告阻断（pass_rate 越界） pass_rate 越界: 1.5
# [PASS] RED 坏报告阻断（total 为 0） total 为 0，无有效评测
# [PASS] RED 坏报告阻断（非 JSON 对象） 报告不是 JSON 对象
# SELFTEST PASS: 8/8 通过
```

真流量反证另跑一次（`--max-turns 1`，见下）。另外回放遇到损坏的结果 JSON 是 exit 2，
和上面的「坏报告 fail-closed」是两层不同的防线。

## 实测数字（2026-11-09–11-10，全部数字可由落盘结果重算）

### v2 评测集（36 题 = 24 基础 + 12 难题）跨后端对照

24 题对强模型是满分（见下节 v1），**没有区分度**。v2 加了 12 道
难题（跨量纲/缺键/错键自纠/两步链/边界值/格式遵循/抗干扰长 prompt），
两个后端立刻分开，而且失败模式完全不同：

| 后端（`--max-tokens 2048`，温度 0） | 通过率 ×4 | mean±sd | LLM 延迟 p50 / p95 / p99 | 平均轮次 | token/次 | 全量耗时 | prefix-cache 命中率 |
|---|---|---|---|---|---|---|---|
| `opencode-go/deepseek-v4.1-flash`（网关） | 0.9444 / 1.0000 / 0.9444 / 0.9444 | **0.9583 ± 0.0241** | 1572 / 2881 / 4949 ms | 2.62 | 71004 | ~160 s | —（无 /metrics） |
| `Qwen3-4B-AWQ`（本机 3060，vLLM 0.33 + hermes parser） | 0.9722 ×4 | **0.9722 ± 0.0000** | 6663 / 17756 / 32597 ms | 2.08 | 72068 | ~590 s | **0.9178**（run-local6 实测） |

- **区分度结论**：v1 的 24/24=1.0 是评测集天花板太低；v2 让两个后端都 <1.0，
  且失败模式完全不同。网关是**非确定性抖动**：4 次全量红灯的题各不相同
  （gw3: multi-01+adv-03；gw5: multi-01+adv-09；gw6: lookup-01+lookup-08），
  而同一网关跑 hard 子集 ×3 = 36/36 零抖动（results/hard-x3/）——抖动集中在
  基础题的循环空转上。本地是**确定性失败**：`adv-05` 在 4/4 次全量里同一处卡死
  （工具报错 → 思考螺旋 2048 token 想完未闭合，被截断守卫判 orchestration_error，
  run-local6 错误原文：「输出被 max_tokens=2048 截断且无工具调用（思考未闭合，
  finish_reason=length）」）。同一分数区间、两种稳定性画像，只有 trace + 归因能拆开。
  本地 adv-05 失败本身是下一个修复闭环的输入（见「失败驱动的修复闭环」节）。
- **冗余调用**（`dup_calls`：同 tool + 同 arguments 重复出现）：网关 19 次（多发生在
  循环空转的 `multi-01`/`adv-09`），本地只有 1 次——pass_rate 接近但浪费差一个量级。
- **本地成本**（口径：常驻 serving 窗口电费，¥0.6/kWh，只算 GPU 电）：
  51.9 W × 592 s = 0.00853 kWh = **¥0.0051 / 次全量评测**（¥0.0001/1k tokens）。
  本地跑评测几乎免费，代价是 p99 延迟 33 s、慢 3.5×。
- **本地部署是共存配置**（不是卡片基准的 0.90）：与另一个 Ollama 进程共享 6 GB 卡，
  `--gpu-memory-utilization 0.585`，KV cache 只有 2,224 tokens、并发上限 1.09x——
  顺序评测够用，压测数字以 vllm-serving-card 为准。
- **两次 harness 修复都来自跨后端实测**（这也是跑第二个后端的价值）：
  ① 本地模型会发括号不平衡的表达式 `"(1+2"`，`SyntaxError` 逃出 dispatch 把任务打成
  orchestration_error → 已包进 `ToolError` 回灌自纠（`TestDispatchSyntaxError`）；
  ② 思考被 `max_tokens` 截断、无工具无答案的轮次，旧逻辑拿空串当最终答案 →
  新守卫判失败并注明原因（`TestAgentTruncatedTurn`）。

复现（评分与对照可离线重算）：
```bash
python3 run_eval.py --replay results/run-gw3.json --gate 0.80   # 0.9444 PASS
python3 run_eval.py --replay results/run-local2.json --gate 0.80  # 0.9722 PASS
python3 run_agent.py --all --model Qwen3-4B-AWQ --base-url http://127.0.0.1:8123/v1 \
    --api-key sk-local --max-tokens 2048 --out results/run-local-N.json
python3 report_cost.py --results results/run-local4.json \
    --power-csv <nvidia-smi 采样 csv> --wall-seconds 592.4
```

### 失败驱动的修复闭环（2026-11-10，含一个诚实的负结果）

**闭环 1（成功）：跨后端实测 → 两个 harness 真 bug → RED→GREEN 修复 → 复评。**
① 本地模型会发括号不平衡的表达式 `"(1+2"`，`SyntaxError` 逃出 dispatch 把任务打成
orchestration_error → 包进 `ToolError` 回灌自纠（`TestDispatchSyntaxError`）；
② 思考被 `max_tokens` 截断、无工具无答案的轮次，旧逻辑拿空串当最终答案 →
新守卫判失败并注明原因（`TestAgentTruncatedTurn`）。修复后全量复评无回归。

**闭环 2（负结果，如实报告）：本地 `adv-05` 确定性失败，两臂修复均未奏效。**
失败链（trace 逐事件，traces/run-20261110T092217Z.jsonl）：模型在思考里心算嵌套
表达式（违反规则 1）→ 发出括号不平衡的 `calc("( (((12+8)*5)-17)*3+9)/6"` →
工具报错回灌 → 模型继续在思考里重推整个表达式 → 2048 token 想完未闭合，
被截断守卫判 orchestration_error（错误原文带 finish_reason=length）。
- 基线（2048）：4/4 次全量同一处失败（run-local2/3/4/6）。
- arm-B（system prompt 加「报错后一句反思立即重试」）：0/3，失败链相同。
- arm-A（max_tokens 4096 放宽预算）：0/3，且更糟——思考在第一轮就不收敛，
  连工具都没调用（turns=1, finish=length）。
- **结论**：这是 4B 模型对「禁心算」指令遵循的能力边界，不是 harness 缺陷；
  门禁的归因与守卫把失败定位到了确切一步（哪一轮、什么 finish_reason、
  工具报错文本是什么）——修复闭环的价值在这里是**把「跑不过」变成可解释、
  可复现、带证据链的失败**，而不是硬把它变绿。网关后端同一题 ×3 全过
  （results/hard-x3/），对照进一步支持是模型能力而非任务缺陷。
  三臂证据已落盘 `results/failure-loop/`（结果 JSON + 对应 trace），复现：
  `ARMS="baseline armA armB" bash run_failure_arms.sh`（需本地 vLLM 服务；
  脚本尾部的断言会在任一臂通过时报错，防止负结果陈述过期）。

### audit 工具集（evalkit × tmaudit，2026-11-10）

第二套工具集 + 独立任务集，演示「同一套门禁评不同能力的 agent」：
`--toolset audit` 挂 3 个规则语义查询工具（`audit_rule`/`audit_check`/`audit_severities`），
规则事实（十类检查、三态证据层级 CONFIRMED/PARTIAL/UNSUBSTANTIATED、默认严重级）
逐字导出自 PyPI 在架包 tmaudit 0.5.1 的源码（`evidence.py` CHECK_EVIDENCE 表）。
网关 ×2：`run-audit-gw1/gw2` 均 **10/10 = 1.0000**，门禁 PASS，42 次 LLM 调用
p50=2245 / p99=8399 ms。trace 纪律同样生效：audit-01 单题内出现 10 次同参
`audit_rule` 调用（模型刷遍规则表），被 `dup_calls` 指标显形。
```bash
python3 run_agent.py --all --toolset audit --tasks-file evals/tasks-audit.jsonl --max-tokens 2048
python3 run_eval.py --replay results/run-audit-gw1.json --tasks-file evals/tasks-audit.jsonl --gate 0.80
```

### prefix-cache 命中率（L3 最小件）

`--metrics-url http://127.0.0.1:8123/metrics` 在 run 前后抓
`vllm:prefix_cache_{queries,hits}_total` 差值算命中率（`evalkit/metrics.py`，
单元测试覆盖解析、整词匹配防 `_created` 误读、差值除零守卫）。
实测读数：单题冒烟时计数器为 0（窗口内零查询 → 守卫记 null，不编 0）；
全量 36 题实测 **0.9178**（run-local6）——系统提示词 + 任务前缀被 KV cache
大量复用，这是顺序评测能省 prefill 的量化证据。两次读数都真实，
差别写明不猜原因。

### v1 评测集（24 题，2026-11-09，`--max-tokens 512`，已归档 `results/v1-24q/`）

后端 `opencode-go/deepseek-v4.1-flash`（Magpie 网关），控制机 RTX 3060 Laptop 6GB，
独立跑 3 次，`python3 aggregate_runs.py results/v1-24q/run-*.json` 重算：

| run | 通过率 | 通过/总数 | LLM 延迟 p50 | p99 | 平均轮次 | token |
|---|---|---|---|---|---|---|
| run-1 | 1.0000 | 24/24 | 1876 ms | 4671 ms | 2.29 | 38811 |
| run-2 | 1.0000 | 24/24 | 1813 ms | 4304 ms | 2.33 | 40819 |
| run-3 | 1.0000 | 24/24 | 1753 ms | 4063 ms | 2.25 | 37680 |

- **门禁反证（真流量注入）**：`--max-turns 1` 跑同一评测集 → 0/24，
  失败归因 `loop_exceeded: 24`，门禁输出 `BLOCK — pass_rate 0.0000 < 门禁 0.80` 且 exit 1。
- **可观测性的价值（真实案例）**：`lookup-08` 三次都答对，但每次都在前几轮重复
  调用同一工具同一参数才收尾——`dup_calls` 实测 3 / 4 / 2 次，
  占各自总工具调用 31 / 32 / 30 次的 9.7% / 12.5% / 6.7%。pass_rate 看不见这层浪费。

原始证据：`results/run-{gw3,gw4,gw5,local2,local3,local4}.json` + `traces/run-*.jsonl`（每 run 一条，
结果 JSON 的 `trace_file` 字段指向对应轨迹）、`results/eval-cost-local4.json`、
`results/v1-24q/`（v1 全套）。CI 离线复算：`python3 run_eval.py --replay results/run-gw5.json --gate 0.80`。

## English abstract

agent-evalkit is a zero-dependency (Python stdlib only) tool-calling agent harness
with a deterministic grader, a fail-closed eval gate, and per-event JSONL traces.
36 tasks (24 basic + 12 adversarial/multi-step) run against two backends
(a hosted deepseek-v4.1-flash and a local Qwen3-4B-AWQ served by vLLM with the
Hermes tool-call parser). Three runs per backend: the hosted model scores
0.9629 ± 0.0321 with run-to-run flakiness and 19 redundant tool calls,
the local model 0.9722 ± 0.0000 with one deterministic failure and 1 redundant
call. A full local eval costs ¥0.0051 of GPU electricity. Every number above is
recomputable offline from committed JSON results and traces.
## 设计取舍（为什么这套东西算 L2 而不是又一个 demo）

1. **评分是确定性的，不请 LLM 当裁判**：24 题都有可机读的 `expected_answer`，
   数值题整数精确相等、浮点题给 1e-3 容差，字符串题只做大小写/空白归一化。
   `required_tools` 让「蒙对答案但没走工具」判失败 —— 评测的是 agent 行为，不是答案本身。
2. **失败归因先于分数**：`api_error / orchestration_error > loop_exceeded > no_answer >
   tool_missing > wrong_answer` 的固定优先级，让一次红灯能直接指到该修的层；
   `--max-turns 1` 的反证把整批归因成 `loop_exceeded: 24`，就是这条规则的实测。
3. **门禁 fail-closed**：报告缺字段、`pass_rate` 非数值/越界、`total=0` 一律阻断，
   不因为「看起来跑完了」就放行。回放评分不调 LLM，CI 里零 secret 也能复算。
4. **trace 是逐事件落盘**（每条 flush）：`llm_call` 带 latency/attempts/usage、
   `tool_call`/`tool_result` 带 arguments、`final`/`error` 带结论。
   有了它才能事后回答「钱花在哪一题」「哪一题在空转」，而不是重跑一遍猜。
5. **成本与轮次是显式预算**：`max_turns=6`、单次 `timeout=60s`、`max_retries=3`
   加指数退避，工具执行错误统一包成 `ToolError` 回灌给模型自纠；工具层用 AST 白名单
   算表达式（`eval` 不进代码），量纲不同的单位拒绝换算。

这套东西的边界也说清楚：24 题是单轮工具调用，不含多 agent 编排、长期记忆和沙箱隔离
（那是 L3）；评测集小，pass_rate 满分只说明「这批任务没退化」，不能外推成能力结论。

## 目录

```
evalkit/          核心包（零第三方依赖）
  llm.py          OpenAI 兼容客户端：超时、指数退避重试、usage 解析
  tools.py        确定性工具集 + JSON Schema 注册表
  agent.py        Agent 循环：消息 → 工具调用 → 最终答案，轮次上限
  trace.py        JSONL trace 写入 + 事件模型
  grade.py        评分（数值容差/归一化字符串）+ 失败归因
evals/
  tasks.jsonl     24 题：8 calc / 8 lookup / 4 convert / 4 multi-step
  corpus.json     lookup 工具的冻结事实库
run_agent.py      跑单题/全部 → traces + results/latest.json
run_eval.py       回放评分 + --gate 门禁 + --selftest
trace_summary.py  聚合：延迟分位/token/工具分布/轮次/冗余调用 + 评分对账
aggregate_runs.py 多次运行的通过率 mean/sd + 延迟分位汇总
tests/            22 个单测（工具/评分器/trace 指标/自检分母）
```
