#!/usr/bin/env python3
"""trace_timeline.py — 把 trace JSONL 渲染成零依赖单文件 HTML 时间线。

用法:
    python3 trace_timeline.py traces/run-20261110T151454Z.jsonl --out timeline.html --limit 8
    python3 trace_timeline.py results/failure-loop/*.jsonl --out adv05.html

设计:
- 每个任务一条泳道，事件按相对首事件的毫秒偏移横向排布；
- llm_call = 蓝条（宽度=latency_ms，悬停看耗时/轮次/finish_reason）；
- tool_call/tool_result = 绿色菱形，error = 红色竖线，final = 黑旗；
- 无第三方依赖，浏览器直接打开。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def _ts_ms(ts: str) -> float:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.timestamp() * 1000


def collect(files: list[Path], limit: int) -> dict[str, list[dict]]:
    per_task: dict[str, list[dict]] = defaultdict(list)
    for f in files:
        with f.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                task = ev.get("task_id")
                if task is not None:
                    per_task[str(task)].append(ev)
    tasks = sorted(per_task, key=lambda t: min(_ts_ms(e["ts"]) for e in per_task[t] if e.get("ts")))
    return {t: per_task[t] for t in tasks[:limit]}


def render(tasks: dict[str, list[dict]], title: str, out: Path) -> None:
    events_by_task = []
    for task, evs in tasks.items():
        evs = [e for e in evs if e.get("ts")]
        t0 = min(_ts_ms(e["ts"]) for e in evs)
        spans = [(_ts_ms(e["ts"]) - t0) for e in evs]
        total = max(spans + [1000.0])
        items = []
        for e, off in zip(evs, spans):
            kind = e.get("event", "?")
            lat = e.get("latency_ms")
            w = max(3.0, (lat or 200.0) / total * 100)
            tip = json.dumps({k: e.get(k) for k in ("event", "turn", "latency_ms",
                "finish_reason", "tool", "n_tool_calls", "error", "error_type") if e.get(k) is not None},
                ensure_ascii=False)
            items.append({"kind": kind, "off_pct": off / total * 100, "w_pct": w, "tip": tip,
                          "label": kind})
        events_by_task.append({"task": task, "total_ms": round(total), "items": items})

    colors = {"llm_call": "#4c8bf5", "tool_call": "#2fae60", "tool_result": "#9bd8b0",
              "final": "#333", "error": "#e0483f"}
    css_marks = "".join(f".{k}{{background:{c}}}" for k, c in colors.items())
    body_rows = []
    for lane in events_by_task:
        marks = "".join(
            f'<div class="ev {i["kind"]}" style="left:{i["off_pct"]:.2f}%;width:{i["w_pct"]:.2f}%" '
            f'title=\'{i["tip"]}\'></div>'
            for i in lane["items"])
        body_rows.append(
            f'<div class="lane"><div class="tname">{lane["task"]}</div>'
            f'<div class="track">{marks}<span class="ms">{lane["total_ms"]}ms</span></div></div>')

    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>
body{{font-family:ui-sans-serif,system-ui;margin:24px;background:#fafafa}}
h1{{font-size:18px}} .lane{{display:flex;align-items:center;margin:6px 0}}
.tname{{width:110px;font:12px/1.4 ui-monospace,monospace;color:#555;text-align:right;padding-right:8px}}
.track{{position:relative;flex:1;height:22px;background:#fff;border:1px solid #e2e2e2;border-radius:4px}}
.ev{{position:absolute;top:4px;height:14px;border-radius:3px;min-width:3px;cursor:pointer}}
{css_marks}
.final{{width:6px !important;background:#333 !important}}
.ms{{position:absolute;right:4px;top:3px;font:10px monospace;color:#999}}
.legend span{{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 12px}}
</style></head><body>
<h1>{title}</h1>
<p class="legend">llm_call 蓝条宽=latency · tool_call/tool_result 绿 · error 红 · final 黑 · 悬停看详情（来源 trace JSONL，零依赖渲染）</p>
{''.join(body_rows)}
</body></html>"""
    out.write_text(html)
    print(f"{out} — {len(events_by_task)} 任务, {sum(len(l['items']) for l in events_by_task)} 事件")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("traces", nargs="+")
    ap.add_argument("--out", default="trace-timeline.html")
    ap.add_argument("--limit", type=int, default=12, help="最多渲染的任务数（按开始时间取前 N）")
    ap.add_argument("--title", default="Trace Timeline")
    a = ap.parse_args()
    files = [Path(p) for p in a.traces]
    for f in files:
        if not f.exists():
            sys.exit(f"trace 不存在: {f}")
    render(collect(files, a.limit), a.title, Path(a.out))


if __name__ == "__main__":
    main()
