"""OpenAI 兼容 chat/completions 客户端：urllib 实现，超时 + 指数退避重试 + 用量解析。"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict


@dataclass
class LLMResult:
    message: dict
    latency_ms: float
    attempts: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float
    finish_reason: str

    def asdict(self) -> dict:
        return asdict(self)


class LLMError(Exception):
    """API 调用最终失败（重试耗尽 / 响应不可解析）。"""


_RETRYABLE_HTTP = {408, 409, 425, 429, 500, 502, 503, 504}


def chat_completions(
    *,
    messages: list[dict],
    tools: list[dict],
    model: str,
    base_url: str,
    api_key: str,
    timeout: float = 60.0,
    max_retries: int = 3,
    temperature: float = 0.0,
    max_tokens: int = 512,
) -> LLMResult:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_err: Exception | None = None

    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            latency_ms = (time.perf_counter() - started) * 1000
            data = json.loads(raw)
            choice = data["choices"][0]
            usage = data.get("usage") or {}
            return LLMResult(
                message=choice["message"],
                latency_ms=round(latency_ms, 2),
                attempts=attempt,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
                cost=float(usage.get("cost") or 0),
                finish_reason=str(choice.get("finish_reason") or ""),
            )
        except urllib.error.HTTPError as exc:
            last_err = exc
            detail = exc.read()[:400].decode("utf-8", "replace")
            if exc.code not in _RETRYABLE_HTTP:
                raise LLMError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError) as exc:
            last_err = exc
        if attempt < max_retries:
            time.sleep(min(2 ** (attempt - 1), 8))

    raise LLMError(f"重试 {max_retries} 次后仍失败: {type(last_err).__name__}: {last_err}")
