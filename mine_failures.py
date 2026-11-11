#!/usr/bin/env python3
"""从真实失败里长出题目——不是为分数好看造题。

与「先想要高分再设计题集」相反：这里的每一道新题都挂在一个**真实失败实例**上，
并带 provenance（源 run / 源题 / 失败类型 / trace 事件序号）。标准答案一律由程序
现算（calc 走工具、audit 走 RULES 表），算不出就丢弃该候选（fail-closed）。

四种失败模式（按优先级判定，同一题只归一类）：
  1. tool_error   —— trace 里出现过工具报错（如同参畸形表达式）→ 同结构新常量重算
  2. wrong_answer —— 判分不符（如规则归类答 C3 兜底）→ 同族不同实例（未被查过的规则）
  3. truncation   —— 思考被 max_tokens 截断且无工具调用 → 单步短答案题（强制先调工具）
  4. deployment   —— api_error（上下文溢出/超时）→ 不生成题，只记录为部署约束

用法:
    python3 mine_failures.py --runs results/run-audit-local1.json results/run-local6.json \
        --out evals/tasks-mined.jsonl
    python3 mine_failures.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evalkit.audit_tools import RULES
from evalkit.grade import grade_all, grade_task
from evalkit.tools import ToolError, calc

ROOT = Path(__file__).resolve().parent
DEFAULT_TASKS = {"36": ROOT / "evals" / "tasks.jsonl", "20": ROOT / "evals" / "tasks-audit.jsonl",
                 "10": ROOT / "evals" / "tasks-audit.jsonl"}


def _load_jsonl(path: Path) -> list[dict]:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _seeded(*parts: str) -> random.Random:
    seed = int(hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:12], 16)
    return random.Random(seed)


def _load_trace(path: str | None) -> dict[str, list[dict]]:
    by_task: dict[str, list[dict]] = {}
    if not path:
        return by_task
    p = Path(path)
    if not p.exists():
        return by_task
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        ev = json.loads(line)
        ev.setdefault("seq", i)
        by_task.setdefault(ev.get("task_id"), []).append(ev)
    return by_task


def _classify(failure_type: str, result: dict, events: list[dict]) -> str:
    if any(ev.get("event") == "tool_result" and str(ev.get("result", "")).startswith("错误:")
           for ev in events):
        return "tool_error"
    if result.get("error_type") == "api_error":
        return "deployment"
    if result.get("error_type") == "orchestration_error":
        return "truncation"
    return failure_type or "unknown"


def _calc_candidate(rng: random.Random, depth: int) -> tuple[str, str]:
    """按源失败的嵌套深度造一条**括号完整**的表达式，期望值由 calc 现算。"""
    depth = max(2, min(depth, 8))
    nums = [rng.randint(2, 19) for _ in range(depth + 2)]
    expr = str(nums[-1])
    for i in range(depth, 0, -1):
        expr = f"({nums[i - 1]}+{i}*({expr}))" if i % 2 else f"({nums[i - 1]}+{expr})"
    return expr, calc(expr)


def _nesting_depth(source_expr: str) -> int:
    depth = 0
    for ch in source_expr or "":
        depth += (ch == "(") - (ch == ")")
    return abs(depth) or 2


def mine(run_paths: list[Path], tasks_file: Path | None = None, per_mode: int = 2) -> dict:
    tasks: list[dict] = []
    modes: dict[str, int] = {}
    skipped: list[dict] = []
    counter = 0

    for path in run_paths:
        run = json.loads(Path(path).read_text(encoding="utf-8"))
        results = run.get("results")
        if not isinstance(results, list) or not results:
            raise ValueError(f"{path} 没有 results 明细，无法挖失败")
        tf = tasks_file or DEFAULT_TASKS.get(str(len(results)))
        if tf is None or not Path(tf).exists():
            raise ValueError(f"{path} 是 {len(results)} 题，找不到对应题集，请传 --tasks-file")
        task_by_id = {t["id"]: t for t in _load_jsonl(Path(tf))}
        traces = _load_trace(run.get("trace_file"))

        graded = {g["task_id"]: g for g in grade_all(list(task_by_id.values()), results)["grades"]}
        emitted_per_mode: dict[str, int] = {}
        for result in results:
            tid = result.get("task_id")
            grade = graded.get(tid)
            events = traces.get(tid, [])
            mode = _classify(grade["failure_type"] if grade else "", result, events)
            passed = bool(grade and grade["passed"])
            if passed:
                continue
            modes[mode] = modes.get(mode, 0) + 1
            if emitted_per_mode.get(mode, 0) >= per_mode:
                continue
            if mode == "deployment":
                skipped.append({"source_run": run.get("run_id"), "source_task": tid,
                                "failure_type": mode,
                                "why": "部署约束（上下文溢出/超时），不是可出题的能力缺口"})
                continue

            rng = _seeded(str(run.get("run_id")), str(tid), mode)
            task = None
            if mode == "tool_error":
                src = next((ev for ev in events if ev.get("event") == "tool_call"
                            and ev.get("tool") == "calc"), None)
                if src is None:
                    skipped.append({"source_run": run.get("run_id"), "source_task": tid,
                                    "failure_type": mode, "why": "工具报错不来自 calc，暂不入题"})
                    continue
                expr, expected = _calc_candidate(rng, _nesting_depth(src["arguments"].get("expr", "")))
                task = {
                    "prompt": f"用 calc 工具计算 {expr}（表达式先原样传给工具，再给结果）。只输出结果。",
                    "expected_answer": expected, "required_tools": ["calc"], "category": "calc",
                    "tool_args": {"expr": expr},
                    "provenance": {"source_run": run.get("run_id"), "source_task": tid,
                                   "failure_type": mode, "event_seq": src.get("seq"),
                                   "source_prompt": task_by_id[tid]["prompt"][:120]},
                }
            elif mode == "wrong_answer":
                src = task_by_id[tid]
                if src.get("category") != "audit":
                    skipped.append({"source_run": run.get("run_id"), "source_task": tid,
                                    "failure_type": mode, "why": "非 audit 类错答案暂不入题"})
                    continue
                queried = {ev.get("arguments", {}).get("rule_id") for ev in events
                           if ev.get("event") == "tool_call" and ev.get("tool") == "audit_rule"}
                candidates = [rid for rid in RULES if rid not in queried]
                if not candidates:
                    skipped.append({"source_run": run.get("run_id"), "source_task": tid,
                                    "failure_type": mode, "why": "源 trace 已把全部规则查过，无新实例"})
                    continue
                rid = candidates[rng.randrange(len(candidates))]
                rule = RULES[rid]
                task = {
                    "prompt": f"用 audit_rule 查 tmaudit 规则 {rid}：它的证据层级是什么？"
                              f"按「id 空格 层级」回答（如 C4 CONFIRMED）。",
                    "expected_answer": f"{rid} {rule['evidence_tier']}",
                    "required_tools": ["audit_rule"], "category": "audit",
                    "tool_args": {"rule_id": rid},
                    "provenance": {"source_run": run.get("run_id"), "source_task": tid,
                                   "failure_type": mode, "event_seq": None,
                                   "source_answer": str(result.get("final_answer"))[:80],
                                   "source_expected": src.get("expected_answer")},
                }
            elif mode == "truncation":
                src = task_by_id[tid]
                if src.get("category") == "audit":
                    rid = rng.choice([r for r in RULES if r not in
                                      {ev.get("arguments", {}).get("rule_id") for ev in events
                                       if ev.get("event") == "tool_call"}])
                    task = {
                        "prompt": f"先调用 audit_rule 查 {rid}，然后只输出该规则的默认严重级（HIGH/MEDIUM/LOW 之一）。",
                        "expected_answer": RULES[rid]["default_severity"],
                        "required_tools": ["audit_rule"], "category": "audit",
                        "tool_args": {"rule_id": rid},
                        "provenance": {"source_run": run.get("run_id"), "source_task": tid,
                                       "failure_type": mode, "event_seq": None,
                                       "source_prompt": src["prompt"][:120]},
                    }
                else:
                    a, b = rng.randint(11, 89), rng.randint(3, 9)
                    expr = f"{a}*{b}"
                    task = {
                        "prompt": f"先调用 calc 算 {expr}，然后只输出结果。",
                        "expected_answer": calc(expr), "required_tools": ["calc"], "category": "calc",
                        "tool_args": {"expr": expr},
                        "provenance": {"source_run": run.get("run_id"), "source_task": tid,
                                       "failure_type": mode, "event_seq": None,
                                       "source_prompt": src["prompt"][:120]},
                    }
            if task is None:
                continue
            # fail-closed：标准答案必须能被程序重算出来，否则丢弃
            try:
                if task["required_tools"] == ["calc"]:
                    assert calc(task["tool_args"]["expr"]) == task["expected_answer"]
                else:
                    rid = task["tool_args"]["rule_id"]
                    assert task["expected_answer"] == f"{rid} {RULES[rid]['evidence_tier']}" or \
                        task["expected_answer"] == RULES[rid]["default_severity"]
            except (ToolError, AssertionError, KeyError) as exc:
                skipped.append({"source_run": run.get("run_id"), "source_task": tid,
                                "failure_type": mode, "why": f"标准答案无法程序验证: {exc}"})
                continue
            counter += 1
            task = {"id": f"mined-{counter:02d}", **task}
            tasks.append(task)
            emitted_per_mode[mode] = emitted_per_mode.get(mode, 0) + 1

    return {"tasks": tasks, "modes": modes, "skipped": skipped,
            "source_runs": [str(p) for p in run_paths]}


def selftest() -> int:
    failures, checks = [], 0

    def check(name: str, cond: bool, detail: str = ""):
        nonlocal checks
        checks += 1
        print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
        if not cond:
            failures.append(name)

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        trace = td / "t.jsonl"
        trace.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
            {"task_id": "c1", "event": "tool_call", "turn": 1, "tool": "calc",
             "arguments": {"expr": "((1+2"}, "seq": 1},
            {"task_id": "c1", "event": "tool_result", "turn": 1, "tool": "calc",
             "result": "错误: calc 执行失败: '(' was never closed", "seq": 2},
        ]) + "\n", encoding="utf-8")
        tasks = td / "tasks.jsonl"
        tasks.write_text(json.dumps({"id": "c1", "prompt": "计算 ((1+2", "expected_answer": "3",
                                     "required_tools": ["calc"], "category": "calc"}) + "\n",
                          encoding="utf-8")
        run = td / "run.json"
        run.write_text(json.dumps({"run_id": "r1", "trace_file": str(trace),
                                   "results": [{"task_id": "c1", "ok": False, "final_answer": None,
                                                "turns": 2, "tools_called": ["calc"],
                                                "error": "截断", "error_type": "orchestration_error"}]}),
                       encoding="utf-8")
        out = mine([run], tasks_file=tasks, per_mode=1)
        check("GREEN 从工具报错长出 1 题", len(out["tasks"]) == 1, str(out["modes"]))
        check("GREEN 标准答案可程序重算",
              out["tasks"][0]["expected_answer"] == calc(out["tasks"][0]["tool_args"]["expr"]))
        check("GREEN provenance 指向源事件",
              out["tasks"][0]["provenance"]["event_seq"] == 1)
        empty = td / "empty.json"
        empty.write_text(json.dumps({"run_id": "x", "results": []}), encoding="utf-8")
        try:
            mine([empty], tasks_file=tasks)
            check("RED 空 run 拒绝", False)
        except ValueError as exc:
            check("RED 空 run 拒绝", True, str(exc)[:40])

    print(f"\nSELFTEST {'PASS' if not failures else 'FAIL'}: {checks - len(failures)}/{checks} 通过")
    return 0 if not failures else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="从真实失败里长出评测题")
    ap.add_argument("--runs", nargs="*", help="源 run JSON（含失败明细）")
    ap.add_argument("--tasks-file", help="源题集；缺省按 run 题数自动选")
    ap.add_argument("--per-mode", type=int, default=2, help="每种失败模式最多出几题")
    ap.add_argument("--out", default=str(ROOT / "evals" / "tasks-mined.jsonl"))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.runs:
        print("需要 --runs（至少一个含失败明细的 run JSON）", file=sys.stderr)
        return 2
    try:
        out = mine([Path(p) for p in args.runs],
                   Path(args.tasks_file) if args.tasks_file else None, args.per_mode)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"挖掘失败: {exc}", file=sys.stderr)
        return 2

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in out["tasks"]),
                        encoding="utf-8")
    # 工具集不同（calc 与 audit）必须分文件：一个 run 只能挂一个 dispatch 表
    for category in sorted({t["category"] for t in out["tasks"]}):
        part = out_path.with_name(f"{out_path.stem}-{category}.jsonl")
        part.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n"
                                for t in out["tasks"] if t["category"] == category), encoding="utf-8")
        print(f"{category:<6} -> {part}")
    print(f"{'失败模式':<14} {'出现次数':>8} {'出题':>4}")
    for mode, n in sorted(out["modes"].items()):
        emitted = sum(1 for t in out["tasks"] if t["provenance"]["failure_type"] == mode)
        print(f"{mode:<14} {n:>8} {emitted:>4}")
    for s in out["skipped"]:
        print(f"  skip {s['failure_type']:<12} {s['source_task']:<10} {s['why']}", file=sys.stderr)
    print(f"\n{len(out['tasks'])} 题 -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
