#!/usr/bin/env python3
"""聚合多次运行：通过率稳定性 + 延迟分位 + 门禁注入反证的可复算读数。"""
from __future__ import annotations

import glob
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evalkit.grade import grade_all
from run_agent import load_tasks

ROOT = Path(__file__).resolve().parent


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "--tasks-file"]
    tasks_file = None
    if "--tasks-file" in sys.argv:
        tasks_file = sys.argv[sys.argv.index("--tasks-file") + 1]
    files = sorted({fp for arg in argv for fp in (glob.glob(arg) or [arg])})
    pattern = argv[0] if argv else str(ROOT / "results" / "run-*.json")
    if not files:
        print("没有找到 run-*.json", file=sys.stderr)
        return 2
    tasks = load_tasks(tasks_file)
    rates, latencies, turns, tokens = [], [], [], []
    print("run                           pass_rate  passed/total  p50(ms)  p99(ms)  avg_turns  tokens")
    for fp in files:
        run = json.loads(Path(fp).read_text(encoding="utf-8"))
        report = grade_all(tasks, run["results"])
        lat = [x for r in run["results"] for x in r.get("llm_latency_ms") or []]
        lat_sorted = sorted(lat)
        p50 = lat_sorted[len(lat_sorted) // 2] if lat_sorted else 0
        p99 = lat_sorted[min(len(lat_sorted) - 1, round(0.99 * (len(lat_sorted) - 1)))] if lat_sorted else 0
        avg_turns = statistics.mean(r["turns"] for r in run["results"])
        tok = sum(r["prompt_tokens"] + r["completion_tokens"] for r in run["results"])
        rates.append(report["pass_rate"])
        latencies.extend(lat)
        turns.extend(r["turns"] for r in run["results"])
        tokens.append(tok)
        print(f"{Path(fp).name:<30} {report['pass_rate']:.4f}     "
              f"{report['passed']:>2}/{report['total']:<2}         {p50:>7.0f}  {p99:>7.0f}  "
              f"{avg_turns:>9.2f}  {tok:>6}")

    lat_sorted = sorted(latencies)
    def q(p):
        return lat_sorted[min(len(lat_sorted) - 1, round(p / 100 * (len(lat_sorted) - 1)))] if lat_sorted else 0
    print(f"\n合计: {len(files)} 次运行, {len(latencies)} 次 LLM 调用, "
          f"通过率 mean={statistics.mean(rates):.4f} sd={statistics.pstdev(rates):.4f} "
          f"min={min(rates):.4f} max={max(rates):.4f}")
    print(f"LLM 延迟: p50={q(50):.0f}ms p95={q(95):.0f}ms p99={q(99):.0f}ms  "
          f"平均轮次={statistics.mean(turns):.2f}  总 token={sum(tokens)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
