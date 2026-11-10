#!/usr/bin/env python3
"""跑评测任务：--all 或 --task <id>，产出 traces/run-*.jsonl 与 results/latest.json。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evalkit.agent import Agent
from evalkit.trace import TraceWriter

ROOT = Path(__file__).resolve().parent
TASKS_PATH = ROOT / "evals" / "tasks.jsonl"


def load_tasks(path=None) -> list[dict]:
    tasks = []
    with Path(path or TASKS_PATH).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))
    return tasks


def main() -> int:
    ap = argparse.ArgumentParser(description="运行 agent 评测任务")
    ap.add_argument("--all", action="store_true", help="跑全部任务")
    ap.add_argument("--task", help="只跑指定 id 的任务")
    ap.add_argument("--limit", type=int, help="只跑前 N 个任务（冒烟用）")
    ap.add_argument("--model", default=os.environ.get("AGENT_MODEL", "opencode-go/deepseek-v4.1-flash"))
    ap.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:3425/v1"))
    ap.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", "sk-local"))
    ap.add_argument("--max-tokens", type=int, default=512,
                    help="单次 LLM 调用的输出上限；思考型模型要调大防止截断（512 只够无思考模型）")
    ap.add_argument("--max-turns", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--replay-trace", default="",
                    help="回放模式：从 trace JSONL 重放 LLM 调用（离线，无 LLM）")
    ap.add_argument("--toolset", default="tools", choices=["tools", "audit"],
                    help="tools=calc/lookup/convert；audit=tmaudit C1-C10 规则语义查询（配 evals/tasks-audit.jsonl）")
    ap.add_argument("--tasks-file", default=str(ROOT / "evals" / "tasks.jsonl"),
                    help="任务集路径；audit 工具集配 evals/tasks-audit.jsonl")
    ap.add_argument("--metrics-url", default="",
                    help="vLLM /metrics 端点（如 http://127.0.0.1:8123/metrics）；给空串则不抓 prefix-cache 命中率")
    ap.add_argument("--traces-dir", default=str(ROOT / "traces"),
                    help="trace JSONL 输出目录")
    ap.add_argument("--out", default=str(ROOT / "results" / "latest.json"))
    args = ap.parse_args()

    if not args.all and not args.task:
        ap.error("需要 --all 或 --task <id>")

    tasks = load_tasks(args.tasks_file)
    if args.task:
        tasks = [t for t in tasks if t["id"] == args.task]
        if not tasks:
            print(f"找不到任务 {args.task}", file=sys.stderr)
            return 2
    if args.limit:
        tasks = tasks[: args.limit]

    from evalkit.metrics import snapshot, diff_rate
    m_before = snapshot(args.metrics_url) if args.metrics_url else None

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    trace_path = Path(args.traces_dir) / f"run-{ts}.jsonl"
    llm_fn = None
    if args.replay_trace:
        from evalkit.replay import ReplayLLM
        llm_fn = ReplayLLM(args.replay_trace)

    agent = Agent(toolset=args.toolset, model=args.model, base_url=args.base_url, api_key=args.api_key,
                  max_turns=args.max_turns, timeout=args.timeout, max_tokens=args.max_tokens,
                  llm_fn=llm_fn)

    results = []
    started = time.time()
    with TraceWriter(trace_path) as trace:
        for i, task in enumerate(tasks, 1):
            t0 = time.time()
            r = agent.run(task, trace)
            results.append({
                "task_id": r.task_id, "ok": r.ok, "final_answer": r.final_answer,
                "turns": r.turns, "tools_called": r.tools_called, "llm_calls": r.llm_calls,
                "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens,
                "cost": round(r.cost, 8), "llm_latency_ms": r.llm_latency_ms,
                "error": r.error, "error_type": r.error_type,
                "wall_ms": round((time.time() - t0) * 1000, 1),
            })
            mark = "ok " if r.ok and r.final_answer else "ERR"
            print(f"[{i:>2}/{len(tasks)}] {mark} {task['id']:<10} turns={r.turns} "
                  f"tools={','.join(r.tools_called) or '-'} answer={r.final_answer!r} "
                  f"({(time.time()-t0)*1000:.0f}ms)", flush=True)

    payload = {
        "run_id": ts,
        "model": args.model,
        "base_url": args.base_url,
        "max_turns": args.max_turns,
        "max_tokens": args.max_tokens,
        "total_tasks": len(tasks),
        "elapsed_s": round(time.time() - started, 1),
        "prefix_cache_hit_rate": diff_rate(m_before, snapshot(args.metrics_url) if args.metrics_url else None),
        "trace_file": str(trace_path),
        "results": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\nresults -> {out}")
    print(f"traces  -> {trace_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
