"""麦麦嘴替过滤器 —— 词表匹配核心（零 SDK 依赖的纯函数，可离线单测）。

规则两类：
    words    精确词，子串匹配，英文大小写不敏感
    regexes  正则（进阶用户用），同样忽略大小写

替换语义：命中片段替换成**占位符池**里随机一个（每个消音位置独立掷骰，
模拟 Neuro-sama 消音的不可预测感）；全部替换后正文若只剩占位符/空白/标点，
由调用方换发兜底替代语。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

DEFAULT_REPLACEMENT = "*已过滤*"


def _normalize_replacements(replacements) -> list[str]:
    """占位符池规范化：容忍 str / list / None 输入，剥空白去空项；
    全空时退回默认占位符（绝不让 Hook 拿到空池）。"""
    if replacements is None:
        return [DEFAULT_REPLACEMENT]
    if isinstance(replacements, str):
        replacements = [replacements]
    pool = [str(r) for r in replacements if str(r).strip()]
    return pool or [DEFAULT_REPLACEMENT]


@dataclass(frozen=True)
class Hit:
    """一条规则的命中记录（不含原文片段，避免日志变成脏话库）。"""

    kind: str      # "word" | "regex"
    pattern: str   # 规则原文（词本身 / 正则源码），进日志用
    count: int     # 本次正文里的替换次数


@dataclass
class CompiledRules:
    """预编译后的规则集。"""

    words: list[re.Pattern[str]] = field(default_factory=list)   # 长词优先
    regexes: list[re.Pattern[str]] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)             # 编译失败被跳过的正则


def compile_rules(words: list[str], regexes: list[str]) -> CompiledRules:
    """把原始词表/正则编译成可执行规则。

    词表去重、按长度降序（先替换长词，短词计数不被长词命中瓜分）、
    ``re.escape`` + IGNORECASE 做大小写不敏感的子串替换；
    无效正则跳过并记入 ``invalid``，不抛异常（用户手滑不该炸掉插件）。
    """
    seen: set[str] = set()
    unique: list[str] = []
    for w in words or []:
        item = str(w).strip()
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    unique.sort(key=len, reverse=True)

    word_patterns = [re.compile(re.escape(w), re.IGNORECASE) for w in unique]

    compiled: list[re.Pattern[str]] = []
    invalid: list[str] = []
    for r in regexes or []:
        src = str(r).strip()
        if not src:
            continue
        try:
            compiled.append(re.compile(src, re.IGNORECASE))
        except re.error:
            invalid.append(src)
    return CompiledRules(words=word_patterns, regexes=compiled, invalid=invalid)


def is_effectively_empty(text: str, replacements) -> bool:
    """正文去掉（池内所有）占位符、空白与标点后是否已无内容（=> 触发兜底替代语）。

    Unicode 模式下中文属于 ``\\w``，``[\\W_]+`` 恰好剥掉空白和各类中英文标点。
    """
    rest = text or ""
    for r in _normalize_replacements(replacements):
        rest = rest.replace(r, "")
    return re.sub(r"[\W_]+", "", rest) == ""


def filter_text(text: str, rules: CompiledRules, replacements) -> tuple[str, list[Hit]]:
    """对正文执行打码，返回 ``(新正文, 命中列表)``。

    每个命中片段独立从占位符池随机选一个替换（repl 传函数时 subn 每次调用
    一次，天然实现"每个消音位独立掷骰"）。无命中时原样返回，命中列表为空。
    """
    hits: list[Hit] = []
    if not text or not (rules.words or rules.regexes):
        return text, hits

    pool = _normalize_replacements(replacements)

    result = text
    for pattern in rules.words:
        result, count = pattern.subn(lambda _m: random.choice(pool), result)
        if count:
            hits.append(Hit(kind="word", pattern=pattern.pattern, count=count))

    for pattern in rules.regexes:
        result, count = pattern.subn(lambda _m: random.choice(pool), result)
        if count:
            hits.append(Hit(kind="regex", pattern=pattern.pattern, count=count))

    return result, hits
