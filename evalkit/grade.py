"""评分器：数值容差 / 归一化字符串匹配 + 必用工具检查 + 失败归因。"""
from __future__ import annotations

import math
import re

_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _parse_number(text: str) -> float | None:
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        m = _NUM_RE.search(text.replace(",", ""))
        return float(m.group()) if m else None


def _norm(text: str) -> str:
    return text.strip().strip(".").strip("。").strip().lower()


def answer_matches(expected: str, actual: str, *, rel_tol: float = 1e-3, abs_tol: float = 1e-3) -> bool:
    """先做精确归一化比较；任一侧可解析为数值则做容差比较。

    整数答案（期望值里无小数点）必须精确相等——否则 4717 与 4716 会被
    千分之一的相对容差放过；带小数的期望值才允许四舍五入后的答案。
    """
    if _norm(expected) == _norm(actual):
        return True
    e_num, a_num = _parse_number(expected), _parse_number(actual)
    if e_num is None or a_num is None:
        return False
    if "." not in expected:  # 整数答案：不许有任何偏差
        return math.isclose(e_num, a_num, rel_tol=0.0, abs_tol=1e-9)
    return math.isclose(e_num, a_num, rel_tol=rel_tol, abs_tol=abs_tol)


def grade_task(task: dict, result: dict) -> dict:
    """result 是 TaskResult.asdict() 形态（run_agent 落盘后的行）。"""
    failure_type: str | None = None
    reasons: list[str] = []

    if result.get("error_type") in ("api_error", "orchestration_error"):
        failure_type = result["error_type"]
        reasons.append(f"运行失败: {result.get('error')}")
    elif result.get("error_type") == "loop_exceeded":
        failure_type = "loop_exceeded"
        reasons.append("超过最大轮次仍未给出最终答案")
    else:
        # 归因是布尔运算，reasons 只是给人看的渲染层——不拿文案做字符串考古
        expected_tools = set(task.get("required_tools") or [])
        missing = bool(expected_tools - set(result.get("tools_called") or []))
        if missing:
            reasons.append(f"未调用必用工具: {sorted(expected_tools - set(result.get('tools_called') or []))}")
        final = (result.get("final_answer") or "").strip()
        answer_ok = bool(final) and answer_matches(str(task["expected_answer"]), final)
        if not final:
            reasons.append("没有产出最终答案")
            failure_type = "no_answer"
        elif not answer_ok:
            reasons.append(f"答案不符: 期望 {task['expected_answer']!r}，实际 {final!r}")
            failure_type = "wrong_answer"
        elif missing:
            failure_type = "tool_missing"

    passed = failure_type is None
    return {
        "task_id": task["id"],
        "passed": passed,
        "failure_type": failure_type,
        "reasons": reasons,
        "turns": result.get("turns"),
        "tools_called": result.get("tools_called") or [],
        "final_answer": result.get("final_answer"),
        "expected_answer": task["expected_answer"],
    }


def grade_all(tasks: list[dict], results: list[dict]) -> dict:
    by_id = {t["id"]: t for t in tasks}
    grades = []
    for r in results:
        task = by_id.get(r["task_id"])
        if task is None:
            grades.append({"task_id": r["task_id"], "passed": False,
                           "failure_type": "unknown_task", "reasons": ["结果里出现评测集外的任务"]})
            continue
        grades.append(grade_task(task, r))
    n = len(grades)
    passed = sum(1 for g in grades if g["passed"])
    attribution: dict[str, int] = {}
    for g in grades:
        if not g["passed"]:
            key = g["failure_type"] or "unknown"
            attribution[key] = attribution.get(key, 0) + 1
    return {
        "pass_rate": round(passed / n, 4) if n else 0.0,
        "passed": passed,
        "total": n,
        "failure_attribution": attribution,
        "grades": grades,
    }


def evaluate_gate(report: dict, threshold: float) -> tuple[bool, str]:
    """门禁判定。返回 (通过?, 原因)。报告损坏一律 fail-closed。"""
    if not isinstance(report, dict):
        return False, "报告不是 JSON 对象"
    if "pass_rate" not in report or "total" not in report or "passed" not in report:
        return False, "报告缺少 pass_rate/passed/total 字段"
    if not isinstance(report["pass_rate"], (int, float)) or isinstance(report["pass_rate"], bool):
        return False, "pass_rate 不是数值"
    rate = float(report["pass_rate"])
    if not (0.0 <= rate <= 1.0):
        return False, f"pass_rate 越界: {rate}"
    if rate < threshold:
        return False, f"pass_rate {rate:.4f} < 门禁 {threshold:.2f}"
    if report["total"] <= 0:
        return False, "total 为 0，无有效评测"
    return True, f"pass_rate {rate:.4f} ≥ 门禁 {threshold:.2f}"
