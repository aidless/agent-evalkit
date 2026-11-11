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
from evalkit.tools import ToolError, calc, convert, dispatch, dispatch_via, lookup
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


class TestAuditTools(unittest.TestCase):
    """audit 工具集：规则查询、未知规则报错、严重级词表。事实全部从 tmaudit 源码导出。"""

    def test_rule_lookup_has_severity_and_tier(self):
        from evalkit.audit_tools import audit_rule
        import json
        r = json.loads(audit_rule("C4"))
        self.assertEqual(r["id"], "C4")
        self.assertEqual(r["evidence_tier"], "CONFIRMED")
        self.assertEqual(r["default_severity"], "MEDIUM")

    def test_unknown_rule_lists_available(self):
        from evalkit.audit_tools import audit_rule
        from evalkit.tools import ToolError
        with self.assertRaises(ToolError) as cm:
            audit_rule("C11")
        self.assertIn("C10", str(cm.exception))

    def test_severities_lists_both_vocabularies(self):
        from evalkit.audit_tools import audit_severities
        import json
        s = json.loads(audit_severities())
        self.assertEqual(s["valid_severities"], ["HIGH", "MEDIUM", "LOW"])
        self.assertEqual(s["evidence_tiers"], ["CONFIRMED", "PARTIAL", "UNSUBSTANTIATED"])
        self.assertIn("PARTIAL", s["tier_meaning"])

    def test_audit_dispatch_wraps_errors(self):
        from evalkit.audit_tools import AUDIT_DISPATCH
        from evalkit.tools import ToolError
        with self.assertRaises(ToolError):
            AUDIT_DISPATCH["audit_rule"](rule_id="C99")


class TestAgentToolset(unittest.TestCase):
    """Agent 可选工具集（tools / audit）；未知工具集必须报错。"""

    def test_toolset_selection(self):
        from evalkit.agent import _toolset
        schemas, dispatch_fn = _toolset("tools")
        self.assertEqual(schemas[0]["function"]["name"], "calc")
        schemas, dispatch_fn = _toolset("audit")
        self.assertEqual(schemas[0]["function"]["name"], "audit_rule")
        self.assertIn("audit_severities", {s["function"]["name"] for s in schemas})

    def test_unknown_toolset_rejected(self):
        from evalkit.agent import _toolset
        with self.assertRaises(ValueError):
            _toolset("nonexistent")


class TestPrefixMetrics(unittest.TestCase):
    """prefix-cache 命中率：Prometheus 文本解析 + 前后差值。网关无 /metrics 时降级为 None。"""

    PROM = '''# HELP vllm:prefix_cache_queries_total ...
vllm:prefix_cache_queries_total{engine="0",model_name="Qwen3-4B-AWQ"} 100.0
vllm:prefix_cache_queries_created{engine="0",model_name="Qwen3-4B-AWQ"} 1.79e+09
vllm:prefix_cache_hits_total{engine="0",model_name="Qwen3-4B-AWQ"} 41.0
vllm:prefix_cache_hits_created{engine="0",model_name="Qwen3-4B-AWQ"} 1.79e+09
'''

    def test_parse_metric_reads_value(self):
        from evalkit.metrics import parse_metric
        self.assertEqual(parse_metric(self.PROM, "vllm:prefix_cache_queries_total"), 100.0)
        self.assertEqual(parse_metric(self.PROM, "vllm:prefix_cache_hits_total"), 41.0)

    def test_parse_metric_missing_returns_none(self):
        from evalkit.metrics import parse_metric
        self.assertIsNone(parse_metric(self.PROM, "vllm:nope_total"))
        # created 后缀同名指标不许误读
        self.assertIsNone(parse_metric(self.PROM, "vllm:prefix_cache_queries_created_total"))


