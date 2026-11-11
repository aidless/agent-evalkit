# aidless-agent-evalkit — 带评测门禁与全链路 trace 的 Tool-Calling Agent

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
run_eval.py ── 评分 + 失败归因 + 门禁 (阈值由 derive_gate.py 推导，退出码≠0 即阻断)
```

## 快速开始

```bash
# 安装（60 秒可跑通的那条在前面：当前唯一确定可用）
git clone https://github.com/aidless/agent-evalkit && cd agent-evalkit
# 或 pip install git+https://github.com/aidless/agent-evalkit  → 同一模块 evalkit.*
# PyPI 发行名 aidless-agent-evalkit 已打包（twine check 双 PASSED），
# 但新账号上传撞 429 冷启动限流，上线前不要写 `pip install aidless-agent-evalkit`。

python3 -c "from evalkit import tools; print(tools.calc('2+3*4'))"   # 14

# 60 秒离线复现主表：单测 + 门禁自检 + 已录 run 的门禁复算（零网络零 GPU）
python3 -m unittest discover -s tests && python3 run_eval.py --selftest
python3 run_eval.py --replay results/run-gw5.json   # → GATE 0.9167: PASS — pass_rate 0.9444

# 1. 跑全部评测任务（需要任意 OpenAI 兼容 API）
export OPENAI_BASE_URL=http://127.0.0.1:3425/v1
export OPENAI_API_KEY=sk-local
export AGENT_MODEL=opencode-go/deepseek-v4.1-flash

python3 run_agent.py --all                # 产出 traces/run-*.jsonl + results/latest.json

# 2. 评分 + 门禁（对已录 trace 回放评分，不调 LLM，CI 可用）
python3 run_eval.py --replay results/latest.json
#    阈值缺省读 results/gate-policy.json 的 36 题桶 = 0.9167；门禁不过 → exit 1

# 3. 观测摘要（吞吐/延迟分位/失败归因）
python3 trace_summary.py "$TRACE_FILE"   # 或指定某一 run 的 trace 文件
```

## 门禁自检（RED→GREEN）

门禁必须能失败，才算门禁（自检里固定用 0.80 做夹具；生产阈值由 `derive_gate.py` 推导，见下节）：

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

## 上生产怎么监控（阈值不是拍脑袋的 0.80）

**阈值来源（`derive_gate.py`，规则唯一且可复算）**：
`threshold = (最低历史通过题数 − safety_tasks) / total_tasks`，`safety_tasks=1`。
语义是「允许比历史最差一次再多错 1 条题，再劣化就阻断」。按题数分桶，
不让 10 题满分把 20/36 题的门禁抬高。当前 `results/gate-policy.json`：

| 题数桶 | 基线 run 数 | 历史最低通过 | 阈值 |
|---|---|---|---|
| 36 | 11（网关 6 + 本地 5） | 34 | 0.9167（不四舍五入，落盘为 0.9166666…） |
| 20 | 1 | 20 | 0.9500 |
| 10 | 2 | 10 | 0.9000 |

原来的 0.80 在 36 题下等于允许错 7 条，比历史最差（错 2 条）还多放 5 条——
是装饰不是门禁。`run_eval.py` 缺省读桶值，**桶不存在 / 报告损坏一律 exit 2**
（fail-closed）；显式 `--gate` 只用于复现历史实验。

**告警接入（`--watch`）**：每次评测往 `results/alerts.jsonl` 追加一行
（ts / run_id / model / pass_rate / passed / total / threshold / blocked /
reason / failure_attribution），越线同时打 `ALERT {...}` 到 stderr；
给 `--alert-url https://hooks.example/...` 则 POST 同一份 JSON。
退出码三态：`0` 通过、`1` 门禁阻断、`2` 输入损坏或**告警投递失败**
（`ALERT_DELIVERY_FAILED`，避免告警没送到却显示绿灯）。

