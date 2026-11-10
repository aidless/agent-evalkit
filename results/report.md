# 评测报告 — v2 评测集（36 题）跨后端对照与成本

日期：2026-11-10 · 全部数字可由落盘结果离线重算，命令见文末。

## 一、对照表（`--max-tokens 2048`，温度 0，每后端独立跑 3 次）

| 后端 | 通过率 ×3 | mean ± sd | LLM 延迟 p50 / p95 / p99 | 平均轮次 | token/次 | 全量耗时 | 冗余工具调用 |
|---|---|---|---|---|---|---|---|
| `opencode-go/deepseek-v4.1-flash`（Magpie 网关） | 0.9444 / 1.0000 / 0.9444 | 0.9629 ± 0.0321 | 1638 / 2772 / 4608 ms | 2.67 | 72737 | 171 s | 19 次 / 5 任务 |
| `Qwen3-4B-AWQ`（本机 RTX 3060，vLLM 0.33 + hermes parser） | 0.9722 / 0.9722 / 0.9722 | 0.9722 ± 0.0000 | 6663 / 18256 / 33102 ms | 2.08 | 72068 | 605 s | 1 次 / 1 任务 |

## 二、失败模式（每次红灯逐题）

| run | 失败题 | 归因 |
|---|---|---|
| gw3 | multi-01, adv-09 | loop_exceeded ×2 |
| gw4 | —（满分） | — |
| gw5 | multi-01, adv-03 | loop_exceeded + wrong_answer（答「是」而非 yes，格式遵循失败） |
| local2/3/4 | adv-05（三次同一题） | orchestration_error：工具报错后陷入思考螺旋，2048 token 未闭合被截断守卫拦下 |

- 网关 = **非确定性抖动**：三次红灯的题不完全相同，sd=0.0321。
- 本地 = **确定性失败**：三次同一题同一归因，sd=0.0000。
- 同一分数区间、两种稳定性画像——只有逐事件 trace + 固定优先级归因能把它们拆开。

## 三、成本（本地后端，口径：常驻 serving 窗口电费）

- 功率窗口均值 51.9 W（nvidia-smi 1 Hz 采样 594 行，与 local4 的 592 s 墙钟对齐）
- 0.008533 kWh × ¥0.6/kWh = **¥0.0051 / 次全量评测**（36 题）
- 折合 ¥0.0001 / 1k tokens；证据 `results/eval-cost-local4.json`
- 只算 GPU 电，不含整机/折旧/机房；网关侧 usage.cost 恒 0，无法给出对价数字。

## 四、可观测性发现

1. 冗余工具调用（`dup_calls`：同 tool + 同 arguments 重复）：网关 19 次（集中在
   loop_exceeded 的 multi-01 / adv-09 的空转轮），本地 1 次。pass_rate 接近，
   浪费差一个量级——这层信息只存在于 trace。
2. 两次 harness 修复来自跨后端实测：
   - `SyntaxError` 逃出 dispatch → 包成 ToolError 回灌自纠（本地模型会发 `"(1+2"`）；
   - 思考被 `max_tokens` 截断且无工具无答案 → 旧逻辑拿空串当答案，新守卫判失败。

## 五、门禁

- `--gate 0.80`：6 个 run 全部 PASS（0.9444 / 1.0000 / 0.9444 / 0.9722 ×3 均 ≥ 0.80）。
- 门禁自检 8/8；单测 34/34。

## 六、复现

```bash
python3 run_eval.py --replay results/run-gw3.json   --gate 0.80   # 0.9444 PASS
python3 run_eval.py --replay results/run-local2.json --gate 0.80  # 0.9722 PASS
python3 run_eval.py --selftest                                    # 门禁自检 8/8
python3 -m unittest discover -s tests                             # 34/34
python3 report_cost.py --results results/run-local4.json \
    --power-csv <1Hz 功率采样 csv> --wall-seconds 592.4           # ¥0.0051
python3 trace_summary.py traces/run-20261110T013405Z.jsonl --results results/run-gw3.json
```

原始证据：`results/run-{gw3,gw4,gw5,local2,local3,local4}.json`、
`results/eval-cost-local4.json`、`traces/run-*.jsonl`（结果 JSON 的 `trace_file`
字段逐 run 指向对应轨迹）、`results/v1-24q/`（v1 24 题全套归档）。
