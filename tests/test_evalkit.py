#!/usr/bin/env python3
"""纯函数单测：工具安全 / 换算正确性 / 评分器与门禁。RED 注入必须失败。"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evalkit.agent import strip_thinking
from evalkit.grade import answer_matches, evaluate_gate, grade_task
from evalkit.tools import ToolError, calc, convert, dispatch, lookup
from trace_summary import aggregate


class TestStripThinking(unittest.TestCase):
    """Qwen3 这类模型会把思考过程塞进 content，最终答案必须剥掉再评分。"""

    def test_removes_closed_block(self):
        self.assertEqual(strip_thinking("我们先算 128*37\n</think>\n43"), "43")

    def test_removes_multiple_blocks(self):
        self.assertEqual(strip_thinking("a\n</think>\nb\n</think>\n43"), "43")

    def test_unterminated_block_yields_empty(self):
        # 被 max_tokens 截断、只有 <think> 没有收尾：后面全是思考内容，不能当答案
        self.assertEqual(strip_thinking("<think>想了很久 42 但没写完"), "")

    def test_plain_answer_untouched(self):
        self.assertEqual(strip_thinking("  212 "), "212")

    def test_empty_input(self):
        self.assertEqual(strip_thinking(""), "")

class TestDispatchSyntaxError(unittest.TestCase):
    """模型会发来括号不平衡的表达式：SyntaxError 必须包成 ToolError 回灌自纠，
    绝不能逃出 dispatch 把整个任务打成 orchestration_error（本地模型实测踩过）。"""

    def test_unbalanced_paren_becomes_tool_error(self):
        with self.assertRaises(ToolError):
            dispatch("calc", {"expr": "(1+2"})

    def test_message_is_actionable(self):
        try:
            dispatch("calc", {"expr": "(1+2"})
        except ToolError as e:
            self.assertIn("calc", str(e))


class TestAgentTruncatedTurn(unittest.TestCase):
    """finish_reason=length 且无工具调用、无内容 = 思考被截断，不是最终答案。
    Agent 必须把它判成任务失败（orchestration_error），不能拿空串当答案收尾。"""

    def _stub(self, monkey_message):
        from evalkit.llm import LLMResult
        res = LLMResult(message=monkey_message, latency_ms=1.0, attempts=1,
                        prompt_tokens=10, completion_tokens=10, total_tokens=20,
                        cost=0.0, finish_reason="length")
        return res

    def test_truncated_empty_turn_is_failure(self):
        import unittest.mock as mock
        import evalkit.agent as agent_mod
        from evalkit.trace import TraceWriter
        stub = self._stub({"role": "assistant", "content": "", "tool_calls": None})
        with mock.patch.object(agent_mod, "chat_completions", return_value=stub):
            a = agent_mod.Agent(model="m", base_url="http://x", api_key="k")
            with tempfile.TemporaryDirectory() as td:
                tw = TraceWriter(str(Path(td) / "t.jsonl"))
                r = a.run({"id": "t1", "prompt": "x", "expected_answer": "1",
                           "required_tools": []}, tw)
        self.assertFalse(r.ok)
        self.assertEqual(r.error_type, "orchestration_error")
        self.assertIn("截断", r.error or "")

    def test_truncated_with_content_still_final(self):
        # 有实际内容的 length 截断（答案已写完但被打断）仍按旧逻辑收尾评分
        import unittest.mock as mock
        import evalkit.agent as agent_mod
        from evalkit.trace import TraceWriter
        stub = self._stub({"role": "assistant", "content": "42", "tool_calls": None})
        with mock.patch.object(agent_mod, "chat_completions", return_value=stub):
            a = agent_mod.Agent(model="m", base_url="http://x", api_key="k")
            with tempfile.TemporaryDirectory() as td:
                tw = TraceWriter(str(Path(td) / "t.jsonl"))
                r = a.run({"id": "t2", "prompt": "x", "expected_answer": "42",
                           "required_tools": []}, tw)
        self.assertTrue(r.ok)
        self.assertEqual(r.final_answer, "42")

class TestCalc(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(calc("128*37-19"), "4717")
        self.assertEqual(calc("(45+55)*12/3"), "400")
        self.assertEqual(calc("2**10 + 24/8"), "1027")

    def test_float_format(self):
        self.assertEqual(calc("0.1+0.2"), "0.3")   # 浮点陷阱必须被格式化压平
        self.assertEqual(calc("2.5*4 + 10/4"), "12.5")
        self.assertEqual(calc("7/2"), "3.5")

    def test_injection_rejected(self):
        for evil in [
            "__import__('os').system('echo pwned')",
            "open('/etc/passwd')",
            "().__class__.__bases__[0].__subclasses__()",
            "x",
        ]:
            with self.assertRaises(ValueError, msg=evil):
                calc(evil)

    def test_pow_bound(self):
        with self.assertRaises(ValueError):
            calc("2**1000000")


class TestConvert(unittest.TestCase):
    def test_length(self):
        self.assertEqual(convert(3.5, "km", "m"), "3500")

    def test_mass(self):
        self.assertEqual(convert(180, "lb", "kg"), "81.6466266")

    def test_temperature(self):
        self.assertEqual(convert(25, "c", "f"), "77")
        self.assertEqual(convert(0, "c", "k"), "273.15")

    def test_unsupported(self):
        with self.assertRaises(ValueError):
            convert(1, "km", "kg")


class TestLookup(unittest.TestCase):
    def test_hit(self):
        self.assertEqual(lookup("gpu.vram_gb"), "6")

    def test_miss_lists_keys(self):
        with self.assertRaises(KeyError):
            lookup("no.such.key")


class TestDispatch(unittest.TestCase):
    def test_unknown_tool(self):
        with self.assertRaises(ToolError):
            dispatch("rm_rf", {})

    def test_bad_args_become_toolerror(self):
        with self.assertRaises(ToolError):
            dispatch("calc", {"nope": 1})


class TestGrade(unittest.TestCase):
    def setUp(self):
        self.task = {"id": "t1", "expected_answer": "4717", "required_tools": ["calc"]}

    def test_pass(self):
        r = {"final_answer": "4717", "tools_called": ["calc"], "turns": 2, "error_type": None}
        g = grade_task(self.task, r)
        self.assertTrue(g["passed"])

    def test_wrong_answer(self):
        r = {"final_answer": "4716", "tools_called": ["calc"], "turns": 2, "error_type": None}
        g = grade_task(self.task, r)
        self.assertFalse(g["passed"])
        self.assertEqual(g["failure_type"], "wrong_answer")

    def test_missing_tool(self):
        r = {"final_answer": "4717", "tools_called": [], "turns": 1, "error_type": None}
        g = grade_task(self.task, r)
        self.assertFalse(g["passed"])
        self.assertEqual(g["failure_type"], "tool_missing")

    def test_numeric_tolerance_and_units(self):
        self.assertTrue(answer_matches("81.6466266", "81.65 kg"))
        self.assertTrue(answer_matches("0.3", "0.3"))
        self.assertFalse(answer_matches("4717", "4718"))
        self.assertTrue(answer_matches("vLLM", "vllm"))


class TestGate(unittest.TestCase):
    def test_gate_block(self):
        ok, _ = evaluate_gate({"pass_rate": 0.5, "passed": 12, "total": 24}, 0.8)
        self.assertFalse(ok)

    def test_gate_pass(self):
        ok, _ = evaluate_gate({"pass_rate": 0.92, "passed": 22, "total": 24}, 0.8)
        self.assertTrue(ok)

    def test_gate_fail_closed(self):
        for broken in [{"passed": 1, "total": 1}, {"pass_rate": "92%", "passed": 22, "total": 24},
                       {"pass_rate": 1.5, "passed": 24, "total": 24}]:
            ok, _ = evaluate_gate(broken, 0.8)
            self.assertFalse(ok)


class TestTraceAggregate(unittest.TestCase):
    """dup_calls 必须只数「同 tool + 同 args」的重复，不能把正常多次调用算成浪费。"""

    def _write(self, path, events):
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def test_duplicate_call_counted_once(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            self._write(p, [
                {"task_id": "a", "event": "tool_call", "tool": "lookup", "arguments": {"key": "gpu.driver"}},
                {"task_id": "a", "event": "tool_call", "tool": "lookup", "arguments": {"key": "gpu.driver"}},
                {"task_id": "a", "event": "tool_call", "tool": "calc", "arguments": {"expression": "1+2"}},
            ])
            agg = aggregate([p])
            self.assertEqual(agg["per_task"]["a"]["tool_calls"], 3)
            self.assertEqual(agg["per_task"]["a"]["dup_calls"], 1)

    def test_same_tool_different_args_not_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            self._write(p, [
                {"task_id": "b", "event": "tool_call", "tool": "lookup", "arguments": {"key": "a"}},
                {"task_id": "b", "event": "tool_call", "tool": "lookup", "arguments": {"key": "b"}},
                {"task_id": "c", "event": "tool_call", "tool": "lookup", "arguments": {"key": "a"}},
            ])
            agg = aggregate([p])
            self.assertEqual(agg["per_task"]["b"]["dup_calls"], 0)
            # 跨任务同参数不算重复：浪费是按任务算的
            self.assertEqual(agg["per_task"]["c"]["dup_calls"], 0)


class TestSelftestAccounting(unittest.TestCase):
    """自检报的分母必须等于实际跑过的断言数（曾写死 6/6 而实际 8 条）。"""

    def test_denominator_matches_real_checks(self):
        root = Path(__file__).resolve().parents[1]
        p = subprocess.run([sys.executable, str(root / "run_eval.py"), "--selftest"],
                           capture_output=True, text=True, cwd=root)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        lines = p.stdout.splitlines()
        checks = sum(1 for l in lines if l.startswith("[PASS]") or l.startswith("[FAIL]"))
        m = re.search(r"SELFTEST (PASS|FAIL): (\d+)/(\d+) 通过", p.stdout)
        self.assertIsNotNone(m, p.stdout)
        self.assertEqual(m.group(1), "PASS")
        self.assertEqual(int(m.group(2)), checks)
        self.assertEqual(int(m.group(3)), checks)
        self.assertGreater(checks, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
