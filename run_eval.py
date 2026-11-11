#!/usr/bin/env python3
"""回放评分 + 门禁。--replay 对已录结果评分（不调 LLM，CI 可用）；--gate 低于阈值 exit 1。"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
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


def policy_threshold(total_tasks: int) -> tuple[float | None, str]:
    """从 derive_gate.py 产出的策略文件取阈值；缺文件/题数对不上返回 None（调用方 fail-closed）。"""
    path = ROOT / "results" / "gate-policy.json"
    if not path.exists():
        return None, f"没有策略文件 {path}；先跑 python3 derive_gate.py"
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
        bucket = policy.get("by_total", {}).get(str(total_tasks))
        if bucket:
            return float(bucket["threshold"]), f"来自 {path.name}（{total_tasks} 题桶，n={bucket['n_runs']}）"
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return None, f"策略文件损坏: {exc}"
    return None, f"策略文件里没有 {total_tasks} 题的门禁；换 --gate 显式指定"


def emit_watch(report: dict, threshold: float, ok: bool, why: str,
               alerts_file: str, alert_url: str) -> int:
    """监控接入点：每次评测写一条记录；越线打 ALERT；webhook 投递失败返回 2。"""
    alert = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": report.get("run_id"),
        "model": report.get("model"),
        "pass_rate": report["pass_rate"],
        "passed": report["passed"],
        "total": report["total"],
        "threshold": round(threshold, 6),
        "blocked": not ok,
        "reason": why,
        "failure_attribution": report.get("failure_attribution", {}),
    }
    path = Path(alerts_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(alert, ensure_ascii=False) + "\n")
    if not ok:
        print(f"ALERT {json.dumps(alert, ensure_ascii=False)}", file=sys.stderr)
    if alert_url:
        body = json.dumps(alert, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(alert_url, data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if not (200 <= resp.status < 300):
                    raise urllib.error.HTTPError(alert_url, resp.status, "非 2xx", {}, None)
        except Exception as exc:  # 投递失败必须显性，不能静默丢告警
            print(f"ALERT_DELIVERY_FAILED {alert_url}: {exc}", file=sys.stderr)
            return 2
    return 0


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
    ap.add_argument("--gate", type=float, default=None,
                    help="通过率阈值；缺省读 results/gate-policy.json（由 derive_gate.py 生成）")
    ap.add_argument("--selftest", action="store_true", help="门禁自检（RED→GREEN）")
    ap.add_argument("--json-out", help="把评分报告写到该 JSON 文件")
    ap.add_argument("--watch", action="store_true",
                    help="监控接入点：写 results/alerts.jsonl，越线打 ALERT，可选 webhook")
    ap.add_argument("--alert-url", default="", help="告警 webhook（POST JSON，含 pass_rate/blocked/归因）")
    ap.add_argument("--alerts-file", default=str(ROOT / "results" / "alerts.jsonl"),
                    help="告警记录落盘路径（JSONL，每次评测一行）")
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

    if args.gate is None:
        threshold, source = policy_threshold(report["total"])
        if threshold is None:
            print(f"无法确定门禁阈值: {source}", file=sys.stderr)
            return 2
        print(f"门禁阈值 = {threshold:.4f}（{source}）")
    else:
        threshold = args.gate

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report -> {args.json_out}")

    ok, why = evaluate_gate(report, threshold)
    print(f"\nGATE {threshold:.4f}: {'PASS' if ok else 'BLOCK'} — {why}")
    if args.watch:
        rc = emit_watch(report, threshold, ok, why, args.alerts_file, args.alert_url)
        if rc != 0:
            return rc
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