class TestContentFullReplay(unittest.TestCase):
    """Carmack 命中的真缺陷：trace 落盘 content[:300] 截断，回放拿截断 content 重放，
    答案 >300 字符的任务 replay 评分与原 run 必然不一致——确定性承诺是假的。
    修复后：trace 存 content_full（全量），replay 优先取 full，旧 trace 降级截断。"""

    LONG_ANSWER = "最终答案：vLLM 三档并发实测 " + "数字很长的结论句子。" * 30  # >300 字符

    def _fake_llm(self):
        from evalkit.llm import LLMResult
        return LLMResult(
            message={"role": "assistant", "content": self.LONG_ANSWER, "tool_calls": None},
            latency_ms=1.0, attempts=1, prompt_tokens=10, completion_tokens=50,
            total_tokens=60, cost=0.0, finish_reason="stop")

    def test_trace_stores_full_and_display_content(self):
        import unittest.mock as mock
        import evalkit.agent as agent_mod
        from evalkit.trace import TraceWriter, read_events
        with mock.patch.object(agent_mod, "chat_completions", return_value=self._fake_llm()):
            a = agent_mod.Agent(model="m", base_url="http://x", api_key="k")
            with tempfile.TemporaryDirectory() as td:
                tp = str(Path(td) / "t.jsonl")
                tw = TraceWriter(tp)
                r = a.run({"id": "t1", "prompt": "x", "expected_answer": self.LONG_ANSWER,
                           "required_tools": []}, tw)
                evs = [e for e in read_events(tp) if e["event"] == "llm_call"]
        self.assertTrue(r.ok)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].get("content_full"), self.LONG_ANSWER)   # 全量，可重放
        self.assertEqual(evs[0]["content"], self.LONG_ANSWER[:300])      # 展示字段仍截断

    def test_replay_long_answer_matches_original(self):
        import unittest.mock as mock
        import evalkit.agent as agent_mod
        from evalkit.trace import TraceWriter
        from evalkit.replay import ReplayLLM
        task = {"id": "t1", "prompt": "x", "expected_answer": self.LONG_ANSWER,
                "required_tools": []}
        with tempfile.TemporaryDirectory() as td:
            tp = str(Path(td) / "t.jsonl")
            tw = TraceWriter(tp)
            with mock.patch.object(agent_mod, "chat_completions", return_value=self._fake_llm()):
                r1 = agent_mod.Agent(model="m", base_url="http://x", api_key="k").run(task, tw)
            # 回放：不碰网络，重放同一条 trace
            r2 = agent_mod.Agent(model="m", base_url="http://x", api_key="k",
                                 llm_fn=ReplayLLM(tp)).run(task, tw)
        self.assertTrue(r1.ok)
        self.assertTrue(r2.ok)
        self.assertEqual(r1.final_answer, r2.final_answer)   # 截断 bug 在这里必现


class TestDispatchViaRegistry(unittest.TestCase):
    """Linus 命中：dispatch_via 收了 table 参数却在错误路径写死全局 IMPLEMENTATIONS，
    audit 工具集未知工具时会报出 calc/lookup/convert 可用——骗模型。"""

    def test_unknown_tool_lists_table_not_global(self):
        table = {"audit_rule": lambda rule_id: "ok"}
        with self.assertRaises(ToolError) as cm:
            dispatch_via(table, "nope", {})
        self.assertIn("audit_rule", str(cm.exception))
        self.assertNotIn("calc", str(cm.exception))


class TestGradeAttributionMatrix(unittest.TestCase):
    """grade_task 归因重构（字符串匹配 → 布尔推导）前先钉死 2×2 行为，防静默漂移。"""

    def setUp(self):
        self.task = {"id": "t1", "expected_answer": "4717", "required_tools": ["calc"]}

    def _grade(self, tools_called, final_answer):
        return grade_task(self.task, {"final_answer": final_answer,
                                      "tools_called": tools_called, "turns": 2,
                                      "error_type": None})

    def test_missing_and_wrong_is_wrong_answer(self):
        self.assertEqual(self._grade([], "4716")["failure_type"], "wrong_answer")

    def test_missing_and_correct_is_tool_missing(self):
        self.assertEqual(self._grade([], "4717")["failure_type"], "tool_missing")

    def test_missing_and_no_answer_is_no_answer(self):
        self.assertEqual(self._grade([], "")["failure_type"], "no_answer")

    def test_called_and_wrong_is_wrong_answer(self):
        self.assertEqual(self._grade(["calc"], "4716")["failure_type"], "wrong_answer")