```bash
# 发布流水线：跑 → 判 → 告警；CI 里把 ALERT_URL 设为 secret
python3 run_agent.py --all --out results/latest.json
python3 run_eval.py --replay results/latest.json --watch ${ALERT_URL:+--alert-url "$ALERT_URL"}
# 基线变好/变多后重推阈值（CI 里的 eval-gate.yml 每次都会重跑这一步）
python3 derive_gate.py --selftest && python3 derive_gate.py
```

**边界（别把它说成托管观测平台）**：没有存储后端、没有自动定时重跑、
没有告警去重/升级、阈值是回归线不是 SLO。这层就是「一次评测 → 一条可追溯
判定 → 一个可挂 webhook 的出口」；真正的调度交给 CI/cron，长期趋势交给
`aggregate_runs.py`。面试里被问「上生产怎么监控」，答这条链 + 这三个缺口。

## 实测数字（2026-11-09–11-10，全部数字可由落盘结果重算）

### v2 评测集（36 题 = 24 基础 + 12 难题）跨后端对照

24 题对强模型是满分（见下节 v1），**没有区分度**。v2 加了 12 道
难题（跨量纲/缺键/错键自纠/两步链/边界值/格式遵循/抗干扰长 prompt），
两个后端立刻分开，而且失败模式完全不同：

| 后端（`--max-tokens 2048`，温度 0） | 通过率 ×5 | mean±sd | LLM 延迟 p50 / p95 / p99 | 平均轮次 | token/次 | 全量耗时 | prefix-cache 命中率 |
|---|---|---|---|---|---|---|---|
| `opencode-go/deepseek-v4.1-flash`（网关） | 0.9444 / 1.0000 / 0.9444 / 0.9444 / 1.0000 | **0.9666 ± 0.0272** | 1583 / 2986 / 5473 ms | 2.60 | 70344 | ~170 s | —（无 /metrics） |
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
python3 run_eval.py --replay results/run-gw3.json     # 0.9444 PASS
python3 run_eval.py --replay results/run-local2.json   # 0.9722 PASS
python3 run_agent.py --all --model Qwen3-4B-AWQ --base-url http://127.0.0.1:8123/v1 \
    --api-key sk-local --max-tokens 2048 --out results/run-local-N.json
python3 report_cost.py --results results/run-local4.json \
    --power-csv <nvidia-smi 采样 csv> --wall-seconds 592.4
