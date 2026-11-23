"""麦麦不要说辣个啦（Neuro-sama 式嘴替打码插件）。

挂 ``maisaka.reply.before_post_process``：麦麦回复正文在拆分/错别字等
后处理**之前**经过本插件，命中的脏话/过分词句被替换为占位符
（默认 ``*已过滤*``），该说的照说、脏的地方自动消音。

v1.1.0 起采用单一词表配置，全部规则均在 WebUI 直接可见与增删，热重载即时生效：
    [filter].filter_words  过滤词表（精确匹配，忽略英文大小写）
    [filter].whitelist     上下文豁免词（白名单，保护区间内不被过滤）
    [filter].regexes       正则规则（进阶用法，忽略大小写）

模块划分：plugin.py 装配（Hook/配置/旧数据兼容）；mouth_filter_core.py 匹配纯函数
（零 SDK 依赖，可离线单测）。
"""

from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path
from typing import Any, ClassVar

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import CONFIG_RELOAD_SCOPE_SELF, ErrorPolicy, HookMode, HookOrder

_PLUGIN_DIR = Path(__file__).resolve().parent


def _load_sibling_module(stem: str) -> Any:
    """以受控方式加载同目录核心模块：不动 Runner 的全局 sys.path，
    以"maibot_mouth_filter_"前缀注册进 sys.modules，避免与其他插件撞名。"""
    module_name = f"maibot_mouth_filter_{stem}"
    spec = importlib.util.spec_from_file_location(module_name, _PLUGIN_DIR / f"{stem}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"无法定位核心模块: {stem}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_core = _load_sibling_module("mouth_filter_core")
DEFAULT_WHITELIST = _core.DEFAULT_WHITELIST
DEFAULT_WORDS = _core.DEFAULT_WORDS
LEGACY_BASE_WORDS = _core.LEGACY_BASE_WORDS
compile_rules = _core.compile_rules
filter_text = _core.filter_text
is_effectively_empty = _core.is_effectively_empty

SUPPORTED_CONFIG_VERSION = "1.1.0"  # 与 manifest version 保持同步


# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------

def _ui_field(label: str, hint: str = "", **kwargs: Any) -> Any:
    """构造带 WebUI 中文元数据的 `Field`（惯例见 fun-pack：配置页只读
    `json_schema_extra` 的 label/hint，`description` 仅给 API）。"""
    extra = dict(kwargs.pop("json_schema_extra", None) or {})
    extra.setdefault("label", label)
    if hint:
        extra.setdefault("hint", hint)
    return Field(description=hint or label, json_schema_extra=extra, **kwargs)


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "filter_alt"
    __ui_order__ = 0

    enabled: bool = _ui_field("启用插件", default=True)
    config_version: str = _ui_field(
        "配置版本",
        "配置版本（与插件版本同步）",
        default=SUPPORTED_CONFIG_VERSION,
        json_schema_extra={"hidden": True, "disabled": True},
    )


class FilterSection(PluginConfigBase):
    """过滤规则与行为。"""

    __ui_label__ = "过滤设置"
    __ui_icon__ = "voice_over_off"
    __ui_order__ = 1

    enabled: bool = _ui_field("启用过滤", "关闭后 Hook 直接放行，不改动任何正文", default=True)
    replacements: list[str] = _ui_field(
        "替换占位符池",
        "每个被消音的位置随机选一个，如 *已过滤* / *Filtered* / ▇▇▇",
        default_factory=lambda: ["*已过滤*", "*Filtered*"],
    )
    whole_reply_chance: float = _ui_field(
        "整句吞噬概率",
        "命中脏话时以此概率把整条回复直接换成一个单独的占位符（模拟整句消音）；0 = 关闭",
        default=0.2,
        ge=0.0,
        le=1.0,
    )
    filter_words: list[str] = _ui_field(
        "过滤词表（精确匹配）",
        "子串匹配，英文不分大小写；可直接增删或清空默认词表，修改保存后即时生效",
        default_factory=lambda: list(DEFAULT_WORDS),
    )
    whitelist: list[str] = _ui_field(
        "上下文豁免词（白名单）",
        "包含这些词的片段不会被过滤（如「你妈妈」保护其中的「你妈」），忽略大小写",
        default_factory=lambda: list(DEFAULT_WHITELIST),
    )
    regexes: list[str] = _ui_field(
        "正则规则",
        "进阶用法，忽略大小写；写错的正则会被跳过并在日志提示",
        default_factory=list,
    )
    fallback_text: str = _ui_field(
        "兜底替代语",
        "逐词打码后正文全空时改发这句话（整句吞噬时不走这条）；整句替换路径已跳过宿主后处理，带引号/括号都安全",
        default="“这段话被麦麦自己咽回去了”",
    )
    log_hits: bool = _ui_field("记录命中日志", "每次打码记一条（会话、规则、次数），便于回看误杀", default=True)
    log_triggers: bool = _ui_field(
        "记录每次触发（调试用）", "每次回复经过 Hook 都记一条长度对比，验证部署/排查用，平时建议关闭", default=False
    )


class MouthFilterConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    filter: FilterSection = Field(default_factory=FilterSection)


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------

class MouthFilterPlugin(MaiBotPlugin):
    """麦麦嘴替过滤器。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = MouthFilterConfig

    def __init__(self) -> None:
        super().__init__()
        self._legacy_extra_words: list[str] = []
        self._legacy_extra_regexes: list[str] = []

    def _handle_legacy_base_words(self) -> None:
        """兼容处理 1.0.x 遗留的 data/base_words.json：
        - 若内容与 1.0.x 默认词表一致，直接归档为 base_words.json.migrated；
        - 若用户曾手工在文件中添加过自定义词/正则，运行期继续兜底生效并提示迁入 WebUI。
        """
        legacy_path = self.ctx.paths.data_dir / "base_words.json"
        if not legacy_path.exists():
            return

        try:
            data = json.loads(legacy_path.read_text(encoding="utf-8"))
            legacy_words = [str(w).strip() for w in (data.get("words") or []) if str(w).strip()]
            legacy_regexes = [str(r).strip() for r in (data.get("regexes") or []) if str(r).strip()]
        except (OSError, ValueError) as e:
            self.ctx.logger.warning("[嘴替过滤] 读取旧版 %s 失败，已跳过: %s", legacy_path.name, e)
            return

        extra_words = [w for w in legacy_words if w not in LEGACY_BASE_WORDS]
        if not extra_words and not legacy_regexes:
            migrated_path = legacy_path.with_name("base_words.json.migrated")
            try:
                if migrated_path.exists():
                    migrated_path.unlink()
                legacy_path.rename(migrated_path)
                self.ctx.logger.info("[嘴替过滤] 旧版默认词表已归档为 %s，现统一使用 WebUI 单一词表", migrated_path.name)
            except OSError as e:
                self.ctx.logger.warning("[嘴替过滤] 归档旧词表 %s 失败: %s", legacy_path.name, e)
        else:
            self._legacy_extra_words = extra_words
            self._legacy_extra_regexes = legacy_regexes
            self.ctx.logger.warning(
                "[嘴替过滤] 检测到旧版 base_words.json 含自定义项（%d 词 / %d 正则），当前已兼容叠加生效；"
                "建议将其移入 WebUI 配置并删除 data/base_words.json",
                len(extra_words),
                len(legacy_regexes),
            )

    async def on_load(self) -> None:
        self._handle_legacy_base_words()
        cfg = self.config.filter
        self.ctx.logger.info(
            "麦麦不要说辣个啦（嘴替过滤器）已加载：过滤词表 %d 词 / %d 豁免词 / %d 正则，占位符池 %r，整句吞噬概率 %.0f%%",
            len(cfg.filter_words or []) + len(self._legacy_extra_words),
            len(cfg.whitelist or []),
            len(cfg.regexes or []) + len(self._legacy_extra_regexes),
            list(cfg.replacements or []),
            float(cfg.whole_reply_chance or 0) * 100,
        )

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="mouth_filter",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def hook_before_post_process(self, **kwargs: Any):
        """正文后处理（拆分/错别字/长度限制）之前的打码点。"""

        if not (self.config.plugin.enabled and self.config.filter.enabled):
            return None
        response = kwargs.get("response")
        if not isinstance(response, str) or not response:
            return None

        cfg = self.config.filter
        pool = [str(r) for r in (cfg.replacements or []) if str(r).strip()] or ["*已过滤*"]
        words = [str(w) for w in (cfg.filter_words or [])] + self._legacy_extra_words
        regexes = [str(r) for r in (cfg.regexes or [])] + self._legacy_extra_regexes
        whitelist = [str(w) for w in (cfg.whitelist or [])]

        rules = compile_rules(words, regexes, whitelist)
        if rules.invalid and cfg.log_hits:
            self.ctx.logger.warning("[嘴替过滤] %d 条正则编译失败已跳过: %s", len(rules.invalid), rules.invalid)

        new_text, hits = filter_text(response, rules, pool)
        session = str(kwargs.get("session_id") or "")
        if cfg.log_triggers:
            self.ctx.logger.info(
                "[嘴替过滤] Hook 触发: session=%s 长度 %d→%d 命中 %d 条规则", session, len(response), len(new_text), len(hits)
            )
        if not hits:
            return None

        chance = float(cfg.whole_reply_chance or 0)
        skip_post_process = bool(kwargs.get("skip_post_process", False))
        if chance > 0 and random.random() < chance:
            new_text = random.choice(pool)
            skip_post_process = True  # 单占位符是成品，跳过宿主括号清理/拆分等后处理
            if cfg.log_hits:
                self.ctx.logger.info(
                    "[嘴替过滤] 整句吞噬（%.0f%% 掷中）: session=%s 原 %d 字直接换成单占位符", chance * 100, session, len(response)
                )
        elif is_effectively_empty(new_text, pool):
            new_text = str(cfg.fallback_text or "")
            skip_post_process = True  # 兜底语同理；且防用户文案带括号被宿主剥成空回复
            self.ctx.logger.info("[嘴替过滤] 正文全被过滤，改发兜底语: session=%s", session)
        if cfg.log_hits:
            rule_summary = ", ".join(f"{h.kind}:{h.pattern}×{h.count}" for h in hits)
            self.ctx.logger.info("[嘴替过滤] 已打码: session=%s 规则[%s]", session, rule_summary)

        # modified_kwargs 必须完整替换；三个策略字段按契约返回布尔值（原值透传，整句替换路径除外）
        kwargs = dict(kwargs)
        kwargs["response"] = new_text
        kwargs["skip_post_process"] = skip_post_process
        kwargs["enable_splitter"] = bool(kwargs.get("enable_splitter", True))
        kwargs["enable_chinese_typo"] = bool(kwargs.get("enable_chinese_typo", True))
        return {"action": "continue", "modified_kwargs": kwargs}

    async def on_unload(self) -> None:
        self.ctx.logger.info("麦麦不要说辣个啦（嘴替过滤器）已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        # 词表每次 Hook 现读 config，无需重建组件
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self.ctx.logger.info("嘴替过滤器配置已热更新 version=%s", version)


def create_plugin() -> MouthFilterPlugin:
    """Runner 加载入口。"""
    return MouthFilterPlugin()
