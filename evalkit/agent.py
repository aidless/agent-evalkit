"""Agent 循环：消息 -> 工具调用 -> 最终答案。轮次上限、错误兜底、全程 trace。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .llm import LLMError, chat_completions
from .tools import ToolError
from .trace import TraceWriter

SYSTEM_PROMPT = (
    "你是一个严谨的工具调用助手。规则：\n"
    "1) 任何算术计算必须调用 calc，任何事实查询必须调用 lookup，任何单位换算必须调用 convert；"
    "禁止心算、禁止凭记忆回答。\n"
    "2) 工具返回错误文本时，读取错误信息并自行纠正后重试。\n"
    "3) 得到所需全部信息后，只用一行输出最终答案：只含答案本身，不要单位、不要解释、不要标点。"
)

_THINK_CLOSE = "</think>"


def strip_thinking(text: str) -> str:
    """剥掉 Qwen3 这类模型的思考块，只留最终答案。

    两条判据：① 只要出现过收尾标记，最后一段收尾之后的文本才是答案
    （前面的成对思考块、残缺块、多余标记都属于思考）；② 开了 `<think>`
    却没有收尾，说明被 max_tokens 截断，后面全是思考内容，不能当答案。
    """
    if not text:
        return ""
    idx = text.rfind(_THINK_CLOSE)
    if idx != -1:
        text = text[idx + len(_THINK_CLOSE) :]
    if "<think" in text.lower():
        return ""
    return text.strip()


@dataclass
class TaskResult:
    task_id: str
    ok: bool
    final_answer: str | None = None
    turns: int = 0
    tools_called: list[str] = field(default_factory=list)
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    llm_latency_ms: list[float] = field(default_factory=list)
    error: str | None = None
    error_type: str | None = None


def _toolset(toolset: str):
    """按名选工具集：tools=计算/换算/查语料；audit=tmaudit 规则语义查询。"""
    if toolset == "tools":
        from .tools import TOOL_SCHEMAS, dispatch
        return TOOL_SCHEMAS, dispatch
    if toolset == "audit":
        from .audit_tools import AUDIT_SCHEMAS, dispatch_audit
        return AUDIT_SCHEMAS, dispatch_audit
    raise ValueError(f"未知工具集 {toolset!r}；可用: tools, audit")


class Agent:
    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        max_turns: int = 6,
        timeout: float = 60.0,
        max_tokens: int = 512,
        max_retries: int = 3,
        toolset: str = "tools",
    ):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self.toolset = toolset
        self._schemas, self._dispatch = _toolset(toolset)

    def run(self, task: dict, trace: TraceWriter) -> TaskResult:
        result = TaskResult(task_id=task["id"], ok=False)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task["prompt"]},
        ]
        tools_called: list[str] = []
        latencies: list[float] = []

        try:
            for turn in range(1, self.max_turns + 1):
                result.turns = turn
                llm = chat_completions(
                    messages=messages,
                    tools=self._schemas,
                    model=self.model,
                    base_url=self.base_url,
                    api_key=self.api_key,
                    timeout=self.timeout,
                    max_tokens=self.max_tokens,
                    max_retries=self.max_retries,
                )
                result.llm_calls += 1
                result.prompt_tokens += llm.prompt_tokens
                result.completion_tokens += llm.completion_tokens
                result.cost += llm.cost
                latencies.append(llm.latency_ms)
                trace.emit(
                    task_id=task["id"],
                    event="llm_call",
                    turn=turn,
                    model=self.model,
                    latency_ms=llm.latency_ms,
                    attempts=llm.attempts,
                    prompt_tokens=llm.prompt_tokens,
                    completion_tokens=llm.completion_tokens,
                    total_tokens=llm.total_tokens,
                    cost=llm.cost,
                    finish_reason=llm.finish_reason,
                    tool_calls=[tc["function"]["name"] for tc in (llm.message.get("tool_calls") or [])],
                    content=(llm.message.get("content") or "")[:300],
                )

                tool_calls = llm.message.get("tool_calls") or []
                if not tool_calls:
                    raw = (llm.message.get("content") or "").strip()
                    if llm.finish_reason == "length" and not strip_thinking(raw):
                        # 思考被 max_tokens 截断、既没调工具也没写答案：
                        # 这是输出预算耗尽，不是最终答案，拿空串收尾会污染 pass_rate。
                        result.error = (
                            f"输出被 max_tokens={self.max_tokens} 截断且无工具调用"
                            "（思考未闭合，finish_reason=length）"
                        )
                        result.error_type = "orchestration_error"
                        trace.emit(task_id=task["id"], event="error", turn=turn,
                                   error=result.error, finish_reason=llm.finish_reason)
                        return result
                    final = strip_thinking(raw)
                    result.final_answer = final
                    result.ok = True
                    result.tools_called = tools_called
                    result.llm_latency_ms = latencies
                    trace.emit(task_id=task["id"], event="final", turn=turn, answer=final)
                    return result

                messages.append(llm.message)
                for tc in tool_calls:
                    fn = tc["function"]
                    name = fn["name"]
                    tools_called.append(name)
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                        if not isinstance(args, dict):
                            raise ValueError("参数必须是 JSON 对象")
                    except (json.JSONDecodeError, ValueError) as exc:
                        args_error = f"参数解析失败: {exc}"
                        trace.emit(task_id=task["id"], event="tool_call", turn=turn,
                                   tool=name, arguments=fn.get("arguments"), parse_error=args_error)
                        messages.append({"role": "tool", "tool_call_id": tc["id"],
                                         "content": f"错误: {args_error}"})
                        continue
                    trace.emit(task_id=task["id"], event="tool_call", turn=turn,
                               tool=name, arguments=args)
                    try:
                        out = self._dispatch(name, args)
                    except ToolError as exc:
                        out = f"错误: {exc}"
                    trace.emit(task_id=task["id"], event="tool_result", turn=turn,
                               tool=name, result=str(out)[:300])
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(out)})

            # 轮次耗尽
            result.error_type = "loop_exceeded"
            result.error = f"超过最大轮次 {self.max_turns}"
            trace.emit(task_id=task["id"], event="error", turn=self.max_turns,
                       error_type="loop_exceeded", error=result.error)

        except LLMError as exc:
            result.error_type = "api_error"
            result.error = str(exc)
            trace.emit(task_id=task["id"], event="error", turn=result.turns,
                       error_type="api_error", error=str(exc))
        except Exception as exc:  # 编排层兜底：任何未预期异常都落 trace，不中断整体评测
            result.error_type = "orchestration_error"
            result.error = f"{type(exc).__name__}: {exc}"
            trace.emit(task_id=task["id"], event="error", turn=result.turns,
                       error_type="orchestration_error", error=result.error)

        result.tools_called = tools_called
        result.llm_latency_ms = latencies
        return result