class TestAgentLlmFnContract(unittest.TestCase):
    """Karpathy 命中：llm_fn 隐式契约必须写进 docstring，且文档不能指向不存在的机制。"""

    def test_agent_docstring_documents_set_task(self):
        import evalkit.agent as agent_mod
        self.assertIn("set_task", (agent_mod.Agent.__doc__ or ""))

    def test_replay_docstring_points_to_set_task(self):
        from evalkit.replay import ReplayLLM
        self.assertIn("set_task", (ReplayLLM.__doc__ or ""))
        self.assertNotIn("_replay_task_id", ReplayLLM.__doc__ or "")

    def test_plain_llm_fn_without_set_task_works(self):
        import unittest.mock as mock
        import evalkit.agent as agent_mod
        from evalkit.trace import TraceWriter
        from evalkit.llm import LLMResult
        stub = LLMResult(message={"role": "assistant", "content": "42", "tool_calls": None},
                         latency_ms=1.0, attempts=1, prompt_tokens=10, completion_tokens=2,
                         total_tokens=12, cost=0.0, finish_reason="stop")
        fn = lambda **kwargs: stub  # 无 set_task 属性的普通函数 = 非回放后端
        with tempfile.TemporaryDirectory() as td:
            tw = TraceWriter(str(Path(td) / "t.jsonl"))
            r = agent_mod.Agent(model="m", base_url="http://x", api_key="k",
                                llm_fn=fn).run(
                {"id": "t1", "prompt": "x", "expected_answer": "42", "required_tools": []}, tw)
        self.assertTrue(r.ok)
        self.assertEqual(r.final_answer, "42")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestGatePolicy(unittest.TestCase):
    """门禁阈值必须从历史无回归 run 推导，不许拍脑袋写 0.80。

    规则（写死在 derive_gate.RULE）：threshold = (最低历史通过题数 - 安全余量) / 总题数。
    语义：允许比历史最差再劣化「安全余量」条题；再劣化 1 条即阻断。
    """

    def _run(self, passed, total=36):
        return {"passed": passed, "total": total, "pass_rate": passed / total}

    def test_derive_from_baseline(self):
        from derive_gate import derive_threshold

        runs = [self._run(34), self._run(34), self._run(34), self._run(36), self._run(36)]
        out = derive_threshold(runs, total_tasks=36, safety_tasks=1)
        self.assertEqual(out["min_passed"], 34)
        self.assertEqual(out["threshold"], 33 / 36)  # 0.9167
        self.assertEqual(out["n_runs"], 5)

    def test_safety_zero_is_strictest(self):
        from derive_gate import derive_threshold

        out = derive_threshold([self._run(34), self._run(36)], total_tasks=36, safety_tasks=0)
        self.assertEqual(out["threshold"], 34 / 36)

    def test_perfect_baseline(self):
        from derive_gate import derive_threshold

        out = derive_threshold([self._run(36), self._run(36)], total_tasks=36, safety_tasks=1)
        self.assertEqual(out["threshold"], 35 / 36)

    def test_threshold_blocks_one_more_failure(self):
        """劣化到 32/36 必须阻断，33/36 放行——这是规则的可执行语义。"""
        from derive_gate import derive_threshold
        from evalkit.grade import evaluate_gate

        th = derive_threshold([self._run(34), self._run(36)], total_tasks=36)["threshold"]
        ok_33, _ = evaluate_gate(self._run(33), th)
        ok_32, why = evaluate_gate(self._run(32), th)
        self.assertTrue(ok_33)
        self.assertFalse(ok_32)
        self.assertIn("门禁", why)

    def test_fail_closed_on_bad_input(self):
        from derive_gate import derive_threshold

        for bad in [[], [{"total": 36}], [{"passed": "34", "total": 36}], [{"passed": 34, "total": 24}]]:
            with self.assertRaises(ValueError):
                derive_threshold(bad, total_tasks=36)

    def test_derive_from_files_records_inputs(self):
        """落盘策略必须带每个输入 run 的通过数，任何人可重算。"""
        import tempfile

        from derive_gate import derive_from_files

        with tempfile.TemporaryDirectory() as td:
            paths = []
            for i, passed in enumerate([34, 36]):
                p = Path(td) / f"run-{i}.json"
                p.write_text(json.dumps({"run_id": f"r{i}", "total_tasks": 36,
                                         "results": [{"ok": True}] * passed +
                                                    [{"ok": False}] * (36 - passed)}),
                             encoding="utf-8")
                paths.append(p)
            out = derive_from_files(paths, total_tasks=36)
        self.assertEqual(out["threshold"], 33 / 36)
        self.assertEqual([r["passed"] for r in out["baseline_runs"]], [34, 36])


class TestWatchAlert(unittest.TestCase):
    """--watch 是监控接入点：每次评测都发一条记录，越线额外告警；投递失败 fail-closed。"""

    def _failing_run_file(self, td: Path) -> Path:
        src = Path(__file__).resolve().parents[1] / "results" / "run-gw5.json"
        run = json.loads(src.read_text(encoding="utf-8"))
        p = td / "run.json"
        p.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
        return p

    def test_watch_posts_alert_and_writes_jsonl(self):
        import http.server
        import threading

        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                received.append(json.loads(self.rfile.read(n)))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        try:
            import run_eval

            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                run_file = self._failing_run_file(td)
                alerts = td / "alerts.jsonl"
                argv = sys.argv
                sys.argv = ["run_eval.py", "--replay", str(run_file), "--gate", "1.0",
                            "--watch", "--alert-url", f"http://127.0.0.1:{port}/hook",
                            "--alerts-file", str(alerts)]
                try:
                    rc = run_eval.main()
                finally:
                    sys.argv = argv
                self.assertEqual(rc, 1)
                self.assertEqual(len(received), 1, "webhook 必须真收到一次 POST")
                self.assertAlmostEqual(received[0]["pass_rate"], 0.9444, places=4)
                self.assertTrue(received[0]["blocked"])
                lines = [json.loads(x) for x in alerts.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(lines), 1)
                self.assertEqual(lines[0]["run_id"], received[0]["run_id"])
        finally:
            srv.shutdown()

    def test_watch_delivery_failure_is_fail_closed(self):
        import run_eval

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            run_file = self._failing_run_file(td)
            argv = sys.argv
            sys.argv = ["run_eval.py", "--replay", str(run_file), "--gate", "1.0",
                        "--watch", "--alert-url", "http://127.0.0.1:1/hook",
                        "--alerts-file", str(td / "alerts.jsonl")]
            try:
                rc = run_eval.main()
            finally:
                sys.argv = argv
        self.assertEqual(rc, 2, "告警投递失败必须与门禁阻断（exit 1）区分")