```

### 失败驱动的修复闭环（2026-11-10）

三个闭环，两个成功一个诚实的负结果（全文与三臂实验证据见
[docs/failure-loop.md](docs/failure-loop.md)，证据落盘 `results/failure-loop/`）：

- **闭环 1（成功）**：跨后端实测暴露两个 harness 真 bug（SyntaxError 逃出 dispatch、
  截断空转被当最终答案），RED→GREEN 修复，见上文 v2 节 ①②。
- **闭环 2（负结果）**：本地 `adv-05` 同一失败链 4/4 复现（模型在思考里心算 →
  畸形表达式 → 报错回灌 → 思考螺旋 2048 不闭合），两臂修复（prompt 反思重试、
  max_tokens 4096）均 0/3——这是 4B 对「禁心算」的指令遵循边界，不是 harness 缺陷；
  网关同题 ×3 全过佐证。价值=把「跑不过」变成可解释、可复现、带证据链的失败。
- **闭环 3（评审驱动）**：外部评审命中 trace `content[:300]` 截断破坏 replay
  确定性承诺 → `content_full` 全量落盘 + 回放优先取 full（`TestContentFullReplay`）。
  复现：`ARMS="baseline armA armB" bash run_failure_arms.sh`。

### audit 工具集（evalkit × tmaudit，2026-11-10）

第二套工具集 + 独立任务集，演示「同一套门禁评不同能力的 agent」：
`--toolset audit` 挂 3 个规则语义查询工具（`audit_rule`/`audit_check`/`audit_severities`），
规则事实（十类检查、三态证据层级 CONFIRMED/PARTIAL/UNSUBSTANTIATED、默认严重级）
逐字导出自 PyPI 在架包 tmaudit 0.5.1 的源码（`evidence.py` CHECK_EVIDENCE 表）。
任务集 2026-11-10 扩到 **20 题**（audit-11..20：默认严重级、证据层级、
    规则间对比）。跨后端对照（`--max-tokens 2048`）：

| 后端 | 通过率 | 主要失败 |
|---|---|---|
| 网关 flash（×3） | **20/20 = 1.0000** | — |
| 本地 Qwen3-4B-AWQ | **7/20 = 0.3500** | wrong_answer 8（其中 7 条是查表后归类错、4 次兜底答 C3；1 条未查先答）、思考截断 3（2048 想不完）、上下文溢出 1（刷遍 10 条规则后 prompt 2049 > max-model-len 2048）、超时 1 |

    **audit 集比工具集区分度更高**（工具集本地 0.9722 vs audit 集 0.35）：
    规则语义题要求模型先查规则再精确归类，4B 在 2048 token 思考预算内做不到——
    这条失败链本身（api_error 400 上下文溢出）还指出了 KV cache 2,224 tokens
    共存配置下 max-model-len 抬不高的部署约束。trace 纪律同样生效（含盲区）：
    audit-07 单题内刷遍 10 条规则（10 次 audit_rule、参数各不相同）把上下文撑过
    2048 直接 400——但 `dup_calls`（同 tool+同 args）读数为 0，**同参重复抓得住、
    非必要全表浏览抓不住**。13 条本地失败的逐条标注（四类分型 + 逐条根因）见
    [docs/error-analysis-13.md](docs/error-analysis-13.md)。
```bash
python3 run_agent.py --all --toolset audit --tasks-file evals/tasks-audit.jsonl --max-tokens 2048
python3 run_eval.py --replay results/run-audit-gw1.json --tasks-file evals/tasks-audit.jsonl
```

### mock-LLM 回放后端（harness 可测试性）

`--replay-trace <trace.jsonl>` 从轨迹重放 LLM 调用（`evalkit/replay.py`），
**不调任何模型**即可端到端跑通 run_agent → run_eval → trace_summary：
工具分派、错误回灌、截断守卫、评分全链在无 LLM、无 GPU 环境可验证。
确定性验收：网关 gw7（36/36 满分）重放后逐题一致（0 mismatch），
证据 `results/replay-demo/`（原 run + trace + replay run）。
trace 相应升级：`llm_call` 事件新增 `tool_calls_full`（id/name/arguments 全量），
旧 trace 无该字段时重放明确报错而不是静默失败。
```bash
python3 run_agent.py --all --replay-trace results/replay-demo/trace-gw7-evidence.jsonl
python3 run_eval.py --replay results/replay-demo/run-gw7-evidence.json
```

### prefix-cache 命中率（L3 最小件 + 一段两级归因弯路）

`--metrics-url http://127.0.0.1:8123/metrics` 在 run 前后抓
`vllm:prefix_cache_{queries,hits}_total` 差值算命中率（`evalkit/metrics.py`，
单元测试覆盖解析、整词匹配防 `_created` 误读、差值除零守卫）。
全量 36 题实测 **0.9178**（run-local6）——系统提示词 + 任务前缀被 KV cache
大量复用，这是顺序评测能省 prefill 的量化证据。

