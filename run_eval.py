#!/usr/bin/env python3
"""回放评分 + 门禁。--replay 对已录结果评分（不调 LLM，CI 可用）；--gate 低于阈值 exit 1。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evalkit.grade import evaluate_gate, grade_all
from run_agent import load_tasks

ROOT = Path(__file__).resolve().parent


def build_report(run: dict, tasks_file=None) -> dict:
    tasks = load_tasks(tasks_file or (ROOT / "evals" / "tasks.jsonl"))
    report = grade_all(tasks, run["results"])
    report["run_id"] = run.get("run_id")
    report["model"] = run.get("model")
    report["eval_tasks_file"] = str(tasks_file or (ROOT / "evals" / "tasks.jsonl"))
    return report


def selftest() -> int:
    """RED→GREEN：注入违规必须阻断，合规必须放行，坏报告必须报错。"""
    failures: list[str] = []
    checks = 0

    def check(name: str, cond: bool, detail: str = ""):
        nonlocal checks
        checks += 1
        print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
        if not cond:
            failures.append(name)

    # GREEN: 达标报告必须放行
    ok_report = {"pass_rate": 0.9167, "passed": 22, "total": 24, "failure_attribution": {}}
    passed, why = evaluate_gate(ok_report, 0.80)
    check("GREEN 合规报告放行", passed is True, why)

    # RED: 不达标必须阻断
    bad_report = {"pass_rate": 0.5, "passed": 12, "total": 24, "failure_attribution": {"wrong_answer": 12}}
    passed, why = evaluate_gate(bad_report, 0.80)
    check("RED 违规报告阻断", passed is False, why)

    # RED: 恰好压线（0.80 == 0.80）必须放行（>= 语义）
    edge_report = {"pass_rate": 0.80, "passed": 20, "total": 25, "failure_attribution": {}}
    passed, why = evaluate_gate(edge_report, 0.80)
    check("GREEN 压线 0.80 放行", passed is True, why)

    # RED: 报告损坏必须 fail-closed
    for broken, name in [
        ({"passed": 1, "total": 1}, "缺 pass_rate"),
        ({"pass_rate": "92%", "passed": 22, "total": 24}, "pass_rate 非数值"),
        ({"pass_rate": 1.5, "passed": 24, "total": 24}, "pass_rate 越界"),
        ({"pass_rate": 0.9, "passed": 0, "total": 0}, "total 为 0"),
        ("not-a-dict", "非 JSON 对象"),
    ]:
        passed, why = evaluate_gate(broken, 0.80)
        check(f"RED 坏报告阻断（{name}）", passed is False, why)

    # 分母从实际执行的 check 数现算，禁止写死（写死过 6/6 而实际 8 条）
    print(f"\nSELFTEST {'PASS' if not failures else 'FAIL'}: {checks - len(failures)}/{checks} 通过")
    return 0 if not failures else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="评测门禁")
    ap.add_argument("--replay", default=str(ROOT / "results" / "latest.json"),
                    help="要评分的结果 JSON（默认 results/latest.json）")
    ap.add_argument("--gate", type=float, default=0.80, help="通过率阈值，低于即 exit 1")
    ap.add_argument("--selftest", action="store_true", help="门禁自检（RED→GREEN）")
    ap.add_argument("--json-out", help="把评分报告写到该 JSON 文件")
    ap.add_argument("--tasks-file", default=str(ROOT / "evals" / "tasks.jsonl"),
                    help="任务集路径；audit 任务集配 evals/tasks-audit.jsonl")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    replay = Path(args.replay)
    if not replay.exists():
        print(f"找不到结果文件 {replay}；先跑 run_agent.py --all 或用 --replay 指定", file=sys.stderr)
        return 2
    try:
        run = json.loads(replay.read_text(encoding="utf-8"))
        report = build_report(run, args.tasks_file)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"结果文件损坏，无法评分: {exc}", file=sys.stderr)
        return 2

    # 逐题明细
    print(f"{'task':<10} {'verdict':<7} turns tools")
    for g in report["grades"]:
        verdict = "PASS" if g["passed"] else f"FAIL({g['failure_type']})"
        print(f"{g['task_id']:<10} {verdict:<7} {str(g['turns']):<5} {','.join(g['tools_called']) or '-'}")
        for r in g["reasons"]:
            print(f"           └─ {r}")
    print(f"\npass_rate = {report['passed']}/{report['total']} = {report['pass_rate']:.4f}")
    if report["failure_attribution"]:
        print("失败归因:", json.dumps(report["failure_attribution"], ensure_ascii=False))

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report -> {args.json_out}")

    ok, why = evaluate_gate(report, args.gate)
    print(f"\nGATE {args.gate:.2f}: {'PASS' if ok else 'BLOCK'} — {why}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
