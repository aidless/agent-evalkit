"""vLLM /metrics（Prometheus 文本）的最小解析：只为 prefix-cache 命中率这一格服务。

这是 L3（上下文工程）的最小件：不做多 agent、不做记忆，只把「系统提示词前缀
被 KV cache 复用了多少」变成 trace/run JSON 里的一个可复核数字。
"""
from __future__ import annotations

import urllib.request


def fetch_metrics(url: str, timeout: float = 5.0) -> str | None:
    """拉 /metrics 文本；任何失败（网关没有该端点、超时）都返回 None，不抛。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def parse_metric(text: str, name: str) -> float | None:
    """取 `<name>{...} <value>` 的值；带标签行才算。_created 同名家族不许误读。

    语义：调用方传完整指标名（如 vllm:prefix_cache_queries_total）；
    行按「指标名{...} 值」切，前缀必须整词匹配到 '{' 或空格，避免
    queries_total 匹配到 queries_created_total 之类的近邻。
    """
    value = None
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        # 整词匹配：指标名后必须紧跟 '{' 或空白
        if line.startswith(name + "{") or line.startswith(name + " "):
            try:
                value = float(line.rsplit(" ", 1)[1])
            except (IndexError, ValueError):
                return None
    return value


def snapshot(url: str) -> dict | None:
    """一次 (queries, hits) 快照；/metrics 不可达或指标缺失返回 None。"""
    text = fetch_metrics(url)
    if text is None:
        return None
    q = parse_metric(text, "vllm:prefix_cache_queries_total")
    h = parse_metric(text, "vllm:prefix_cache_hits_total")
    if q is None or h is None:
        return None
    return {"queries": q, "hits": h}


def diff_rate(before: dict | None, after: dict | None) -> float | None:
    """run 前后差值 → 命中率；任一侧缺、或窗口内零查询（除零无意义）都返回 None。"""
    if before is None or after is None:
        return None
    dq = after["queries"] - before["queries"]
    dh = after["hits"] - before["hits"]
    if dq <= 0:
        return None
    return round(dh / dq, 4)