跟踪时走过一段两级归因弯路（冒烟 0 → 错怪计数器接线 → 错怪 LBNHC layout →
提了 [vllm#66786](https://github.com/vllm-project/vllm/issues/66786) →
最终定位为 block 粒度缓存的设计行为：不足 16-token 一个 block 的短 prompt
没有 block_hashes，永不命中；72-token 相同 prompt 第二次命中 89%）。
issue 已自答关闭（not_planned），完整复盘与教训见
[docs/incident-66786.md](docs/incident-66786.md)——核心教训：报 bug 前先做
「改变一个变量」的对照实验，长度就是那个变量。

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
`results/v1-24q/`（v1 全套）。CI 离线复算：`python3 run_eval.py --replay results/run-gw5.json`（v1 归档时阈值写死 0.80，现由策略文件推导）。

## 评测集溯源：题从真实失败长出来

`mine_failures.py` 从两个真实失败 run（本地 audit 13 条失败、`adv-05` 1 条）
按四种失败模式挖出 7 道带 `provenance` 的新题，标准答案全部由 calc / RULES
程序现算，`--selftest` 4/4。**结果是负结果**：网关与本地在 7 题上都 7/7——
单步查表题恰好绕开了 4B 模型的真实缺口（多步链 / 思考预算 / 上下文溢出），
所以它不是有效区分器。挖掘过程中还抓到一次「题面有歧义导致误判」的自身 defect。
完整表格、失败模式计数、下一轮待办见 [`docs/evalset-provenance.md`](docs/evalset-provenance.md)。

## English abstract

agent-evalkit is a zero-dependency (Python stdlib only) tool-calling agent harness
with a deterministic grader, a fail-closed eval gate, and per-event JSONL traces.
36 tasks (24 basic + 12 adversarial/multi-step) run against two backends
(a hosted deepseek-v4.1-flash and a local Qwen3-4B-AWQ served by vLLM with the
Hermes tool-call parser). Five gateway runs score 0.9666 ± 0.0272 with
run-to-run flakiness; four local runs score 0.9722 ± 0.0000, failing the
same task with the same failure chain every time. A full local eval costs
¥0.0051 of GPU electricity. Every number above is
recomputable offline from committed JSON results and traces.
## 设计取舍（为什么这套东西算 L2 而不是又一个 demo）

1. **评分是确定性的，不请 LLM 当裁判**：36 题都有可机读的 `expected_answer`，
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

这套东西的边界也说清楚：36 题是单轮工具调用，不含多 agent 编排、长期记忆和沙箱隔离
（那是 L3）；评测集小，pass_rate 满分只说明「这批任务没退化」，不能外推成能力结论。

## 目录

```
evalkit/          核心包（零第三方依赖）
  llm.py          OpenAI 兼容客户端：超时、指数退避重试、usage 解析
  tools.py        确定性工具集 + JSON Schema 注册表 + dispatch_via 统一异常包装
  audit_tools.py  audit 工具集（tmaudit 规则语义查询，10 条规则事实源）
  agent.py        Agent 循环：消息 → 工具调用 → 最终答案，轮次上限/截断守卫
  replay.py       mock-LLM 回放后端（按 trace 重放，无 LLM 无 GPU 跑全链）
  metrics.py      vLLM /metrics 抓取 + prefix-cache 命中率差值
  trace.py        JSONL trace 写入 + 事件模型
  grade.py        评分（数值容差/归一化字符串）+ 布尔失败归因
evals/
  tasks.jsonl     36 题：24 基础（8 calc / 8 lookup / 4 convert / 4 multi）
                  + 12 hard（对抗/多步）
  tasks-audit.jsonl 20 题 audit 工具集场景（--toolset audit）
  corpus.json     lookup 工具的冻结事实库
docs/             复盘与叙事：incident-66786 / failure-loop / error-analysis-13 /
                  interview-walkthrough / mock-interview / trace-timeline.html
run_agent.py      跑单题/全部 → traces + results/latest.json
run_eval.py       回放评分 + --gate 门禁 + --selftest
report_cost.py    从结果 JSON + 功率采样折电费成本
run_failure_arms.sh 负结果三臂复现脚本（尾部断言防止陈述过期）
trace_summary.py  聚合：延迟分位/token/工具分布/轮次/冗余调用 + 评分对账
trace_timeline.py 单文件 HTML 时间线（每任务一泳道，蓝条宽=LLM latency、
                  红线=error；零依赖，浏览器直开）：
                  python3 trace_timeline.py traces/run-*.jsonl --out docs/trace-timeline.html
aggregate_runs.py 多次运行的通过率 mean/sd + 延迟分位汇总
tests/            52 个单测（工具/评分器/trace 指标/归因矩阵/回放契约）
```