class TestMineFailures(unittest.TestCase):
    """新题必须从真实失败长出：题干来自 trace 的失败实例，expected 由工具现算。"""

    def _fixture(self, td: Path):
        """造一个真失败的 run + trace：calc 括号未闭合 + audit 查错规则。"""
        trace = td / "trace.jsonl"
        events = [
            {"task_id": "calc-x", "event": "tool_call", "turn": 1, "tool": "calc",
             "arguments": {"expr": "((12+8)*5"}, "seq": 1},
            {"task_id": "calc-x", "event": "tool_result", "turn": 1, "tool": "calc",
             "result": "错误: calc 执行失败: '(' was never closed", "seq": 2},
            {"task_id": "audit-x", "event": "tool_call", "turn": 1, "tool": "audit_rule",
             "arguments": {"rule_id": "C3"}, "seq": 3},
        ]
        with trace.open("w", encoding="utf-8") as f:
            for i, ev in enumerate(events):
                ev.setdefault("ts", f"2026-11-11T00:00:0{i}Z")
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        run = td / "run.json"
        run.write_text(json.dumps({
            "run_id": "fixture-run", "trace_file": str(trace), "total_tasks": 2,
            "results": [
                {"task_id": "calc-x", "ok": False, "final_answer": None,
                 "turns": 2, "tools_called": ["calc"], "error": "max_tokens 截断",
                 "error_type": "orchestration_error"},
                {"task_id": "audit-x", "ok": True, "final_answer": "C3",
                 "turns": 2, "tools_called": ["audit_rule"]},
            ]}, ensure_ascii=False), encoding="utf-8")
        tasks = td / "tasks.jsonl"
        with tasks.open("w", encoding="utf-8") as f:
            f.write(json.dumps({"id": "calc-x", "prompt": "计算 ((12+8)*5",
                                "expected_answer": "100", "required_tools": ["calc"],
                                "category": "calc"}) + "\n")
            f.write(json.dumps({"id": "audit-x", "prompt": "符号 R 未定义属于哪条规则",
                                "expected_answer": "C1", "required_tools": ["audit_rule"],
                                "category": "audit"}) + "\n")
        return run, tasks, trace

    def test_mined_task_expected_comes_from_tool(self):
        from evalkit.audit_tools import RULES
        from evalkit.tools import dispatch
        from mine_failures import mine

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            run, tasks, trace = self._fixture(td)
            out = mine([run], tasks_file=tasks, per_mode=2)
        self.assertGreaterEqual(len(out["tasks"]), 2)
        for t in out["tasks"]:
            args = t["tool_args"]
            if t["required_tools"] == ["calc"]:
                self.assertEqual(t["expected_answer"], dispatch("calc", args),
                                 f"expected 必须等于工具现算: {t['id']}")
            else:  # audit 题的标准答案是「id 空格 层级」，必须由 RULES 推导
                rule_id = args["rule_id"]
                self.assertEqual(t["expected_answer"],
                                 f"{rule_id} {RULES[rule_id]['evidence_tier']}")
            self.assertIn("provenance", t)
            self.assertTrue(t["provenance"]["source_task"])
            self.assertTrue(t["provenance"]["failure_type"])
        modes = {t["provenance"]["failure_type"] for t in out["tasks"]}
        self.assertIn("tool_error", modes)
        self.assertIn("wrong_answer", modes)

    def test_audit_candidate_avoids_rules_already_queried(self):
        from mine_failures import mine

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            run, tasks, trace = self._fixture(td)
            out = mine([run], tasks_file=tasks, per_mode=2)
        audit = [t for t in out["tasks"] if t["category"] == "audit"]
        self.assertTrue(audit)
        for t in audit:
            self.assertNotEqual(t["tool_args"]["rule_id"], "C3",
                                "已在源 trace 查过的规则不算新证据")

    def test_empty_run_fails_closed(self):
        from mine_failures import mine

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            run, tasks, _ = self._fixture(td)
            bad = td / "bad.json"
            bad.write_text(json.dumps({"run_id": "x", "results": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                mine([bad], tasks_file=tasks)
