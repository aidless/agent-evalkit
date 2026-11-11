# audit 集本地失败逐条标注（run-audit-local1，13/20 失败）

> 来源：`results/run-audit-local1.json`（Qwen3-4B-AWQ @ 127.0.0.1:8123，max_tokens 2048，
> 2026-11-10），失败归因由 `grade_all` 重算（wrong_answer 8 / orchestration_error 3 /
> api_error 2），根因逐条对照 `traces/run-20261110T101228Z.jsonl` 手标。
> 这是全流程唯一零 GPU、零 API 的分析步——总分 0.35 不告诉你怎么修，逐条标注才告诉。

## 四类错误分型

| 类型 | 数量 | 任务 |
|---|---|---|
| 查表归类错（调了工具、答错规则） | 7 | audit-01/02/05/09/10/17/19 |
| 未查先答（没调 audit_rule 直接答） | 1 | audit-15 |
| 思考截断（2048 想完未闭合） | 3 | audit-03/11/13 |
| 部署/网络（非模型能力） | 2 | audit-07（上下文溢出 400）、audit-14（超时 ×3） |

## 逐条标注

| 题号 | 期望 | 实答 | 类型 | 一句话根因 |
|---|---|---|---|---|
| audit-01 | C1 | C3 | 归类错 | 「符号用了未定义」应归 C1（Abstract symbol definitions），模型把它理解成「概念未形式化」→ 兜底答 C3 |
| audit-02 | C5 | C3 | 归类错 | p 值缺样本量与检验名应归 C5（Sample size and effect-size transparency），又答 C3——C3 成了万能兜底 |
| audit-03 | C4 CONFIRMED | — | 截断 | 思考 2048 token 未闭合，无工具无答案，被截断守卫判 orchestration_error |
| audit-05 | C9 | C3 | 归类错 | 图缺 caption 应归 C9（Figure captions），第三次答 C3 |
| audit-07 | C4,C8 | — | 部署约束 | 模型**刷遍 10 条规则**（10 次 audit_rule，参数各不相同）后仍要再想，累计上下文 2049 > max-model-len 2048 → HTTP 400。KV 2,224 tokens 共存配置下 max-model-len 抬不高 |
| audit-09 | C6 LOW | C1 HIGH | 归类错 | 套话替换应归 C6（Blacklisted vocabulary，默认 LOW），归到 C1 且严重级连带答错 |
| audit-10 | C2 PARTIAL | C7 PARTIAL | 归类错 | Bonferroni 校正数与比较族数对不上应归 C2，误归 C7（引用语境）——层级 PARTIAL 是对的，规则选错 |
| audit-11 | C1 HIGH | — | 截断 | 同 audit-03 |
| audit-13 | C10 LOW | — | 截断 | 同 audit-03（可复现性判断题） |
| audit-14 | C1,C2 | — | 网络 | 服务端 3 次超时，重试耗尽；与本机模型能力无关 |
| audit-15 | C3 | C2 | 未查先答 | reasons 同时含「未调用必用工具 audit_rule」与答案不符——先答后没查，归因优先级判 wrong_answer |
| audit-17 | C7 | C2 | 归类错 | ceremonial 引用应归 C7（Citation context），误归 C2 |
| audit-19 | C9 | C3 | 归类错 | caption 与图数据不符应归 C9，第四次答 C3 |

## 模式与盲区

1. **C3 兜底效应**（4/13）：「形式化」的规则描述太宽，模型把一切概念/定义类
   问题都往 C3 塞。修法方向不是改 harness，是把规则 description 写得更判别
   （或者评测集里加 C3 的负例）。
2. **截断三条全是「规则+严重级」两段式作答题**：模型要先查规则再查默认严重级
   （两次工具调用），2048 思考预算不够——这与部署约束（max-model-len=2048
   抬不高）同根，属环境限制而非纯能力判断。
3. **dup_calls 的盲区（如实记录）**：audit-07 刷遍规则表的 10 次调用参数各不相同，
   `dup_calls`（同 tool+同 args）读数为 0——**同参重复抓得住，非必要全表浏览
   抓不住**。README 早前把这条写成「audit-01 十次同参调用被 dup_calls 显形」是
   错的（混淆了单题 smoke 与全量 run），已改正。浪费可观测性的下一格是
   「每题工具调用数 vs 必要调用数」，尚未实现。
4. **网关同题 20/20**（run-audit-gw3）：同样的题、同样的工具、同样的门禁，
   4B 本地 0.35 vs 网关 1.00——这 13 条就是两个后端差距的全部内容。
