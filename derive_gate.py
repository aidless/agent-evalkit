#!/usr/bin/env python3
"""从历史无回归 run 推导门禁阈值——替代拍脑袋的 0.80。

规则（唯一一条，可复算）:
    threshold = (最低历史通过题数 - safety_tasks) / total_tasks
语义: 允许比历史最差一次再劣化 safety_tasks 条题；再劣化 1 条即阻断。

为什么不是 0.80: 36 题下 0.80 等于允许失败 7 条题，而网关 6 次实测最差只失败
2 条（34/36），即 0.80 会把「比历史最差还多错 5 条」放过去——门禁形同装饰。

用法:
    python3 derive_gate.py                     # 扫 results/ 里的基线 run，写 results/gate-policy.json
    python3 derive_gate.py --selftest          # RED→GREEN 自检
    python3 derive_gate.py --runs a.json b.json
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RULE = "threshold = (最低历史通过题数 - safety_tasks) / total_tasks"

# 基线集合：只收「无回归」的正式全量 run；regression-*/rerun 一律不入基线
BASELINE_GLOBS = {
    "gateway": "results/run-gw[0-9].json",
    "local": "results/run-local[0-9].json",
    "audit-gateway": "results/run-audit-gw[0-9].json",
}


def _passed_count(run: dict) -> tuple[int, int]:
    """从 run 明细数出 (passed, total)；字段缺失或自相矛盾一律 ValueError。"""
    results = run.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError("run 缺少 results 明细，无法复算通过数")
    total = len(results)
    declared = run.get("total_tasks")
    if declared is not None and declared != total:
        raise ValueError(f"total_tasks={declared} 与明细条数 {total} 不一致")
    passed = 0
    for r in results:
        if not isinstance(r, dict) or "ok" not in r:
            raise ValueError("results 条目缺少 ok 字段")
        if r["ok"]:
            passed += 1
    return passed, total


def derive_threshold(runs: list[dict], total_tasks: int, safety_tasks: int = 1) -> dict:
    """核心规则。runs 里每项可含 passed/total 或 results 明细。"""
    if not isinstance(runs, list) or not runs:
        raise ValueError("没有基线 run，无法推导阈值")
    if not isinstance(total_tasks, int) or total_tasks <= 0:
        raise ValueError(f"total_tasks 非法: {total_tasks!r}")
    if not isinstance(safety_tasks, int) or safety_tasks < 0:
        raise ValueError(f"safety_tasks 非法: {safety_tasks!r}")

    baseline = []
    for run in runs:
        if not isinstance(run, dict):
            raise ValueError("run 不是 JSON 对象")
        if "results" in run:
            passed, total = _passed_count(run)
        else:
            passed, total = run.get("passed"), run.get("total")
            if isinstance(passed, bool) or not isinstance(passed, int):
                raise ValueError(f"passed 不是整数: {passed!r}")
            if isinstance(total, bool) or not isinstance(total, int):
                raise ValueError(f"total 不是整数: {total!r}")
        if total != total_tasks:
            raise ValueError(f"基线 run 题数 {total} ≠ {total_tasks}，不能混算")
        if not (0 <= passed <= total):
            raise ValueError(f"passed 越界: {passed}/{total}")
        baseline.append({"run_id": run.get("run_id"), "passed": passed, "total": total,
                         "pass_rate": round(passed / total, 4)})

    passed_counts = [b["passed"] for b in baseline]
    min_passed = min(passed_counts)
    keep = min(min_passed, total_tasks)
    threshold = (keep - safety_tasks) / total_tasks
    if threshold <= 0:
        raise ValueError(f"推导出阈值 {threshold} ≤ 0: 历史基线太差，先修质量再定门禁")

    rates = [b["pass_rate"] for b in baseline]
    return {
        "rule": RULE,
        "safety_tasks": safety_tasks,
        "total_tasks": total_tasks,
        "min_passed": min_passed,
        "threshold": threshold,  # 不四舍五入：阈值语义按下式精确比较
        "n_runs": len(baseline),
        "mean_pass_rate": round(statistics.fmean(rates), 6),
        "sd_pass_rate": round(statistics.stdev(rates), 6) if len(rates) > 1 else 0.0,
        "baseline_runs": baseline,
    }


def derive_from_files(paths: list[Path], total_tasks: int, safety_tasks: int = 1) -> dict:
    runs = []
    for p in paths:
        try:
            runs.append(json.loads(Path(p).read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{p} 不是合法 JSON: {exc}") from exc
    out = derive_threshold(runs, total_tasks, safety_tasks)
    out["inputs"] = [str(p) for p in paths]
    return out


def collect(sets: dict[str, str] | None = None) -> dict[int, dict]:
    """按集合扫基线文件，再按题数分桶。

    同一集合里的 run 题数不一致（例如 audit 的 10 题早期版与 20 题现行版）时
    按题数各自成桶，绝不混算——混算会让 10 题的 10/10 把 20 题的门禁抬高。
    """
    sets = sets or BASELINE_GLOBS
    buckets: dict[int, list[Path]] = {}
    for name, pattern in sets.items():
        files = sorted(Path(p) for p in glob.glob(str(ROOT / pattern)))
        if not files:
            raise ValueError(f"集合 {name} 没有基线文件（{pattern}）")
        for f in files:
            total = len(json.loads(f.read_text(encoding="utf-8"))["results"])
            buckets.setdefault(total, []).append(f)
    out: dict[int, dict] = {}
    for total, files in sorted(buckets.items()):
        out[total] = derive_from_files(files, total)
        out[total]["inputs"] = [str(p) for p in files]
    return out


def selftest() -> int:
    failures, checks = [], 0

    def check(name: str, cond: bool, detail: str = ""):
        nonlocal checks
        checks += 1
        print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
        if not cond:
            failures.append(name)

    # GREEN: 已知基线 34,34,34,36,36 → 33/36
    out = derive_threshold([{"passed": p, "total": 36} for p in (34, 34, 34, 36, 36)], 36)
    check("GREEN 基线推导 33/36", abs(out["threshold"] - 33 / 36) < 1e-9, f"{out['threshold']:.4f}")
    # GREEN: 全满分基线 → 少容一条题
    out = derive_threshold([{"passed": 36, "total": 36}] * 2, 36)
    check("GREEN 满分基线 35/36", abs(out["threshold"] - 35 / 36) < 1e-9, f"{out['threshold']:.4f}")
    # RED: 空基线
    try:
        derive_threshold([], 36)
        check("RED 空基线拒绝", False)
    except ValueError as exc:
        check("RED 空基线拒绝", True, str(exc)[:40])
    # RED: 题数不一致
    try:
        derive_threshold([{"passed": 34, "total": 24}], 36)
        check("RED 题数不一致拒绝", False)
    except ValueError as exc:
        check("RED 题数不一致拒绝", True, str(exc)[:40])
    # RED: passed 非整数
    try:
        derive_threshold([{"passed": "34", "total": 36}], 36)
        check("RED passed 非整数拒绝", False)
    except ValueError as exc:
        check("RED passed 非整数拒绝", True, str(exc)[:40])
    # RED: 明细条数与 total_tasks 冲突
    try:
        derive_threshold([{"results": [{"ok": True}] * 10}], 36)
        check("RED 明细条数冲突拒绝", False)
    except ValueError as exc:
        check("RED 明细条数冲突拒绝", True, str(exc)[:40])

    print(f"\nSELFTEST {'PASS' if not failures else 'FAIL'}: {checks - len(failures)}/{checks} 通过")
    return 0 if not failures else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="从历史 run 推导门禁阈值")
    ap.add_argument("--runs", nargs="*", help="显式基线文件；缺省按 BASELINE_GLOBS 扫")
    ap.add_argument("--safety-tasks", type=int, default=1, help="允许比历史最差再劣化的题数（默认 1）")
    ap.add_argument("--out", default=str(ROOT / "results" / "gate-policy.json"))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    try:
        if args.runs:
            by_total: dict[int, dict] = {}
            for p in map(Path, args.runs):
                total = len(json.loads(p.read_text(encoding="utf-8"))["results"])
                by_total.setdefault(total, []).append(p)
            policy = {"rule": RULE, "by_total": {
                str(t): derive_from_files(ps, t, args.safety_tasks) for t, ps in sorted(by_total.items())}}
        else:
            policy = {"rule": RULE, "by_total": {
                str(t): v for t, v in collect().items()}}
    except (ValueError, KeyError, OSError) as exc:
        print(f"无法推导阈值: {exc}", file=sys.stderr)
        return 2

    policy["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{'题数':<6} {'n':>2} {'最低通过':>8} {'mean':>7} {'sd':>7} {'阈值':>8}")
    for total, d in policy["by_total"].items():
        print(f"{total:<6} {d['n_runs']:>2} {d['min_passed']:>8} {d['mean_pass_rate']:>7.4f} "
              f"{d['sd_pass_rate']:>7.4f} {d['threshold']:>8.4f}")
    print(f"\n规则: {RULE}")
    print(f"policy -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
