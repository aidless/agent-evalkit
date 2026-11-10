#!/usr/bin/env python3
"""trace 聚合摘要：延迟分位 / token 用量 / 工具调用分布 / 失败归因。"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evalkit.trace import read_events

ROOT = Path(__file__).resolve().parent


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
    return s[k]


def aggregate(files: list[Path]) -> dict:
    """把一批 trace 文件折成统计量。纯函数，便于单测。"""
    latencies: list[float] = []
    tool_counter: Counter = Counter()
    per_task: dict[str, dict] = defaultdict(lambda: {"turns": 0, "tool_calls": 0, "llm_calls": 0,
                                                     "prompt_tokens": 0, "completion_tokens": 0,
                                                     "cost": 0.0, "errors": 0, "seen_calls": set(),
                                                     "dup_calls": 0})
    n_events = 0
    for fp in files:
        for ev in read_events(fp):
            n_events += 1
            tid = ev.get("task_id", "?")
            t = per_task[tid]
            kind = ev.get("event")
            if kind == "llm_call":
                latencies.append(float(ev.get("latency_ms") or 0))
                t["llm_calls"] += 1
                t["turns"] = max(t["turns"], int(ev.get("turn") or 0))
                t["prompt_tokens"] += int(ev.get("prompt_tokens") or 0)
                t["completion_tokens"] += int(ev.get("completion_tokens") or 0)
                t["cost"] += float(ev.get("cost") or 0)
            elif kind == "tool_call":
                tool_counter[ev.get("tool") or "?"] += 1
                t["tool_calls"] += 1
                sig = (ev.get("tool"), json.dumps(ev.get("arguments"), sort_keys=True, ensure_ascii=False))
                if sig in t["seen_calls"]:
                    t["dup_calls"] += 1   # 同工具同参数的重复调用：pass_rate 看不见的浪费
                t["seen_calls"].add(sig)
            elif kind == "error":
                t["errors"] += 1
    return {"n_events": n_events, "latencies": latencies,
            "tool_counter": tool_counter, "per_task": per_task}


def main() -> int:
    ap = argparse.ArgumentParser(description="trace 摘要")
    ap.add_argument("traces", nargs="?", default=str(ROOT / "traces"),
                    help="trace 文件或目录")
    ap.add_argument("--results", default=str(ROOT / "results" / "latest.json"),
                    help="run_agent 的结果 JSON（用于失败归因对账）")
    args = ap.parse_args()

    tpath = Path(args.traces)
    files = sorted(tpath.glob("*.jsonl")) if tpath.is_dir() else [tpath]
    if not files:
        print(f"没有找到 trace 文件: {tpath}", file=sys.stderr)
        return 2

    agg = aggregate(files)
    latencies = agg["latencies"]
    tool_counter = agg["tool_counter"]
    per_task = agg["per_task"]
    n_events = agg["n_events"]

    results = None
    rpath = Path(args.results)
    if rpath.exists():
        try:
            results = json.loads(rpath.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"警告: 结果文件损坏，跳过对账: {rpath}", file=sys.stderr)

    n_tasks = len(per_task)
    total_prompt = sum(t["prompt_tokens"] for t in per_task.values())
    total_completion = sum(t["completion_tokens"] for t in per_task.values())
    total_cost = sum(t["cost"] for t in per_task.values())
    avg_turns = statistics.mean(t["turns"] for t in per_task.values()) if per_task else 0
    avg_tools = statistics.mean(t["tool_calls"] for t in per_task.values()) if per_task else 0

    print(f"trace 文件: {len(files)} 个，事件 {n_events} 条，任务 {n_tasks} 个")
    print(f"\n== LLM 延迟 (ms) ==")
    print(f"  n={len(latencies)}  p50={percentile(latencies,50):.0f}  "
          f"p95={percentile(latencies,95):.0f}  p99={percentile(latencies,99):.0f}  "
          f"max={max(latencies):.0f}" if latencies else "  无 llm_call 事件")
    print(f"\n== Token 用量 ==")
    print(f"  prompt={total_prompt}  completion={total_completion}  total={total_prompt+total_completion}")
    print(f"  cost(网关计)={total_cost:.6f}")
    print(f"\n== 工具调用分布 ==")
    for name, cnt in tool_counter.most_common():
        print(f"  {name:<8} x{cnt}")
    print(f"\n== 任务行为 ==")
    print(f"  平均轮次 {avg_turns:.2f}  平均工具调用 {avg_tools:.2f}/任务  "
          f"错误任务 {sum(1 for t in per_task.values() if t['errors'])}")
    dup_total = sum(t["dup_calls"] for t in per_task.values())
    dup_tasks = [tid for tid, t in per_task.items() if t["dup_calls"]]
    print(f"  冗余工具调用 {dup_total} 次（同 tool+同 args 重复）"
          f"涉及任务 {len(dup_tasks)} 个：{', '.join(sorted(dup_tasks)) or '无'}")

    if results:
        from evalkit.grade import grade_all
        from run_agent import load_tasks
        report = grade_all(load_tasks(), results["results"])
        print(f"\n== 评分对账（来自 {rpath.name}）==")
        print(f"  pass_rate {report['passed']}/{report['total']} = {report['pass_rate']:.4f}")
        if report["failure_attribution"]:
            for k, v in sorted(report["failure_attribution"].items()):
                print(f"  失败归因 {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
