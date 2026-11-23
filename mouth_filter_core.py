"""麦麦嘴替过滤器 —— 词表匹配核心（零 SDK 依赖的纯函数，可离线单测）。

规则三类：
    words      精确词，子串匹配，英文大小写不敏感（长词优先）
    regexes    正则（进阶用户用），同样忽略大小写
    whitelist  上下文豁免词（白名单），子串忽略大小写；凡是与豁免词区间
               重叠的脏话命中一律跳过（如「你妈妈」保护其中的「你妈」）

替换语义：在原始正文坐标上定位所有非豁免、互不重叠的命中区间（Span），
再一次性拼接替换为**占位符池**里随机一个（每个消音位置独立掷骰，
模拟 Neuro-sama 消音的不可预测感），杜绝链式二次命中；
全部替换后正文若只剩占位符/空白/标点，由调用方换发兜底替代语。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

DEFAULT_REPLACEMENT = "*已过滤*"

# 精修后的默认过滤词表（单一词表默认初始值，可在 WebUI 直接增删）
# 注：相比 1.0.x 内置词表，移除了高误杀裸词「你妈」「日你」「干你」，
# 并去除了因 IGNORECASE 而冗余的小写重复项（傻b/sb/tm的），补充「日你妈」。
DEFAULT_WORDS: tuple[str, ...] = (
    "傻逼",
    "煞笔",
    "傻B",
    "SB",
    "妈的",
    "他妈的",
    "TM的",
    "操你",
    "草你",
    "日你妈",
    "尼玛",
    "去死",
    "废物",
    "蠢货",
    "白痴",
    "智障",
    "脑残",
    "滚蛋",
    "混蛋",
    "王八蛋",
    "狗东西",
    "贱人",
    "婊子",
    "fuck",
    "shit",
    "bitch",
    "damn you",
    "motherfucker",
    "asshole",
    "bastard",
    "cunt",
    "dickhead",
)

# 1.0.x 旧版内置词表（仅用于老版本升级迁移时识别“用户是否未修改过旧默认词表”）
LEGACY_BASE_WORDS: frozenset[str] = frozenset(
    {
        "傻逼", "煞笔", "傻B", "傻b", "SB", "sb",
        "妈的", "他妈的", "TM的", "tm的",
        "操你", "草你", "日你", "干你", "尼玛", "你妈",
        "去死", "废物", "蠢货", "白痴", "智障", "脑残",
        "滚蛋", "混蛋", "王八蛋", "狗东西", "贱人", "婊子",
        "fuck", "shit", "bitch", "damn you", "motherfucker",
        "asshole", "bastard", "cunt", "dickhead",
    }
)

# 默认上下文豁免词（白名单）
DEFAULT_WHITELIST: tuple[str, ...] = (
    "你妈妈",
    "你妈咪",
    "种草你",
    "去死磕",
    "USB",
)


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

    words: list[re.Pattern[str]] = field(default_factory=list)      # 长词优先
    regexes: list[re.Pattern[str]] = field(default_factory=list)
    whitelist: list[re.Pattern[str]] = field(default_factory=list)  # 上下文豁免词
    invalid: list[str] = field(default_factory=list)                # 编译失败被跳过的正则


def _compile_literal_list(items: list[str] | None) -> list[re.Pattern[str]]:
    """对字面量词表去重、去空白、按长度降序，编译为忽略大小写正则。"""
    seen: set[str] = set()
    unique: list[str] = []
    for w in items or []:
        item = str(w).strip()
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    unique.sort(key=len, reverse=True)
    return [re.compile(re.escape(w), re.IGNORECASE) for w in unique]


def compile_rules(
    words: list[str],
    regexes: list[str],
    whitelist: list[str] | None = None,
) -> CompiledRules:
    """把原始词表/正则/豁免词编译成可执行规则。

    词表去重、按长度降序（先匹配长词，短词计数不被长词命中瓜分）、
    ``re.escape`` + IGNORECASE 做大小写不敏感的子串匹配；
    无效正则跳过并记入 ``invalid``，不抛异常（用户手滑不该炸掉插件）。
    """
    word_patterns = _compile_literal_list(words)
    whitelist_patterns = _compile_literal_list(whitelist)

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
    return CompiledRules(
        words=word_patterns,
        regexes=compiled,
        whitelist=whitelist_patterns,
        invalid=invalid,
    )


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    """判断区间 [start, end) 是否与 spans 中任意区间存在重叠。"""
    return any(start < s_end and end > s_start for s_start, s_end in spans)


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

    在原始正文坐标上先标出豁免词保护区间，再依次收集精确词（长词优先）
    与正则的非重叠命中区间，最后一次性拼接替换（每个命中片段独立从占位符池
    随机选一个）。无命中时原样返回，命中列表为空。
    """
    hits: list[Hit] = []
    if not text or not (rules.words or rules.regexes):
        return text, hits

    protected_spans: list[tuple[int, int]] = []
    for wp in rules.whitelist:
        for m in wp.finditer(text):
            if m.start() < m.end():
                protected_spans.append((m.start(), m.end()))

    occupied: list[tuple[int, int]] = []
    replacements_to_apply: list[tuple[int, int]] = []

    def _collect(patterns: list[re.Pattern[str]], kind: str) -> None:
        for pattern in patterns:
            count = 0
            for m in pattern.finditer(text):
                s, e = m.start(), m.end()
                if s >= e:
                    continue
                if _overlaps(s, e, protected_spans) or _overlaps(s, e, occupied):
                    continue
                occupied.append((s, e))
                replacements_to_apply.append((s, e))
                count += 1
            if count:
                hits.append(Hit(kind=kind, pattern=pattern.pattern, count=count))

    _collect(rules.words, "word")
    _collect(rules.regexes, "regex")

    if not replacements_to_apply:
        return text, hits

    pool = _normalize_replacements(replacements)
    replacements_to_apply.sort(key=lambda span: span[0])

    parts: list[str] = []
    cursor = 0
    for s, e in replacements_to_apply:
        parts.append(text[cursor:s])
        parts.append(random.choice(pool))
        cursor = e
    parts.append(text[cursor:])

    return "".join(parts), hits
