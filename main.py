from __future__ import annotations

import asyncio
import json
import ast
import copy
import datetime
import os
import re
import time
import uuid
from collections import OrderedDict, deque, defaultdict
from typing import Any, AsyncGenerator

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.core import logger
from astrbot.core.message.message_event_result import ResultContentType

# 保存原始的 send_streaming 方法
_original_lark_send_streaming = None


async def _empty_generator():
    """空的异步生成器，用于调用父类方法"""
    return
    yield  # 让它成为异步生成器


class LarkStreamingCard:
    """飞书流式卡片处理器，用于实现打字机效果"""

    # 卡片更新间隔 (秒)
    UPDATE_INTERVAL = 0.3
    # 最小更新字符数
    MIN_UPDATE_CHARS = 5

    def __init__(self, lark_client: Any, chat_id: str, reply_to_message_id: str):
        self.lark_client = lark_client
        self.chat_id = chat_id
        self.reply_to_message_id = reply_to_message_id
        self.card_message_id: str | None = None
        self._content_buffer: str = ""
        self._last_update_time: float = 0
        self._last_update_length: int = 0

    def _build_card_content(self, text: str, is_finished: bool = False) -> str:
        """构建卡片 JSON 内容"""
        # 使用 Markdown 元素显示内容
        elements = []

        if text:
            elements.append({
                "tag": "markdown",
                "content": text,
            })

        if not is_finished:
            # 添加加载指示器
            elements.append({
                "tag": "markdown",
                "content": "◉ *正在输入...*",
            })

        card = {
            "schema": "2.0",
            "config": {
                "wide_screen_mode": True,
            },
            "body": {
                "elements": elements if elements else [
                    {"tag": "markdown", "content": "◉ *思考中...*"}
                ],
            },
        }
        return json.dumps(card, ensure_ascii=False)

    async def create_initial_card(self) -> bool:
        """创建初始卡片消息"""
        try:
            from lark_oapi.api.im.v1 import (
                ReplyMessageRequest,
                ReplyMessageRequestBody,
            )

            content = self._build_card_content("", is_finished=False)

            request = (
                ReplyMessageRequest.builder()
                .message_id(self.reply_to_message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .content(content)
                    .msg_type("interactive")
                    .uuid(str(uuid.uuid4()))
                    .reply_in_thread(False)
                    .build()
                )
                .build()
            )

            if self.lark_client.im is None:
                logger.error("[lark_enhance] lark_client.im 未初始化")
                return False

            response = await self.lark_client.im.v1.message.areply(request)

            if response.success() and response.data:
                self.card_message_id = response.data.message_id
                logger.debug(
                    f"[lark_enhance] Created streaming card: {self.card_message_id}"
                )
                return True
            else:
                logger.error(
                    f"[lark_enhance] Failed to create card: {response.code} - {response.msg}"
                )
                return False
        except Exception as e:
            logger.error(f"[lark_enhance] Create card exception: {e}")
            return False

    async def update_card(self, text: str, force: bool = False) -> bool:
        """更新卡片内容"""
        if not self.card_message_id:
            return False

        self._content_buffer = text
        now = time.time()

        # 防抖：检查是否需要更新
        if not force:
            time_elapsed = now - self._last_update_time
            chars_added = len(text) - self._last_update_length

            if time_elapsed < self.UPDATE_INTERVAL and chars_added < self.MIN_UPDATE_CHARS:
                return True  # 跳过本次更新

        try:
            from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

            content = self._build_card_content(text, is_finished=False)

            request = (
                PatchMessageRequest.builder()
                .message_id(self.card_message_id)
                .request_body(
                    PatchMessageRequestBody.builder().content(content).build()
                )
                .build()
            )

            response = await self.lark_client.im.v1.message.apatch(request)

            if response.success():
                self._last_update_time = now
                self._last_update_length = len(text)
                return True
            else:
                logger.warning(
                    f"[lark_enhance] Failed to update card: {response.code} - {response.msg}"
                )
                return False
        except Exception as e:
            logger.error(f"[lark_enhance] Update card exception: {e}")
            return False

    async def finalize_card(self, text: str) -> bool:
        """完成卡片，移除加载指示器"""
        if not self.card_message_id:
            return False

        try:
            from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

            content = self._build_card_content(text, is_finished=True)

            request = (
                PatchMessageRequest.builder()
                .message_id(self.card_message_id)
                .request_body(
                    PatchMessageRequestBody.builder().content(content).build()
                )
                .build()
            )

            response = await self.lark_client.im.v1.message.apatch(request)

            if response.success():
                logger.debug(f"[lark_enhance] Finalized streaming card")
                return True
            else:
                logger.warning(
                    f"[lark_enhance] Failed to finalize card: {response.code} - {response.msg}"
                )
                return False
        except Exception as e:
            logger.error(f"[lark_enhance] Finalize card exception: {e}")
            return False


