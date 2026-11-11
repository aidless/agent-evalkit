# 评测集溯源：题从哪来（不是为分数好看造题）

> 起因：Eugene Yan 评审指出「36 题的来源链反了——为分数好看设计评测集，
> 而不是从真实失败长出来」。本文给出可复算的反证链：每一道新题都挂在一个**真实失败实例**上，
> 带 `provenance`，标准答案由程序现算，挖题脚本 `mine_failures.py` 自带 `--selftest`（4/4）。

## 一、源失败（落盘载体）

| 源 run | 后端 | 题集 | 失败数 | 失败画像 |
|---|---|---|---|---|
| `results/run-audit-local1.json` | 本地 Qwen3-4B-AWQ | `evals/tasks-audit.jsonl`（20 题） | 13 | wrong_answer 8 / orchestration_error 3 / api_error 2（详见 `docs/error-analysis-13.md`） |
| `results/run-local6.json` | 本地 Qwen3-4B-AWQ | `evals/tasks.jsonl`（36 题） | 1 | `adv-05`：畸形表达式 → 工具报错 → 思考螺旋截断 |

`mine_failures.py` 按优先级把失败归到四种模式（`_classify`）：
工具报错（trace 中 `tool_result` 以「错误:」开头）> 部署（api_error）>
思考截断（orchestration_error）> 判分不符（grade 的 failure_type）。

实测计数（源一共 14 条失败）：

| 模式 | 出现 | 出题 | 处置 |
|---|---|---|---|
| wrong_answer | 8 | 3 | 同族不同实例：从 `RULES` 里挑**源 trace 未查过**的 rule_id，答案 `f"{rid} {tier}"` 由 RULES 推导 |
| truncation | 3 | 3 | 单步短题：强制先调工具再答（audit 问 default_severity，calc 造 `a*b`） |
| tool_error | 1 | 1 | 按源表达式嵌套深度造**括号完整**的新表达式，答案走 `calc()` 现算 |
| deployment | 2 | 0 | 上下文溢出 / 超时是部署约束，不是能力缺口 → 只 skip 记录 |

`--per-mode 3` 限制单模式题量，避免 8 条同类失败刷成 8 道重复题。
每道候选都要过「标准答案能否被程序重算」这道 fail-closed 校验（calc 重算 / RULES 推导），
算不出就丢弃并写进 `skipped`。随机数由 `sha256(run_id:task_id:mode)` 播种，可复现。

## 二、七道题与溯源

| 题号 | 类别 | 源题 / 失败模式 | 期望（程序现算） | 网关 | 本地 |
|---|---|---|---|---|---|
| mined-01 | audit | audit-01 / wrong_answer | `C9 PARTIAL` | ✅ | ✅ |
| mined-02 | audit | audit-02 / wrong_answer | `C10 PARTIAL` | ✅ | ✅ |
| mined-03 | audit | audit-03 / truncation | `HIGH` | ✅ | ✅ |
| mined-04 | audit | audit-05 / wrong_answer | `C2 PARTIAL` | ✅ | ✅ |
| mined-05 | audit | audit-11 / truncation | `MEDIUM` | ✅ | ✅ |
| mined-06 | audit | audit-13 / truncation | `LOW` | ✅ | ✅ |
| mined-07 | calc | adv-05 / tool_error | `30` | ✅ | ✅ |

题面见 `evals/tasks-mined.jsonl`；因为一个 run 只能挂一个 dispatch 表，
另拆 `evals/tasks-mined-audit.jsonl`（6 题）与 `evals/tasks-mined-calc.jsonl`（1 题）。
四份结果：`results/run-mined-{gw,local}-{audit,calc}.json`，判分均为满分。
`provenance` 字段含 `source_run / source_task / failure_type / event_seq / source_prompt`
（tool_error 题的 `event_seq` 指向源 trace 里那条畸形 `calc` 工具调用）。

## 三、这轮实验的诚实结论：挖掘出来的题**没有区分度**

两个后端在 7 道新题上都是 7/7。这不是「评测集变强了」，而是一条**负结果**：

- 新题全是一次工具调用 + 短答案；源失败集中在**多步链 / 长思考预算 / 2048 上下文溢出**
  （`adv-05` 的思考螺旋、`audit-07` 刷 10 条规则撑爆上下文）。
- 换句话说，本地 4B 的缺口不是「会不会查表」，而是「长链里能不能不把自己绕死」。
  单步题恰好绕开了这个缺口，所以是无效的区分器。
- 下一轮该做的是**按失败链的长度造题**（把 `adv-05` 的 6 层嵌套保留、换常量；
  把 audit-07 的 10 规则浏览压缩成 3 条以内），而不是再刷单步查表题。
  这条待办写在这里，不假装已完成。

**顺带抓到自己的一个 defect（真实运行发现的，不是事先想到的）**：
第一版 truncation 题面写「再用一句话说明默认严重级」，模型如实答
`C6 的默认严重级是 LOW`，判分只认裸词 → 判成 wrong_answer。
题干已改为「只输出该规则的默认严重级（HIGH/MEDIUM/LOW 之一）」，
重生成后网关 6/6。**是题面有歧义，不是模型错**——这正是「先看数据再下结论」的价值。

## 四、和基线门禁的关系

`derive_gate.py` 的 `BASELINE_GLOBS` 只收 `run-gw[0-9]` / `run-local[0-9]` /
`run-audit-gw[0-9]`：这批 7 题是**从失败派生的探针**，不是基线，
不会进入门禁阈值推导（否则 6 题满分会把 6 题桶的阈值抬到 0.8333，污染基线）。

## 复现

```bash
python3 mine_failures.py --selftest
python3 mine_failures.py --runs results/run-audit-local1.json results/run-local6.json --per-mode 3
python3 run_agent.py --all --tasks-file evals/tasks-mined-audit.jsonl --toolset audit \
  --out results/run-mined-gw-audit.json
python3 run_eval.py --replay results/run-mined-gw-audit.json \
  --tasks-file evals/tasks-mined-audit.jsonl --gate 0.9
```
