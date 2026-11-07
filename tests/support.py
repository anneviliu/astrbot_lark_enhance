"""Minimal AstrBot API doubles; Lark requests use the real lark-oapi SDK."""

import logging
import sys
from collections import OrderedDict, defaultdict, deque
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch


class Plain:
    def __init__(self, text):
        self.text = text


class At:
    def __init__(self, qq, name=None):
        self.qq = qq
        self.name = name


class BaseEvent:
    async def send_streaming(self, generator, use_fallback=False):
        async for _ in generator:
            pass
        self._has_send_oper = True


class ResultContentType:
    STREAMING_FINISH = "streaming_finish"


def astrbot_stubs(lark_event_class=None):
    """Patch only the framework boundary, without installing/running AstrBot."""
    names = (
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.message_components",
        "astrbot.api.provider",
        "astrbot.core",
        "astrbot.core.message",
        "astrbot.core.message.message_event_result",
        "astrbot.core.platform",
        "astrbot.core.platform.astr_message_event",
        "astrbot.core.platform.sources",
        "astrbot.core.platform.sources.lark",
        "astrbot.core.platform.sources.lark.lark_event",
    )
    modules = {name: ModuleType(name) for name in names}
    modules["astrbot.api"].logger = logging.getLogger("lark_enhance.tests")
    modules["astrbot.api.event"].AstrMessageEvent = BaseEvent
    modules["astrbot.api.message_components"].Plain = Plain
    modules["astrbot.api.message_components"].At = At
    modules["astrbot.api.provider"].ProviderRequest = SimpleNamespace
    modules[
        "astrbot.core.message.message_event_result"
    ].ResultContentType = ResultContentType
    modules["astrbot.core.platform.astr_message_event"].AstrMessageEvent = BaseEvent
    if lark_event_class is not None:
        modules[
            "astrbot.core.platform.sources.lark.lark_event"
        ].LarkMessageEvent = lark_event_class
    return patch.dict(sys.modules, modules)


with astrbot_stubs():
    from lark_enhance.mixins import HistoryMixin, LarkContextMixin, TextMixin


class Plugin(HistoryMixin, LarkContextMixin, TextMixin):
    _CACHE_TTL = 300
    _USER_CACHE_MAX_SIZE = 5000
    _CLEAN_CONTENT_MAX_LEN = 10000
    _SAVE_DEBOUNCE = 5

    def __init__(self, data_dir):
        self.config = {
            "enable_real_name": True,
            "enable_group_info": False,
            "enable_quoted_content": False,
            "enable_meme_memory": False,
            "enable_user_memory": False,
            "enable_vibe_sense": False,
            "enable_human_rhythm": False,
            "history_inject_count": 20,
            "bot_name": "测试助手",
        }
        self.user_cache = OrderedDict()
        self.group_members_cache = {}
        self._group_members_cache_time = {}
        self._mention_pattern_cache = {}
        self._history_maxlen = 20
        self.group_history = defaultdict(lambda: deque(maxlen=20))
        self._data_dir = data_dir
        self._history_file = data_dir / "group_history.json"
        self._last_save_time = 0
        self._pending_save = False


class Event:
    def __init__(self, timestamp=1788536384, sender_id="ou_test4ada", nickname=None):
        self.message_str = "今天交付了第一版"
        self.message_obj = SimpleNamespace(
            timestamp=timestamp,
            group_id="oc_test",
            message_id="om_current",
            message_str=self.message_str,
            message=[Plain(self.message_str)],
            sender=SimpleNamespace(nickname=nickname or sender_id[:8]),
            raw_message=SimpleNamespace(parent_id=None),
        )
        self.sender_id = sender_id
        self.extras = {}
        self.result = None
        self.bot = None

    def get_sender_id(self):
        return self.sender_id

    def get_self_id(self):
        return "ou_bot"

    def get_platform_name(self):
        return "lark"

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_result(self):
        return self.result


def response(data=None, code=0):
    return SimpleNamespace(
        success=lambda: code == 0, data=data, code=code, msg="test response"
    )


def client(contact_response=None, members_responses=None):
    contact_get = AsyncMock(return_value=contact_response or response(code=41050))
    members_get = AsyncMock(
        side_effect=members_responses
        or [
            response(
                SimpleNamespace(
                    items=[SimpleNamespace(member_id="ou_test4ada", name="张三")],
                    has_more=False,
                    page_token=None,
                )
            )
        ]
    )
    return SimpleNamespace(
        contact=SimpleNamespace(
            v3=SimpleNamespace(user=SimpleNamespace(aget=contact_get))
        ),
        im=SimpleNamespace(
            v1=SimpleNamespace(chat_members=SimpleNamespace(aget=members_get))
        ),
    )
