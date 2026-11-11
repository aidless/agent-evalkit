"""审计规则工具集：把 tmaudit 的 C1–C10 规则语义暴露成 agent 可查询的工具。

事实来源：tmaudit（PyPI 在架 0.5.1；本地开发版 0.7.0）的
``src/tmaudit/evidence.py``（CHECK_EVIDENCE / CHECK_RATIONALE，逐条复制）
与 ``src/tmaudit/templates/verify_TEMPLATE.py``（类别定义 + 默认严重级）。
RULES 里的每一条都是从源码逐字/摘抄导出，不做推断。

这里不重新实现检查器——只暴露「规则语义」供 agent 回答
「这条场景该查哪条规则 / 该报什么级 / 证据层级是什么」类问题。
"""
from __future__ import annotations

import json

from .tools import ToolError

EVIDENCE_TIERS = ("CONFIRMED", "PARTIAL", "UNSUBSTANTIATED")
VALID_SEVERITIES = ("HIGH", "MEDIUM", "LOW")

TIER_MEANING = {
    "CONFIRMED": "确定性判据：判定不经过任何代理步骤，误报需要判据本身算错（如 .bib key 解析、效应量重算）。",
    "PARTIAL": "代理判据：扫描/启发式只覆盖部分语义，可能双向出错（如摘要符号扫描、句式启发式）。",
    "UNSUBSTANTIATED": "未分类检测器默认层级——未归类的检查不得显得可信（tier_of 的兜底值）。",
}

RULES: dict[str, dict] = {
    "C1": {
        "name": "Abstract symbol definitions",
        "description": "摘要中每个声明的符号，其首次提及的 250 字符窗口内必须有内联定义短语；扫描 CHECKS_CONFIG['c1_symbols']。",
        "default_severity": "HIGH",
        "evidence_tier": "PARTIAL",
        "rationale": "扫描摘要找未定义符号；符号可能在别处定义或扫描漏掉内联定义。",
        "config_keys": ["c1_symbols"],
        "variable_severity": None,
    },
    "C2": {
        "name": "Bonferroni scheme consistency",
        "description": "摘要、正文、表注与 CHECKS_CONFIG['c2_families'] 声明的族数一致（Bonferroni 算术自洽）；可选校验 c2_section_pattern 匹配的小节存在。",
        "default_severity": "HIGH",
        "evidence_tier": "PARTIAL",
        "rationale": "检查声明的 Bonferroni 算术内部一致；无法从实验设计重新推导多重性。",
        "config_keys": ["c2_families", "c2_section_pattern"],
        "variable_severity": None,
    },
    "C3": {
        "name": "Formalization of a key concept",
        "description": "声明的核心概念（c3_concept_token）在被定性使用之前必须有匹配 c3_formal 的形式化定义。",
        "default_severity": "MEDIUM",
        "evidence_tier": "PARTIAL",
        "rationale": "检测概念在某处被形式化；形式化是否恰当超出能力范围。",
        "config_keys": ["c3_concept_token", "c3_formal"],
        "variable_severity": None,
    },
    "C4": {
        "name": "Citation hygiene",
        "description": "正文每个 \\cite{...} key 必须在 refs.bib（@...{key,...}）或编译 .bbl（\\bibitem[l]{key}）中有条目；两者都没有时 C4 报 not applicable。自引率须低于 c4_self_cite_threshold。",
        "default_severity": "MEDIUM",
        "evidence_tier": "CONFIRMED",
        "rationale": "引用 key 要么在 .bib 里解析、要么不在。确定性判定，无代理步骤。",
        "config_keys": ["c4_self_cite_threshold"],
        "variable_severity": "无 sources 时整条 not applicable",
    },
    "C5": {
        "name": "Sample size and effect-size transparency",
        "description": "摘要中每个 p 值必须配对 n 和检验名；摘要中每个 d=... 必须声明为 CHECKS_CONFIG['c5_d_type']（默认 Cohen's d）。",
        "default_severity": "MEDIUM",
        "evidence_tier": "PARTIAL",
        "rationale": "按原样读样本量与效应量；表列标错会静默传播。",
        "config_keys": ["c5_d_type"],
        "variable_severity": None,
    },
    "C6": {
        "name": "Blacklisted vocabulary",
        "description": "CHECKS_CONFIG['c6_blacklist'] 里的每个词在正文中出现即标记待替换。",
        "default_severity": "LOW",
        "evidence_tier": "PARTIAL",
        "rationale": "黑名单词是真实缺陷的词汇代理；ruflo ADR-081 实测过这种代理双向出错。",
        "config_keys": ["c6_blacklist"],
        "variable_severity": None,
    },
    "C7": {
        "name": "Citation context (ceremonial vs engaged)",
        "description": "对正文每个 \\cite 分类引用句是 engaged 还是 ceremonial：无 engage 动词（show/demonstrate/extend/build on...）、无比较词（however/while...）且句子 <30 词判 ceremonial。默认宽松：不超过 c7_max_ceremonial（默认 2）条静默，第 (N+1) 条起报 MEDIUM。",
        "default_severity": "MEDIUM",
        "evidence_tier": "PARTIAL",
        "rationale": "engaged/ceremonial 分类是句式启发式：写得反常的真引用会被读反。",
        "config_keys": ["c7_max_ceremonial"],
        "variable_severity": "超出 max_ceremonial 后报 MEDIUM",
    },
    "C8": {
        "name": "Statistical claims",
        "description": "对声明的效应量做重推导 + 事后功效（post-hoc power）；每个显著性声明必须带 p 值。",
        "default_severity": "MEDIUM",
        "evidence_tier": "CONFIRMED",
        "rationale": "从表格自身数字重算 Cohen's d 与达成功效；误报需要重算本身出错。",
        "config_keys": [],
        "variable_severity": "可变：d 不匹配 HIGH，功效问题 MEDIUM；默认 MEDIUM",
    },
    "C9": {
        "name": "Figure captions",
        "description": "每张图在图形下方必须有 caption；每个定义过的 fig: label 必须在正文某处被引用。",
        "default_severity": "MEDIUM",
        "evidence_tier": "PARTIAL",
        "rationale": "caption 与图块做文本比对；不检查渲染后的坐标轴与图元。",
        "config_keys": [],
        "variable_severity": "可变：缺 caption HIGH，位置/内容/未引用 MEDIUM；默认 MEDIUM",
    },
    "C10": {
        "name": "Reproducibility",
        "description": "可用性声明（availability statement）及其时态 + 元数据（超参数、随机种子、硬件、库版本）。",
        "default_severity": "LOW",
        "evidence_tier": "PARTIAL",
        "rationale": "可用性声明靠措辞检测；有实质但措辞不符的声明会被漏掉。",
        "config_keys": [],
        "variable_severity": "可变：缺可用性声明 HIGH，一致性/将来时 MEDIUM，缺元数据 LOW；默认 LOW",
    },
}


