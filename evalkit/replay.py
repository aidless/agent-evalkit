"""回放式 mock LLM：从 trace JSONL 重放 llm_call 事件，无 LLM、无 GPU 跑通 agent 全链。

用途：harness 可测试性——run_agent 的编排逻辑（工具分派、错误回灌、截断守卫、
评分）不依赖真实模型也能端到端验证；对已有 run 的 trace 做 replay，
评分结果应与原 run 一致（确定性回归）。
"""
from __future__ import annotations

import json
import pathlib

from .llm import LLMError, LLMResult


class ReplayExhausted(LLMError):
    """trace 里该任务的 llm_call 已耗尽（agent 循环与原 run 不一致）。"""


# llm.py 的 LLMResult 需要的位置参数顺序见其 dataclass 定义
_FIELDS = ("message", "latency_ms", "attempts", "prompt_tokens",
           "completion_tokens", "total_tokens", "cost", "finish_reason")


class ReplayLLM:
    """按 (task_id, 调用序) 重放 trace 中的 llm_call。

    与 Agent 的接缝：签名与 llm.chat_completions 相同的关键字接口，
    Agent.__init__ 注入 llm_fn=self 即可；回放后端必须实现 set_task(task_id)
    （Agent.run 开始时调用，切换重放游标），见 agent.py 的 llm_fn 契约说明。
    """

    def __init__(self, trace_path: str | pathlib.Path):
        self._calls: dict[str, list[dict]] = {}
        self._idx: dict[str, int] = {}
        with pathlib.Path(trace_path).open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                if ev.get("event") == "llm_call":
                    self._calls.setdefault(ev["task_id"], []).append(ev)

    def set_task(self, task_id: str) -> None:
        """切换当前重放任务（Agent.run 开始时调用），重置游标。"""
        self._current = task_id
        self._idx[task_id] = 0

    def __call__(self, *, messages, tools, model, base_url, api_key,
                 timeout=60.0, max_tokens=512, max_retries=3, **_):
        tid = getattr(self, "_current", None)
        seq = self._calls.get(tid)
        if not seq:
            raise ReplayExhausted(f"trace 中没有任务 {tid!r} 的 llm_call")
        i = self._idx.get(tid, 0)
        if i >= len(seq):
            raise ReplayExhausted(f"任务 {tid!r} 的 llm_call 已耗尽（第 {i + 1} 次调用）")
        ev = seq[i]
        self._idx[tid] = i + 1
        full = ev.get("tool_calls_full")
        if ev.get("tool_calls") and not full:
            raise ReplayExhausted(
                f"trace 过旧（无 tool_calls_full 字段），无法重放任务 {tid!r} 的工具调用")
        tool_calls = [
            {"id": tc.get("id") or f"replay-{tid}-{i}-{j}",
             "type": "function",
             "function": {"name": tc["name"], "arguments": tc["arguments"]}}
            for j, tc in enumerate(full or [])]
        return LLMResult(
            message={"role": "assistant",
                     # content_full 是 agent 落盘的全量原文；旧 trace 无此字段时
                     # 只能降级用截断的 content（>300 字符答案的重放会失真，
                     # 重跑该任务收集新 trace 才能恢复确定性）。
                     "content": ev.get("content_full", ev.get("content")),
                     "tool_calls": tool_calls or None},
            latency_ms=ev.get("latency_ms", 0.0),
            attempts=1,
            prompt_tokens=ev.get("prompt_tokens", 0),
            completion_tokens=ev.get("completion_tokens", 0),
            total_tokens=ev.get("total_tokens",
                                ev.get("prompt_tokens", 0) + ev.get("completion_tokens", 0)),
            cost=ev.get("cost", 0.0),
            finish_reason=ev.get("finish_reason", "stop"),
        )
