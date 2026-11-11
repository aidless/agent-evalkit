"""确定性工具集。零第三方依赖，任何计算/查询/换算都必须经过这里，禁止模型心算。"""
from __future__ import annotations

import ast
import json
import math
import operator
from pathlib import Path

CORPUS_PATH = Path(__file__).resolve().parents[1] / "evals" / "corpus.json"

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Load)


def fmt_number(x: float) -> str:
    """把浮点结果压成稳定可读的字符串：0.1+0.2 -> '0.3'，2.0 -> '2'。"""
    if isinstance(x, complex) or math.isnan(x) or math.isinf(x):
        raise ValueError("结果不是有限实数")
    if abs(x - round(x)) < 1e-12:
        return str(int(round(x)))
    return str(round(x, 10))


def _eval_node(node: ast.AST) -> float:
    if not isinstance(node, _ALLOWED_NODES):
        raise ValueError(f"不允许的语法节点: {type(node).__name__}")
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
            raise ValueError("只允许数值常量")
        return float(node.value)
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"不允许的运算符: {type(node.op).__name__}")
        # 幂运算加幅度上限，避免 2**1000000 把内存打满
        if isinstance(node.op, ast.Pow):
            right = _eval_node(node.right)
            if abs(right) > 64:
                raise ValueError("指数绝对值不得超过 64")
        return op(_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"不允许的一元运算符: {type(node.op).__name__}")
        return op(_eval_node(node.operand))
    raise ValueError(f"不允许的语法节点: {type(node).__name__}")


def calc(expr: str) -> str:
    """安全的算术求值：AST 白名单，无 eval / 无名字 / 无函数调用。"""
    tree = ast.parse(expr, mode="eval")
    value = _eval_node(tree)
    if abs(value) > 1e15:
        raise ValueError("结果超出范围")
    return fmt_number(value)


def _corpus() -> dict:
    with CORPUS_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def lookup(key: str) -> str:
    """从冻结事实库按键取值；未命中时列出可用键，供 agent 自行纠错重试。"""
    data = _corpus()
    if key in data:
        return str(data[key])
    keys = ", ".join(sorted(data.keys()))
    raise KeyError(f"未知键 {key!r}；可用键: {keys}")


# 长度（基准米）与质量（基准克）分开，禁止跨量纲静默换算
_LENGTH = {"mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0}
_MASS = {"mg": 0.001, "g": 1.0, "kg": 1000.0, "lb": 453.59237}


def convert(value: float, src: str, dst: str) -> str:
    """长度/质量换算与摄氏-华氏-开尔文换算。"""
    src, dst = src.strip().lower(), dst.strip().lower()
    v = float(value)
    if src in _LENGTH and dst in _LENGTH:
        return fmt_number(v * _LENGTH[src] / _LENGTH[dst])
    if src in _MASS and dst in _MASS:
        return fmt_number(v * _MASS[src] / _MASS[dst])
    if {src, dst} <= {"c", "f", "k"}:
        if src == "c":
            kelvin = v + 273.15
        elif src == "f":
            kelvin = (v - 32) * 5 / 9 + 273.15
        else:
            kelvin = v
        if dst == "k":
            return fmt_number(kelvin)
        if dst == "c":
            return fmt_number(kelvin - 273.15)
        return fmt_number((kelvin - 273.15) * 9 / 5 + 32)
    raise ValueError(f"不支持的换算: {src} -> {dst}")


class ToolError(Exception):
    """工具执行失败的统一包装，带可被 agent 读回的错误信息。"""


def dispatch_via(table: dict, name: str, arguments: dict) -> str:
    """按给定注册表执行工具；任何异常都转成给模型看的结构化错误文本。"""
    fn = table.get(name)
    if fn is None:
        raise ToolError(f"未知工具 {name!r}；可用工具: {', '.join(sorted(table))}")
    try:
        return fn(**arguments)
    # SyntaxError 也要接：模型会发来括号不平衡的表达式，ast.parse 直接抛
    # SyntaxError，不接住就会逃出 dispatch，把整个任务打成 orchestration_error
    # （本地 Qwen3-4B-AWQ 实测踩过：adv-05 发了 "(1+2"。
    except (TypeError, ValueError, KeyError, ZeroDivisionError, OverflowError,
            SyntaxError) as exc:
        raise ToolError(f"{name} 执行失败: {exc}") from exc


def dispatch(name: str, arguments: dict) -> str:
    """默认工具集入口（calc/lookup/convert）。"""
    return dispatch_via(IMPLEMENTATIONS, name, arguments)


IMPLEMENTATIONS = {"calc": calc, "lookup": lookup, "convert": convert}

# OpenAI 兼容的 tools 数组
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "calc",
            "description": "计算算术表达式，返回数字字符串。支持 + - * / ** % // 和括号。",
            "parameters": {
                "type": "object",
                "properties": {"expr": {"type": "string", "description": "算术表达式，如 '128*37-19'"}},
                "required": ["expr"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "从冻结事实库按键查询，返回该键的值字符串。",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string", "description": "事实库中的键名，如 'gpu.vram_gb'"}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "convert",
            "description": "单位换算。长度: mm/cm/m/km；质量: mg/g/kg/lb；温度: c/f/k。返回数字字符串。",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"type": "number", "description": "要换算的数值"},
                    "src": {"type": "string", "description": "源单位"},
                    "dst": {"type": "string", "description": "目标单位"},
                },
                "required": ["value", "src", "dst"],
            },
        },
    },
]