def audit_rule(rule_id: str) -> str:
    """查询一条审计规则：类别定义、默认严重级、证据层级、理由、配置键。"""
    rid = str(rule_id).strip().upper()
    rule = RULES.get(rid)
    if rule is None:
        raise ToolError(f"未知规则 {rule_id!r}；可用：{', '.join(RULES)}")
    return json.dumps({"id": rid, **rule}, ensure_ascii=False)


def audit_severities() -> str:
    """查询严重级词表、证据三态及其语义。"""
    return json.dumps({
        "valid_severities": list(VALID_SEVERITIES),
        "evidence_tiers": list(EVIDENCE_TIERS),
        "tier_meaning": TIER_MEANING,
    }, ensure_ascii=False)


AUDIT_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "audit_rule",
            "description": "查询 tmaudit 审计规则（C1–C10）：类别定义、默认严重级、证据层级（CONFIRMED/PARTIAL/UNSUBSTANTIATED）、理由、配置键。",
            "parameters": {
                "type": "object",
                "properties": {"rule_id": {"type": "string", "description": "规则 id，如 C1..C10"}},
                "required": ["rule_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "audit_severities",
            "description": "查询严重级词表（HIGH/MEDIUM/LOW）与证据三态（CONFIRMED/PARTIAL/UNSUBSTANTIATED）的确切语义。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

AUDIT_DISPATCH = {
    "audit_rule": audit_rule,
    "audit_severities": audit_severities,
}


def dispatch_audit(name: str, arguments: dict) -> str:
    """audit 工具集入口：与 tools.dispatch 同一异常包装语义。"""
    from .tools import dispatch_via
    return dispatch_via(AUDIT_DISPATCH, name, arguments)