class Main(star.Star):
    # 缓存 TTL (秒)
    _CACHE_TTL = 300  # 5 分钟
    # 历史保存防抖间隔 (秒)
    _SAVE_DEBOUNCE = 5

    def __init__(self, context: star.Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

        # open_id -> nickname
        self.user_cache: dict[str, str] = {}
        # group_id -> {nickname -> open_id} (群成员映射)
        self.group_members_cache: dict[str, dict[str, str]] = {}
        # group_id -> cache_time (缓存时间戳)
        self._group_members_cache_time: dict[str, float] = {}
        # group_id -> group info cache
        self.group_info_cache: dict[str, dict] = {}
        # group_id -> cache_time
        self._group_info_cache_time: dict[str, float] = {}

        # 已添加表情回复的消息 ID (使用 OrderedDict 保证 FIFO)
        self._reacted_messages: OrderedDict[str, bool] = OrderedDict()

        # group_id -> deque of messages
        self._history_maxlen = self.config.get("history_inject_count", 20) or 20
        self.group_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self._history_maxlen)
        )

        # 持久化存储路径
        self._data_dir = os.path.join(os.path.dirname(__file__), "data")
        self._history_file = os.path.join(self._data_dir, "group_history.json")

        # 历史保存防抖
        self._last_save_time: float = 0
        self._pending_save: bool = False

        # 加载持久化的历史记录
        self._load_history()

        # 设置流式卡片的 monkey patch
        if self.config.get("enable_streaming_card", False):
            self._setup_streaming_patch()

    def _load_history(self):
        """从文件加载历史记录"""
        if not os.path.exists(self._history_file):
            return

        try:
            with open(self._history_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            for group_id, items in data.items():
                self.group_history[group_id] = deque(items, maxlen=self._history_maxlen)

            logger.info(f"[lark_enhance] Loaded history for {len(data)} groups")
        except Exception as e:
            logger.error(f"[lark_enhance] Failed to load history: {e}")

    def _setup_streaming_patch(self):
        """设置流式卡片的 monkey patch"""
        global _original_lark_send_streaming

        try:
            from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

            # 避免重复 patch
            if _original_lark_send_streaming is not None:
                logger.debug("[lark_enhance] Streaming patch already applied")
                return

            _original_lark_send_streaming = LarkMessageEvent.send_streaming

            # 保存 self 引用给闭包使用
            plugin_instance = self

            async def patched_send_streaming(event_self, generator, use_fallback: bool = False):
                """Monkey-patched send_streaming 方法，使用流式卡片"""
                # 检查是否是飞书事件
                if not hasattr(event_self, "bot") or event_self.bot is None:
                    return await _original_lark_send_streaming(event_self, generator, use_fallback)

                lark_client = event_self.bot
                chat_id = event_self.message_obj.group_id or event_self.get_sender_id()
                message_id = event_self.message_obj.message_id

                if not chat_id or not message_id:
                    return await _original_lark_send_streaming(event_self, generator, use_fallback)

                streaming_card = LarkStreamingCard(
                    lark_client=lark_client,
                    chat_id=chat_id,
                    reply_to_message_id=message_id,
                )

                # 创建初始卡片
                if not await streaming_card.create_initial_card():
                    logger.warning("[lark_enhance] Failed to create streaming card, using fallback")
                    return await _original_lark_send_streaming(event_self, generator, use_fallback)

                # 处理流式内容
                full_content = ""
                try:
                    async for chain in generator:
                        if chain and chain.chain:
                            for comp in chain.chain:
                                if isinstance(comp, Plain):
                                    full_content += comp.text
                            # 更新卡片
                            await streaming_card.update_card(full_content)

                    # 清洗最终内容
                    full_content = plugin_instance._clean_content(full_content)

                    # 完成卡片
                    await streaming_card.finalize_card(full_content)

                    logger.info(f"[lark_enhance] Streaming card completed, length: {len(full_content)}")

                    # 调用父类方法更新统计
                    from astrbot.core.platform.astr_message_event import AstrMessageEvent as BaseEvent
                    await BaseEvent.send_streaming(event_self, _empty_generator(), use_fallback)

                except Exception as e:
                    logger.error(f"[lark_enhance] Streaming card error: {e}")
                    if full_content:
                        await streaming_card.finalize_card(full_content + "\n\n*（输出中断）*")

            LarkMessageEvent.send_streaming = patched_send_streaming
            logger.info("[lark_enhance] Streaming card patch applied successfully")

        except ImportError as e:
            logger.warning(f"[lark_enhance] Failed to import LarkMessageEvent: {e}")
        except Exception as e:
            logger.error(f"[lark_enhance] Failed to setup streaming patch: {e}")

    def _save_history(self, force: bool = False):
        """将历史记录保存到文件（带防抖机制）"""
        now = time.time()

        # 如果距离上次保存不足防抖间隔，标记待保存
        if not force and now - self._last_save_time < self._SAVE_DEBOUNCE:
            self._pending_save = True
            return

        try:
            os.makedirs(self._data_dir, exist_ok=True)

            data = {
                group_id: list(items)
                for group_id, items in self.group_history.items()
                if items
            }

            with open(self._history_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            self._last_save_time = now
            self._pending_save = False
            logger.debug(f"[lark_enhance] Saved history for {len(data)} groups")
        except Exception as e:
            logger.error(f"[lark_enhance] Failed to save history: {e}")

    def _flush_pending_save(self):
        """强制保存待保存的历史记录"""
        if self._pending_save:
            self._save_history(force=True)

    def _ensure_history_deque(self, group_id: str, history_count: int):
        """确保 deque 长度符合配置"""
        if self.group_history[group_id].maxlen != history_count:
            old_data = list(self.group_history[group_id])
            self.group_history[group_id] = deque(old_data, maxlen=history_count)
            self._history_maxlen = history_count

    def _clear_history_for_session(self, unified_msg_origin: str):
        """清空指定会话的历史记录"""
        parts = unified_msg_origin.split(":")
        if len(parts) >= 3 and parts[0] == "lark":
            target_id = parts[2]
            if target_id in self.group_history:
                self.group_history[target_id].clear()
                self._save_history(force=True)
                logger.info(
                    f"[lark_enhance] Cleared history for session: {unified_msg_origin}"
                )

    @staticmethod
    def _is_lark_event(event: AstrMessageEvent) -> bool:
        return event.get_platform_name() == "lark"

    @staticmethod
    def _get_lark_client(event: AstrMessageEvent) -> Any | None:
        return getattr(event, "bot", None)

    def _is_cache_valid(self, cache_time: float) -> bool:
        """检查缓存是否有效"""
        return time.time() - cache_time < self._CACHE_TTL

    async def _get_user_nickname(
        self, lark_client: Any, open_id: str, event: AstrMessageEvent = None
    ) -> str | None:
        if open_id in self.user_cache:
            return self.user_cache[open_id]

        # 避免查询机器人自己
        if event and open_id == event.get_self_id():
            return self.config.get("bot_name", "助手")

        logger.debug(f"[lark_enhance] Querying Lark user info for open_id: {open_id}")

        try:
            from lark_oapi.api.contact.v3 import GetUserRequest

            request = (
                GetUserRequest.builder()
                .user_id(open_id)
                .user_id_type("open_id")
                .build()
            )

            contact = getattr(lark_client, "contact", None)
            if contact is None or contact.v3 is None or contact.v3.user is None:
                logger.warning(
                    "[lark_enhance] lark_client.contact 未初始化，无法获取用户信息"
                )
                return None

            response = await contact.v3.user.aget(request)

            if response.success() and response.data and response.data.user:
                nickname = response.data.user.name
                self.user_cache[open_id] = nickname
                return nickname
            elif response.code == 41050:
                logger.debug(
                    f"获取飞书用户信息失败 (权限不足): {response.msg}。可能是机器人ID或外部联系人。"
                )
                self.user_cache[open_id] = f"用户({open_id[-4:]})"
            else:
                logger.warning(f"获取飞书用户信息失败: {response.code} - {response.msg}")
        except Exception as e:
            logger.error(f"获取飞书用户信息异常: {e}")

        return None

    async def _get_group_info(self, lark_client: Any, chat_id: str) -> dict | None:
        """获取群组信息（名称和描述）"""
        # 检查缓存是否有效
        if chat_id in self.group_info_cache:
            if self._is_cache_valid(self._group_info_cache_time.get(chat_id, 0)):
                return self.group_info_cache[chat_id]

        logger.debug(f"[lark_enhance] Querying Lark group info for chat_id: {chat_id}")

        try:
            from lark_oapi.api.im.v1 import GetChatRequest

            request = GetChatRequest.builder().chat_id(chat_id).build()

            im = getattr(lark_client, "im", None)
            if im is None or im.v1 is None or im.v1.chat is None:
                logger.warning(
                    "[lark_enhance] lark_client.im.v1.chat 未初始化，无法获取群组信息"
                )
                return None

            response = await im.v1.chat.aget(request)

            if response.success() and response.data:
                group_info = {
                    "name": getattr(response.data, "name", None),
                    "description": getattr(response.data, "description", None),
                }
                self.group_info_cache[chat_id] = group_info
                self._group_info_cache_time[chat_id] = time.time()
                return group_info
            else:
                logger.warning(
                    f"获取飞书群组信息失败: {response.code} - {response.msg}"
                )
        except Exception as e:
            logger.error(f"获取飞书群组信息异常: {e}")

        return None

    async def _get_group_members(
        self, lark_client: Any, chat_id: str
    ) -> dict[str, str]:
        """获取群成员列表，返回 nickname -> open_id 的映射"""
        # 检查缓存是否有效
        if chat_id in self.group_members_cache:
            if self._is_cache_valid(self._group_members_cache_time.get(chat_id, 0)):
                return self.group_members_cache[chat_id]

        logger.debug(
            f"[lark_enhance] Querying Lark group members for chat_id: {chat_id}"
        )
        members_map: dict[str, str] = {}

        try:
            from lark_oapi.api.im.v1 import GetChatMembersRequest

            im = getattr(lark_client, "im", None)
            if im is None or im.v1 is None or im.v1.chat_members is None:
                logger.warning(
                    "[lark_enhance] lark_client.im.v1.chat_members 未初始化，无法获取群成员"
                )
                return members_map

            page_token = None
            while True:
                request_builder = (
                    GetChatMembersRequest.builder()
                    .chat_id(chat_id)
                    .member_id_type("open_id")
                    .page_size(100)
                )

                if page_token:
                    request_builder = request_builder.page_token(page_token)

                request = request_builder.build()
                response = await im.v1.chat_members.aget(request)

                if not response.success():
                    logger.warning(
                        f"获取飞书群成员失败: {response.code} - {response.msg}"
                    )
                    break

                if response.data and response.data.items:
                    for member in response.data.items:
                        member_id = getattr(member, "member_id", None)
                        name = getattr(member, "name", None)
                        if member_id and name:
                            members_map[name] = member_id
                            self.user_cache[member_id] = name

                if (
                    response.data
                    and response.data.has_more
                    and response.data.page_token
                ):
                    page_token = response.data.page_token
                else:
                    break

            self.group_members_cache[chat_id] = members_map
            self._group_members_cache_time[chat_id] = time.time()
            logger.info(
                f"[lark_enhance] Loaded {len(members_map)} members for group {chat_id}"
            )

        except Exception as e:
            logger.error(f"获取飞书群成员异常: {e}")

        return members_map

    def _clean_content(self, content_str: str) -> str:
        """清洗消息内容，尝试从 JSON/Python-repr 中提取纯文本"""
        if not content_str:
            return content_str

        content_str = content_str.strip()

        # 快速检查：如果不是以 [ 或 { 开头，大概率是普通文本
        if not (content_str.startswith("[") or content_str.startswith("{")):
            return content_str

        # 尝试解析为 JSON
        try:
            data = json.loads(content_str)
            result = self._extract_text_from_data(data)
            return result if result else content_str
        except json.JSONDecodeError:
            pass

        # 尝试解析为 Python 字面量 (处理单引号情况)
        try:
            data = ast.literal_eval(content_str)
            result = self._extract_text_from_data(data)
            return result if result else content_str
        except (ValueError, SyntaxError):
            pass

        return content_str

    def _extract_text_from_data(self, data: Any) -> str:
        """递归从 list/dict 中提取 text 字段"""
        texts = []
        if isinstance(data, list):
            for item in data:
                result = self._extract_text_from_data(item)
                if result:
                    texts.append(result)
        elif isinstance(data, dict):
            if "text" in data:
                text_value = data["text"]
                if isinstance(text_value, str):
                    if text_value.strip().startswith(
                        "["
                    ) or text_value.strip().startswith("{"):
                        return self._clean_content(text_value)
                    return text_value
                return str(text_value)
            for value in data.values():
                if isinstance(value, (list, dict)):
                    result = self._extract_text_from_data(value)
                    if result:
                        texts.append(result)
        elif isinstance(data, str):
            if data.strip().startswith("[") or data.strip().startswith("{"):
                return self._clean_content(data)
            return data

        return "".join(texts)

    async def _get_message_content(
        self,
        lark_client: Any,
        message_id: str,
    ) -> tuple[str, str | None] | None:
        """获取消息内容和发送者信息"""
        try:
            from lark_oapi.api.im.v1 import GetMessageRequest

            request = GetMessageRequest.builder().message_id(message_id).build()

            im = getattr(lark_client, "im", None)
            if im is None or im.v1 is None or im.v1.message is None:
                logger.warning(
                    "[lark_enhance] lark_client.im 未初始化，无法获取引用消息"
                )
                return None

            response = await im.v1.message.aget(request)

            if response.success() and response.data and response.data.items:
                msg_item = response.data.items[0]

                # 获取发送者信息
                sender_name = None
                sender = getattr(msg_item, "sender", None)
                if sender:
                    sender_id_obj = getattr(sender, "sender_id", None)
                    if sender_id_obj:
                        sender_open_id = getattr(sender_id_obj, "open_id", None)
                        if sender_open_id:
                            sender_name = await self._get_user_nickname(
                                lark_client, sender_open_id
                            )

                body = getattr(msg_item, "body", None)
                content = await self._parse_message_body(lark_client, body)

                return content, sender_name
            else:
                logger.warning(
                    f"获取飞书消息内容失败: {response.code} - {response.msg}"
                )
        except Exception as e:
            logger.error(f"获取飞书消息内容异常: {e}")

        return None

    async def _parse_message_body(self, lark_client: Any, body: Any) -> str | None:
        """解析 Lark 消息体 content"""
        content = getattr(body, "content", None)
        if not content:
            return None

        try:
            content_json = json.loads(content)

            # 处理 text 消息
            if "text" in content_json:
                text_content = content_json["text"]
                return self._clean_content(text_content)

            # 处理 post 消息
            if "content" in content_json:
                texts = []
                for line in content_json.get("content", []):
                    for segment in line:
                        tag = segment.get("tag")
                        if tag == "text":
                            texts.append(segment.get("text", ""))
                        elif tag == "at":
                            user_id = segment.get("user_id")
                            if user_id:
                                real_name = await self._get_user_nickname(
                                    lark_client, user_id
                                )
                                texts.append(f"@{real_name or user_id}")
                            else:
                                texts.append("@未知用户")
                return "".join(texts)

            return content
        except Exception:
            return content

    @filter.platform_adapter_type(filter.PlatformAdapterType.LARK)
    async def on_message(self, event: AstrMessageEvent):
        """监听飞书平台的所有消息事件"""
        logger.debug(
            f"[lark_enhance] Processing message: {event.message_obj.message_id}"
        )

        lark_client = self._get_lark_client(event)
        if lark_client is None:
            logger.warning("[lark_enhance] lark_client is None")
            return

        sender_id = event.get_sender_id()
        enable_real_name = self.config.get("enable_real_name", True)

        # 1. 增强用户信息 (获取真实昵称)
        if sender_id and enable_real_name:
            nickname = await self._get_user_nickname(lark_client, sender_id, event)
            if nickname:
                logger.debug(f"[lark_enhance] Found nickname: {nickname} for {sender_id}")
                event.message_obj.sender.nickname = nickname

            # 增强 Mention 用户信息
            for comp in event.message_obj.message:
                if isinstance(comp, At) and comp.qq:
                    real_name = await self._get_user_nickname(
                        lark_client, comp.qq, event
                    )
                    if real_name:
                        logger.debug(f"[lark_enhance] Resolve At: {comp.qq} -> {real_name}")
                        comp.name = real_name

            # 重新构建 message_str
            new_msg_str = ""
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    new_msg_str += f"@{comp.name or comp.qq} "
                elif hasattr(comp, "text"):
                    new_msg_str += comp.text

            if new_msg_str:
                event.message_obj.message_str = new_msg_str
                event.message_str = new_msg_str

        # 2. 记录群聊历史
        group_id = event.message_obj.group_id
        history_count = self.config.get("history_inject_count", 20)
        if group_id and history_count and history_count > 0:
            try:
                self._ensure_history_deque(group_id, history_count)

                time_str = datetime.datetime.now().strftime("%H:%M:%S")
                sender_name = (
                    event.message_obj.sender.nickname or sender_id or "未知用户"
                )
                content_str = self._clean_content(event.message_str)

                if content_str:  # 只记录非空内容
                    record_item = {
                        "msg_id": event.message_obj.message_id,
                        "time": time_str,
                        "sender": sender_name,
                        "content": content_str,
                    }
                    self.group_history[group_id].append(record_item)
                    self._save_history()
                    logger.debug(
                        f"[lark_enhance] Recorded message for group {group_id}: {content_str[:20]}..."
                    )
            except Exception as e:
                logger.error(f"[lark_enhance] Failed to record message history: {e}")

        # 3. 获取群组信息
        if group_id and self.config.get("enable_group_info", True):
            group_info = await self._get_group_info(lark_client, group_id)
            if group_info:
                event.set_extra("lark_group_info", group_info)

        # 4. 处理引用消息
        if self.config.get("enable_quoted_content", True):
            raw_msg = event.message_obj.raw_message
            parent_id = getattr(raw_msg, "parent_id", None)
            if parent_id:
                logger.debug(
                    f"[lark_enhance] Found parent_id: {parent_id}, fetching quoted content..."
                )
                result = await self._get_message_content(lark_client, parent_id)

                if result:
                    quoted_content, sender_name = result
                    logger.debug(
                        f"[lark_enhance] Fetched quoted content: {quoted_content}, sender: {sender_name}"
                    )
                    event.set_extra("lark_quoted_content", quoted_content)
                    event.set_extra("lark_quoted_sender", sender_name)

    @filter.after_message_sent()
    async def on_message_sent(self, event: AstrMessageEvent):
        """记录机器人自己发送的消息到群聊历史，并处理 /reset 命令"""
        if not self._is_lark_event(event):
            return

        # 强制保存待保存的历史（利用消息发送事件作为触发点）
        self._flush_pending_save()

        # 处理 /reset 命令
        if event.get_extra("_clean_ltm_session", False):
            unified_msg_origin = event.unified_msg_origin
            if unified_msg_origin:
                self._clear_history_for_session(unified_msg_origin)

        # 记录机器人自己发送的消息
        group_id = event.message_obj.group_id
        if not group_id:
            return

        history_count = self.config.get("history_inject_count", 20)
        if not history_count or history_count <= 0:
            return

        try:
            self._ensure_history_deque(group_id, history_count)

            time_str = datetime.datetime.now().strftime("%H:%M:%S")
            sender_name = self.config.get("bot_name", "助手")

            content_str = ""
            result = event.get_result()
            if result and result.chain:
                texts = [c.text for c in result.chain if isinstance(c, Plain)]
                content_str = "".join(texts)

            if not content_str:
                return

            content_str = self._clean_content(content_str)
            if not content_str:
                return

            msg_id = f"sent_{int(datetime.datetime.now().timestamp())}"
            record_item = {
                "msg_id": msg_id,
                "time": time_str,
                "sender": sender_name,
                "content": content_str,
            }

            self.group_history[group_id].append(record_item)
            self._save_history()
            logger.debug(
                f"[lark_enhance] Recorded SELF message for group {group_id}: {content_str[:20]}..."
            )
        except Exception as e:
            logger.error(f"[lark_enhance] Failed to record self message history: {e}")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在请求 LLM 前，将增强的信息注入到 prompt 中。"""
        if not self._is_lark_event(event):
            return

        # 清理 Context 中的 tool_calls
        if self.config.get("enable_context_cleaner", True) and req.contexts:
            new_contexts = copy.deepcopy(req.contexts)
            cleaned_contexts = []

            for ctx in new_contexts:
                if ctx.get("role") == "assistant" and "tool_calls" in ctx:
                    logger.debug(
                        f"[lark_enhance] Cleaning tool_calls from context: {ctx.get('tool_calls')}"
                    )
                    ctx.pop("tool_calls", None)
                    if not ctx.get("content"):
                        ctx["content"] = "（已执行工具调用）"
                    cleaned_contexts.append(ctx)
                elif ctx.get("role") == "tool":
                    logger.debug(
                        f"[lark_enhance] Cleaning tool message from context: {ctx}"
                    )
                    content = ctx.get("content", "")
                    if "The tool has no return value" in content:
                        tool_content = "（已执行动作）"
                    else:
                        tool_content = f"（工具执行结果：{content}）"

                    if cleaned_contexts and cleaned_contexts[-1].get("role") == "user":
                        cleaned_contexts[-1]["content"] = (
                            cleaned_contexts[-1].get("content", "") + "\n" + tool_content
                        )
                    else:
                        cleaned_contexts.append({"role": "user", "content": tool_content})
                else:
                    cleaned_contexts.append(ctx)

            req.contexts = cleaned_contexts

        prompts_to_inject = []

        # 1. 注入群组信息
        if self.config.get("enable_group_info", True):
            group_info = event.get_extra("lark_group_info")
            if group_info:
                group_name = group_info.get("name")
                group_desc = group_info.get("description")
                if group_name:
                    info_parts = [f"群名称：{group_name}"]
                    if group_desc:
                        info_parts.append(f"群描述：{group_desc}")
                    prompts_to_inject.append(
                        f"[当前群组信息]\n" + "\n".join(info_parts)
                    )

        # 2. 注入引用消息
        if self.config.get("enable_quoted_content", True):
            quoted_content = event.get_extra("lark_quoted_content")
            quoted_sender = event.get_extra("lark_quoted_sender")

            if quoted_content:
                logger.debug("[lark_enhance] Injecting quoted content into LLM prompt.")
                if quoted_sender:
                    prompts_to_inject.append(
                        f"「{quoted_sender}」在回复的消息中说道：\n{quoted_content}\n"
                    )
                else:
                    prompts_to_inject.append(f"[引用消息]\n{quoted_content}\n")

        # 3. 注入群聊历史
        history_count = self.config.get("history_inject_count", 0)
        group_id = event.message_obj.group_id

        if history_count > 0 and group_id:
            history_list = list(self.group_history.get(group_id, []))
            if history_list:
                current_msg_id = event.message_obj.message_id
                filtered_history = [
                    f"[{item['time']}] {item['sender']}: {item['content']}"
                    for item in history_list
                    if item["msg_id"] != current_msg_id
                ]

                if filtered_history:
                    recent_history = filtered_history[-history_count:]
                    history_str = "\n".join(recent_history)
                    prompts_to_inject.append(
                        f"\n[当前群聊最近 {len(recent_history)} 条消息记录（仅供参考，不包含当前消息）]\n{history_str}\n"
                    )

        if prompts_to_inject:
            final_inject = (
                "\n----------------\n".join(prompts_to_inject) + "\n----------------\n\n"
            )
            req.system_prompt = (req.system_prompt or "") + "\n\n" + final_inject

        # 调试日志
        logger.debug("=" * 20 + " [lark_enhance] LLM Request Payload " + "=" * 20)
        logger.debug(f"System Prompt: {req.system_prompt}")
        logger.debug(f"Contexts (History): {req.contexts}")
        logger.debug(f"Current Prompt: {req.prompt}")
        logger.debug("=" * 60)

    @filter.llm_tool(name="lark_emoji_reply")
    async def lark_emoji_reply(self, event: AstrMessageEvent, emoji: str):
        """飞书表情回复工具。仅在非常有必要对用户的消息表达强烈情感（如点赞、开心、收到）且无需文字回复时使用。请勿滥用此工具，不要对每条消息都进行表情回复。

        常用表情代码值：
        THUMBSUP(点赞), THUMBSDOWN(踩), FIGHTON(加油), THANKS(感谢), HEART(比心), OK(OK), YES(Yes/V手势), NO(NO/X手势)
        CLAP(鼓掌), LAUGH(大笑), WAH(哇), CRY(流泪), GLANCE(狗头/斜眼笑), DULL(呆无辜), KISS(飞吻), ROSE(玫瑰)
        DONE(完成), CHECK(勾), CROSS(叉), WAVE(再见), BLUSH(脸红), SOB(大哭), JOY(破涕为笑)
        BEER(啤酒), CAKE(蛋糕), JIAYI(+1), HIGHFIVE(击掌), EYES(围观), AWESOME(666), FIRE(给力/火), MUSCLE(强/肌肉)
        SHAKE(握手), SALUTE(敬礼), WINK(眨眼), SHHH(嘘), PROUD(得意), HEARTBROKEN(心碎), POOP(便便), GIFT(礼物)
        CUCUMBER(吃瓜), SLAP(打脸), SPIT(吐血), ANGRY(生气), WHOA(震惊), SWEAT(汗), EATING(吃饭), SLEEP(睡觉)
        SMART(机智), MURMUR(暗中观察), GET(Get!), LUCK(祝好/锦鲤), HUG(拥抱)
        SUN(太阳), MOON(月亮), RAINBOW(彩虹), STAR(星星), FLOWER(花), SNOWMAN(雪人), UNICORN(独角兽)
        SKULL(骷髅), GHOST(幽灵), ALIEN(外星人), ROBOT(机器人)
        MONKEY(猴子), DOG(狗), CAT(猫), PIG(猪), CHICKEN(鸡), BEAR(熊), PANDA(熊猫), RABBIT(兔子), KOALA(考拉)
        TIGER(老虎), LION(狮子), HORSE(马), COW(牛), DRAGON(龙), WHALE(鲸鱼), DOLPHIN(海豚), FISH(鱼), OCTOPUS(章鱼)
        SHARK(鲨鱼), BUTTERFLY(蝴蝶), BEE(蜜蜂), SPIDER(蜘蛛), ANT(蚂蚁), SNAIL(蜗牛), LADYBEETLE(瓢虫)
        SCORPION(蝎子), MOSQUITO(蚊子), FLY(苍蝇), WORM(蠕虫), BUG(虫子)

        Args:
            emoji(string): 表情代码。请使用上述列表中的全大写英文代码。
        """
        if not self._is_lark_event(event):
            return "不是飞书平台，无法使用表情回复。"

        message_id = event.message_obj.message_id
        if message_id in self._reacted_messages:
            logger.debug(
                f"[lark_enhance] Message {message_id} already has emoji reaction, skipping"
            )
            return "该消息已添加过表情回复，每条消息只能添加一个表情。"

        try:
            await event.react(emoji)
            self._reacted_messages[message_id] = True

            # 限制集合大小，FIFO 移除旧记录
            while len(self._reacted_messages) > 1000:
                self._reacted_messages.popitem(last=False)

            logger.info(f"[lark_enhance] Reacted with {emoji} to message {message_id}")
            return None
        except Exception as e:
            logger.error(f"[lark_enhance] React failed: {e}")
            return f"添加 {emoji} 表情失败"

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在消息发送前处理，清洗消息格式并将文本中的 @名字 转换为飞书 At 组件"""
        if not self._is_lark_event(event):
            return

        result = event.get_result()
        if result is None or not result.chain:
            return

        # 如果启用了流式卡片且是流式完成状态，跳过处理（已由流式卡片处理）
        if (
            self.config.get("enable_streaming_card", False)
            and result.result_content_type == ResultContentType.STREAMING_FINISH
        ):
            return

        # 第一步：清洗消息内容（处理 LLM 输出的序列化格式）
        cleaned_chain = []
        for comp in result.chain:
            if isinstance(comp, Plain):
                cleaned_text = self._clean_content(comp.text)
                if cleaned_text != comp.text:
                    logger.info(
                        f"[lark_enhance] Cleaned message format: {comp.text[:50]}... -> {cleaned_text[:50]}..."
                    )
                cleaned_chain.append(Plain(cleaned_text))
            else:
                cleaned_chain.append(comp)
        result.chain = cleaned_chain

        # 第二步：处理 @ 转换（如果启用）
        if not self.config.get("enable_mention_convert", True):
            return

        lark_client = self._get_lark_client(event)
        if lark_client is None:
            return

        group_id = event.message_obj.group_id
        if not group_id:
            return

        members_map = await self._get_group_members(lark_client, group_id)
        if not members_map:
            logger.debug(
                "[lark_enhance] No group members found, skipping mention conversion"
            )
            return

        # 构建正则模式
        sorted_names = sorted(members_map.keys(), key=len, reverse=True)
        if not sorted_names:
            return

        escaped_names = [re.escape(name) for name in sorted_names]
        pattern = re.compile(r"@(" + "|".join(escaped_names) + r")")

        new_chain = []
        for comp in result.chain:
            if not isinstance(comp, Plain):
                new_chain.append(comp)
                continue

            text = comp.text
            last_end = 0
            segments = []

            for match in pattern.finditer(text):
                name = match.group(1)
                open_id = members_map.get(name)
                if not open_id:
                    continue

                if match.start() > last_end:
                    segments.append(Plain(text[last_end : match.start()]))

                segments.append(At(qq=open_id, name=name))
                last_end = match.end()

                logger.debug(
                    f"[lark_enhance] Converted @{name} to At component (open_id: {open_id})"
                )

            if last_end < len(text):
                segments.append(Plain(text[last_end:]))

            if segments:
                new_chain.extend(segments)
            else:
                new_chain.append(comp)

        result.chain = new_chain

