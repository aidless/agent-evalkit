# 我的评测框架抓到了自己两个 bug，整套跑完只花半分钱电费

> 中文稿（掘金 / 知乎 / V2EX / 公众号）。英文标题备选：
> *My eval harness found two bugs in itself, and the full run cost half a cent of electricity*
> （HN / Reddit r/MachineLearning 用英文标题 + 一张 trace 时间线截图）。
> 仓库：https://github.com/aidless/agent-evalkit

## 开场先给结论，不铺垫

写一个「Agent 评测门禁」最容易骗到自己：门禁绿了，你就以为 agent 没问题。
我这套东西反过来——**先把评测仪自己拆开验一遍**。TDD 的 RED 阶段当场抓到两个真 bug：

1. `convert(1, "km", "kg")` 静默返回 `1`：长度和质量混在同一张线性换算表里，
   跨量纲换算压根没被拒绝。修法：拆成 `_LENGTH` / `_MASS` 两张表，跨量纲抛 ValueError。
2. `answer_matches` 的 `rel_tol=1e-3` 把 `4717` 和 `4716` 判成相等（相对误差 0.02%）。
   修法：整数答案走精确相等，只有含小数点的答案才用容差。

如果没先写会红的测试，这两个 bug 会让 36 题评测集「满分」——而满分是假的。
后面九人格评审团又揪出三处：`trace` 把 `content` 截到 300 字符却承诺
「回放确定性」（>300 字符答案必然不一致）、`dispatch_via` 报错引用了错误的
工具注册表、失败归因靠 `"答案不符" in reasons` 做字符串匹配。全部 RED→GREEN 修完，
现在 **52 → 63 个单测**、回放逐字一致。代码零第三方依赖，`clone` 下来
`python3 -m unittest discover -s tests` 就能跑。

## 60 秒验证入口（别信我的截图，自己跑）

```bash
git clone https://github.com/aidless/agent-evalkit && cd agent-evalkit
python3 -m unittest discover -s tests
python3 run_eval.py --selftest                 # 8/8，其中 5 条是注入违规的反证
python3 run_eval.py --replay results/run-gw5.json   # GATE 0.9167: PASS — 0.9444
```

最后那条不需要 API、不需要 GPU：它拿已录的 36 题 run 回放评分。
门禁阈值也不是我拍脑袋写的 0.80，而是 `derive_gate.py` 从历史 11 次全量 run 推导：
`(最差一次通过数 34 − 1) / 36 = 0.9167`。0.80 等于允许比历史最差再多错 5 道，
那叫装饰不叫门禁。越线时 `--watch` 写 `results/alerts.jsonl` 并可 POST webhook，
告警投递失败单独返回 exit 2（不让「告警没送到」伪装成绿灯）。

## 数字，以及数字里不好看的那部分

| 后端 / 题集 | 通过率 | 说明 |
|---|---|---|
| 网关 ×5（36 题） | 0.9666 ± 0.0272 | 每次红灯的题不一样，是非确定性抖动 |
| 本地 Qwen3-4B-AWQ ×4 | 0.9722 ± 0.0000 | 每次都栽在同一道题、同一条失败链 |
| 本地 audit 20 题 | 0.3500 | 网关同题 20/20；13 条失败逐条标在 docs/error-analysis-13.md |
| 从失败挖出的 7 道新题 | 两个后端都 7/7 | **负结果**：单步题绕开了 4B 的真实缺口 |

最硬的数字不是 0.9666，而是「0.35 vs 1.00」这种分离度：同一套工具、同一套门禁，
把两个后端的能力差直接顶到脸上。最不硬的数字是任何单次通过率——
网关 5 次的 sd 就有 0.0272，样本量不够，我不会拿它做显著性结论。

一次 36 题全量评测的电费 ≈ **¥0.0051**（只算 GPU 电，按实测功率窗口 × ¥0.6/kWh
折算，不含整机/折旧/机房）。这个数字的意义是「评测可以常跑」，不是「评测免费」。

## 一个能打自己脸的对照

我曾以为发现 vLLM 0.33 的 prefix-cache bug：相同 prompt 永远 0 命中，
先怀疑流量归因，再怀疑新引入的 LBNHC KV layout，还提了 vllm#66786。
最后打日志做单变量对照才发现：13-token 的 prompt **短于一个 16-token block**，
调度时根本没有完整 block 的 hash 可查；换成 ~72 token 的相同 prompt，
第二次 `new_tokens=64`（89% 命中）。这是 block 粒度缓存的设计行为，不是 bug。
issue 我自己答自己关了（not_planned），两级归因弯路写在
`docs/incident-66786.md`。教训一句话：**报 bug 前先做单变量对照，长度就是那个变量。**

## 边界（免得被当成托管观测平台）

没有存储后端、没有自动定时重跑、没有告警去重/升级；阈值是回归线不是 SLO；
36 题满分只说明「没退化」，不说明「能力强」；从失败挖出的 7 题两个后端都满分，
恰恰证明下一轮该按失败链长度造题，而不是再刷单步查表题。
真正能长期复用的不是这些分数，是那条链：真流量 → 逐事件 flush 的 trace →
确定性评分 → 失败归因 → 可推导的门禁 → 可挂出去的告警。

（配图建议：`python3 trace_timeline.py` 生成的 `docs/trace-timeline.html` 截一张，
12 条泳道里那条红色失败泳道比任何表格都直观。）
