"""mouth_filter_core 纯函数单测（零 SDK 依赖，直接 `python tests/test_core.py` 跑）。"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mouth_filter_core import (
    CompiledRules,
    _normalize_replacements,
    compile_rules,
    filter_text,
    is_effectively_empty,
)

REPL = "*已过滤*"  # 单元素池 = 确定性
POOL = ["*已过滤*", "*Filtered*"]
_passed = 0
_failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
    else:
        _failed.append(f"{name}: {detail}")
        print(f"FAIL {name} {detail}")


# --- compile_rules ----------------------------------------------------------
rules = compile_rules(["傻逼", "fuck", "fuck", " 妈的 "], [r"去死吧?", "", "(未闭合"])
check("词表去重与去空白", len(rules.words) == 3, f"got {len(rules.words)}")
check("无效正则进 invalid 不炸", rules.invalid == ["(未闭合"], f"got {rules.invalid}")
check("长词优先排序", compile_rules(["ab", "abc"], []).words[0].pattern == "abc", compile_rules(["ab", "abc"], []).words[0].pattern)

# --- 占位符池规范化 ------------------------------------------------------------
check("池规范化 str 兼容", _normalize_replacements(REPL) == [REPL])
check("池规范化 None 退默认", _normalize_replacements(None) == ["*已过滤*"])
check("池规范化 空列表退默认", _normalize_replacements([]) == ["*已过滤*"])
check("池规范化 全空白项退默认", _normalize_replacements(["", "  "]) == ["*已过滤*"])
check("池规范化 去空白保留", _normalize_replacements([" a ", ""]) == [" a "])

# --- 中文精确词（单元素池 = 确定性）---------------------------------------------
r = compile_rules(["傻逼"], [])
out, hits = filter_text("你这个傻逼，真的是傻逼", r, [REPL])
check("中文词替换", out == f"你这个{REPL}，真的是{REPL}", out)
check("命中计数累计", hits and hits[0].count == 2, str(hits))

# --- 英文大小写 ---------------------------------------------------------------
r = compile_rules(["fuck"], [])
for variant in ("FUCK", "Fuck", "fUcK"):
    out, hits = filter_text(f"oh {variant} yes", r, [REPL])
    check(f"大小写 {variant}", out == f"oh {REPL} yes" and hits[0].count == 1, out)

# --- 正则 ---------------------------------------------------------------------
r = compile_rules([], [r"滚(出?去)"])
out, hits = filter_text("你给我滚出去，滚开", r, [REPL])
check("正则替换与计数", out == f"你给我{REPL}，滚开" and hits[0].kind == "regex" and hits[0].count == 1, out)
check("正则源码进 Hit", hits[0].pattern == "滚(出?去)", hits[0].pattern)

# --- 零误伤 -------------------------------------------------------------------
untouched = "今天天气不错，去公园走走吧"
r = compile_rules(["傻逼", "fuck"], [r"滚(出?去)"])
out, hits = filter_text(untouched, r, POOL)
check("无命中原样返回", out == untouched and hits == [], out)
out, _ = filter_text("", r, POOL)
check("空正文安全", out == "")

r = CompiledRules()
out, hits = filter_text("随便说点什么", r, POOL)
check("空规则集放行", out == "随便说点什么" and hits == [])

# --- 长词优先 -----------------------------------------------------------------
r = compile_rules(["傻逼", "大傻逼"], [])
out, hits = filter_text("他是个大傻逼", r, [REPL])
check("长词优先整体命中", out == f"他是个{REPL}", out)

# --- 占位符池随机（每个消音位独立掷骰）-------------------------------------------
r = compile_rules(["傻逼"], [])
outs = set()
for _ in range(60):
    out, hits = filter_text("你个傻逼", r, POOL)
    outs.add(out)
    if out not in (f"你个{POOL[0]}", f"你个{POOL[1]}"):
        check("池随机输出合法", False, out)
        break
else:
    check("池随机输出合法", True)
check("池随机两种都出现过（60 次）", len(outs) == 2, str(outs))

r = compile_rules(["傻逼", "fuck"], [])
mixed_ok = True
for _ in range(60):
    out, _h = filter_text("傻逼 fuck", r, POOL)
    if not (out in (f"{POOL[0]} {POOL[0]}", f"{POOL[0]} {POOL[1]}", f"{POOL[1]} {POOL[0]}", f"{POOL[1]} {POOL[1]}")):
        mixed_ok = False
        break
check("多命中每个位置独立随机", mixed_ok, "60 次内出现非法组合")

# --- is_effectively_empty（多占位符混排）------------------------------------------
check("纯占位符判空", is_effectively_empty(REPL * 3, POOL))
check("混合占位符判空", is_effectively_empty(f"{POOL[0]}！{POOL[1]}？  ", POOL))
check("空串判空", is_effectively_empty("", POOL))
check("占位符+正文不判空", not is_effectively_empty(f"{POOL[0]}，但是还好啦", POOL))
check("普通正文不判空", not is_effectively_empty("哈哈哈", POOL))
check("池为空串时不误判", not is_effectively_empty("哈哈", ""))
check("str 池兼容", is_effectively_empty(REPL + "。", REPL))

# --- Hook 组合行为模拟（还原 plugin 层掷骰逻辑）------------------------------------
def simulate(response: str, words: list[str], pool: list[str], fallback: str, whole_chance: float, rng: random.Random):
    rules = compile_rules(words, [])
    new_text, hits = filter_text(response, rules, pool)
    if not hits:
        return new_text, hits, "pass"
    if rng.random() < whole_chance:
        return rng.choice(pool), hits, "swallow"
    if is_effectively_empty(new_text, pool):
        return fallback, hits, "fallback"
    return new_text, hits, "mask"


rng = random.Random(42)
out, hits, mode = simulate("傻逼", ["傻逼"], POOL, "（咽回去了）", 0.0, rng)
check("全掩码+零吞噬走兜底语", out == "（咽回去了）" and mode == "fallback", f"{out}/{mode}")
out, hits, mode = simulate("你真是个傻逼啊朋友", ["傻逼"], POOL, "（咽回去了）", 0.0, rng)
check(
    "部分命中正常打码",
    out in (f"你真是个{POOL[0]}啊朋友", f"你真是个{POOL[1]}啊朋友") and mode == "mask",
    f"{out}/{mode}",
)

swallowed = 0
for _ in range(2000):
    out, hits, mode = simulate("你真是个傻逼啊", ["傻逼"], POOL, "（咽回去了）", 0.5, rng)
    if mode == "swallow":
        swallowed += 1
        if out not in POOL or hits[0].count != 1:
            check("整句吞噬输出单占位符", False, out)
            break
else:
    check("整句吞噬输出单占位符", True)
check("吞噬频率≈设定值(0.5, 2000 次)", 850 <= swallowed <= 1150, f"swallowed={swallowed}")

clean_kept = all(simulate("今天天气不错", ["傻逼"], POOL, "x", 1.0, rng)[2] == "pass" for _ in range(100))
check("干净回复永不吞噬(概率=1 也不吞)", clean_kept)

print(f"\n{_passed} passed, {len(_failed)} failed")
sys.exit(1 if _failed else 0)
