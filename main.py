from __future__ import annotations

import atexit
import re
from collections import OrderedDict, defaultdict, deque
from pathlib import Path

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import StarTools

from .lark_enhance.handlers import (
    handle_lark_emoji_reply,
    handle_lark_forget_memory,
    handle_lark_list_memory,
    handle_lark_save_memory,
    handle_on_decorating_result,
    handle_on_llm_request,
    handle_on_message,
    handle_on_message_sent,
)
from .lark_enhance.mixins import (
    HistoryMixin,
    LarkContextMixin,
    StreamingMixin,
    TextMixin,
    configure_streaming_runtime,
)
from .lark_enhance.stores import UserMemoryStore


class Main(HistoryMixin, LarkContextMixin, TextMixin, StreamingMixin, star.Star):
    _VERSION = "0.3.1"

    _CACHE_TTL = 300
    _SAVE_DEBOUNCE = 5
    _USER_CACHE_MAX_SIZE = 5000
    _CLEAN_CONTENT_MAX_LEN = 10000
    _MEME_CAPTURE_PATTERNS = [
        re.compile(r"^\s*记住这个梗[:：]?\s*(.+)$"),
        re.compile(r"^\s*(?:这|这个)?(?:就是)?(?:我们)?群梗[:：]?\s*(.+)$"),
    ]

    def __init__(self, context: star.Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

        self.user_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self.group_members_cache: dict[str, dict[str, str]] = {}
        self._group_members_cache_time: dict[str, float] = {}
        self._mention_pattern_cache: dict[str, tuple[re.Pattern, float]] = {}
        self.group_info_cache: dict[str, dict] = {}
        self._group_info_cache_time: dict[str, float] = {}

        self._reacted_messages: OrderedDict[str, bool] = OrderedDict()

        self._history_maxlen = self.config.get("history_inject_count", 20) or 20
        self.group_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self._history_maxlen)
        )

        self._data_dir: Path = StarTools.get_data_dir("astrbot_plugin_lark_enhance")
        self._history_file: Path = self._data_dir / "group_history.json"

        self._last_save_time: float = 0
        self._pending_save: bool = False

        self._load_history()
        self._memory_store = UserMemoryStore(self._data_dir)

        atexit.register(self._atexit_save)

        configure_streaming_runtime(self.config, self._clean_content)

        if self.config.get("enable_streaming_card", False):
            self._setup_streaming_patch()
        else:
            logger.info("[lark_enhance] Streaming card is disabled by config")

        logger.info(
            f"[lark_enhance] ====== Plugin loaded successfully ====== "
            f"Version: {self._VERSION}, "
            f"UserMemory: {self.config.get('enable_user_memory', True)}, "
            f"VibeSense: {self.config.get('enable_vibe_sense', True)}, "
            f"MemeMemory: {self.config.get('enable_meme_memory', True)}"
        )

    @filter.platform_adapter_type(filter.PlatformAdapterType.LARK)
    async def on_message(self, event: AstrMessageEvent):
        await handle_on_message(self, event)

    @filter.after_message_sent()
    async def on_message_sent(self, event: AstrMessageEvent):
        await handle_on_message_sent(self, event)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        await handle_on_llm_request(self, event, req)

    @filter.llm_tool(name="lark_emoji_reply")
    async def lark_emoji_reply(self, event: AstrMessageEvent, emoji: str):
        """飞书表情回复工具。
        当需要强烈表达情绪（点赞、祝贺、惊讶、安慰、无语、收到）且不需要长文本时，优先调用本工具。
        该工具用于“情绪表达增强”，不要机械地每条都用。

        官方 emoji_type 枚举（飞书文档，117 个）：
            OK, THUMBSUP, THANKS, MUSCLE, FINGERHEART, APPLAUSE, FISTBUMP, JIAYI, DONE, SMILE
            BLUSH, LAUGH, SMIRK, LOL, FACEPALM, LOVE, WINK, PROUD, WITTY, SMART
            SCOWL, THINKING, SOB, CRY, ERROR, NOSEPICK, HAUGHTY, SLAP, SPITBLOOD, TOASTED
            GLANCE, DULL, INNOCENTSMILE, JOYFUL, WOW, TRICK, YEAH, ENOUGH, TEARS, EMBARRASSED
            KISS, SMOOCH, DROOL, OBSESSED, MONEY, TEASE, SHOWOFF, COMFORT, CLAP, PRAISE
            STRIVE, XBLUSH, SILENT, WAVE, WHAT, FROWN, SHY, DIZZY, LOOKDOWN, CHUCKLE
            WAIL, CRAZY, WHIMPER, HUG, BLUBBER, WRONGED, HUSKY, SHHH, SMUG, ANGRY
            HAMMER, SHOCKED, TERROR, PETRIFIED, SKULL, SWEAT, SPEECHLESS, SLEEP, DROWSY, YAWN
            SICK, PUKE, BETRAYED, HEADSET, LGTM, SALUTE, SHAKE, HIGHFIVE, UPPERLEFT, SLIGHT
            TONGUE, EYESCLOSED, CALF, BEAR, BULL, RAINBOWPUKE, ROSE, HEART, PARTY, LIPS
            BEER, CAKE, GIFT, CUCUMBER, CANDIEDHAWS, OKR, AWESOMEN, BOMB, FIREWORKS, REDPACKET
            FORTUNE, LUCK, FIRECRACKER, HEARTBROKEN, POOP, CLEAVER, TV
        文档：https://open.feishu.cn/document/server-docs/im-v1/message-reaction/emojis-introduce

        Args:
            emoji(string): 表情代码。请尽量使用上述枚举值（全大写）。插件会自动兼容部分别名，如 THUMBS_UP -> THUMBSUP。
        """
        return await handle_lark_emoji_reply(self, event, emoji)

    @filter.llm_tool(name="lark_save_memory")
    async def lark_save_memory(
        self,
        event: AstrMessageEvent,
        memory_type: str,
        content: str,
        scope: str = "user",
    ):
        """保存记忆。

        Args:
            memory_type(string): 记忆类型，支持 preference/fact/instruction/meme。
            content(string): 要记住的内容。
            scope(string): 记忆范围，支持 user/group。
        """
        return await handle_lark_save_memory(self, event, memory_type, content, scope)

    @filter.llm_tool(name="lark_list_memory")
    async def lark_list_memory(
        self,
        event: AstrMessageEvent,
        scope: str = "user",
        memory_type: str = "all",
    ):
        """查询记忆。

        Args:
            scope(string): 查询范围，支持 user/group/all。
            memory_type(string): 类型筛选，支持 all/preference/fact/instruction/meme。
        """
        return await handle_lark_list_memory(self, event, scope, memory_type)

    @filter.llm_tool(name="lark_forget_memory")
    async def lark_forget_memory(
        self,
        event: AstrMessageEvent,
        target: str = "all",
        scope: str = "user",
        memory_type: str = "all",
    ):
        """删除记忆。

        Args:
            target(string): 删除目标，all 或关键词。
            scope(string): 删除范围，支持 user/group。
            memory_type(string): 类型筛选，支持 all/preference/fact/instruction/meme。
        """
        return await handle_lark_forget_memory(self, event, target, scope, memory_type)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        await handle_on_decorating_result(self, event)
