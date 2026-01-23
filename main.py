from __future__ import annotations

import atexit
import copy
import datetime
import inspect
import json
import re
import time
import uuid
from collections import OrderedDict, deque, defaultdict
from pathlib import Path
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import StarTools
from astrbot.core.message.message_event_result import ResultContentType

# Lark SDK imports (顶部导入，避免方法内 import)
from lark_oapi.api.im.v1 import (
    DeleteMessageRequest,
    GetChatRequest,
    GetChatMembersRequest,
    GetMessageRequest,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from lark_oapi.api.contact.v3 import GetUserRequest

# 保存原始的 send_streaming 方法
_original_lark_send_streaming = None
# 全局配置引用（用于 monkey patch）
_streaming_config: dict | None = None
# 全局 clean_content 函数引用
_clean_content_func = None


async def _empty_generator():
    """空的异步生成器，用于调用父类方法"""
    return
    yield  # 让它成为异步生成器


class LarkCardBuilder:
    """飞书消息卡片构建器 - 提供优雅的链式 API 构建卡片"""

    def __init__(self):
        self._elements: list[dict] = []
        self._config = {"wide_screen_mode": True}

    def markdown(self, content: str) -> "LarkCardBuilder":
        """添加 Markdown 元素"""
        self._elements.append({"tag": "markdown", "content": content})
        return self

    def divider(self) -> "LarkCardBuilder":
        """添加分割线"""
        self._elements.append({"tag": "hr"})
        return self

    def loading_indicator(self, text: str = "正在输入...") -> "LarkCardBuilder":
        """添加加载指示器"""
        self._elements.append({"tag": "markdown", "content": f"◉ *{text}*"})
        return self

    def thinking_indicator(self) -> "LarkCardBuilder":
        """添加思考中指示器"""
        return self.loading_indicator("思考中...")

    def build(self) -> str:
        """构建并返回卡片 JSON 字符串"""
        card = {
            "schema": "2.0",
            "config": self._config,
            "body": {
                "elements": self._elements if self._elements else [
                    {"tag": "markdown", "content": "◉ *思考中...*"}
                ],
            },
        }
        return json.dumps(card, ensure_ascii=False)

    @classmethod
    def streaming_card(cls, text: str, is_finished: bool = False) -> str:
        """快速创建流式输出卡片"""
        builder = cls()
        if text:
            builder.markdown(text)
        if not is_finished:
            builder.loading_indicator()
        return builder.build()


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

    async def create_initial_card(self) -> bool:
        """创建初始卡片消息"""
        try:
            content = LarkCardBuilder.streaming_card("", is_finished=False)

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
            content = LarkCardBuilder.streaming_card(text, is_finished=False)

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
            content = LarkCardBuilder.streaming_card(text, is_finished=True)

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
                logger.debug("[lark_enhance] Finalized streaming card")
                return True
            else:
                logger.warning(
                    f"[lark_enhance] Failed to finalize card: {response.code} - {response.msg}"
                )
                return False
        except Exception as e:
            logger.error(f"[lark_enhance] Finalize card exception: {e}")
            return False

    async def delete_card(self) -> bool:
        """删除卡片消息（用于内容为空时清理）"""
        if not self.card_message_id:
            return False

        try:
            request = (
                DeleteMessageRequest.builder()
                .message_id(self.card_message_id)
                .build()
            )

            response = await self.lark_client.im.v1.message.adelete(request)

            if response.success():
                logger.debug("[lark_enhance] Deleted empty streaming card")
                self.card_message_id = None
                return True
            else:
                logger.warning(
                    f"[lark_enhance] Failed to delete card: {response.code} - {response.msg}"
                )
                return False
        except Exception as e:
            logger.error(f"[lark_enhance] Delete card exception: {e}")
            return False


class UserMemoryStore:
    """用户记忆存储管理器 - 按群隔离的用户记忆系统"""

    # 记忆类型优先级（用于排序）
    TYPE_PRIORITY = {"instruction": 0, "preference": 1, "fact": 2}

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir / "user_memory"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        # 内存缓存: group_id -> group_data
        self._cache: dict[str, dict] = {}
        # 保存防抖
        self._pending_saves: set[str] = set()
        self._last_save_time: dict[str, float] = {}
        self._save_debounce = 5  # 秒

    def _get_file_path(self, group_id: str) -> Path:
        """获取群记忆文件路径"""
        # 使用安全的文件名
        safe_id = group_id.replace("/", "_").replace("\\", "_")
        return self._data_dir / f"{safe_id}.json"

    def _load_group_data(self, group_id: str) -> dict:
        """加载群记忆数据"""
        if group_id in self._cache:
            return self._cache[group_id]

        file_path = self._get_file_path(group_id)
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._cache[group_id] = data
                    return data
            except Exception as e:
                logger.error(f"[lark_enhance] Failed to load memory for group {group_id}: {e}")

        # 初始化空数据
        data = {"group_id": group_id, "users": {}, "updated_at": time.time()}
        self._cache[group_id] = data
        return data

    def _save_group_data(self, group_id: str, force: bool = False):
        """保存群记忆数据（带防抖）"""
        now = time.time()
        last_save = self._last_save_time.get(group_id, 0)

        if not force and now - last_save < self._save_debounce:
            self._pending_saves.add(group_id)
            return

        if group_id not in self._cache:
            return

        try:
            data = self._cache[group_id]
            data["updated_at"] = now
            file_path = self._get_file_path(group_id)

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            self._last_save_time[group_id] = now
            self._pending_saves.discard(group_id)
            logger.debug(f"[lark_enhance] Saved memory for group {group_id}")
        except Exception as e:
            logger.error(f"[lark_enhance] Failed to save memory for group {group_id}: {e}")

    def flush_all(self):
        """强制保存所有待保存的数据"""
        for group_id in list(self._pending_saves):
            self._save_group_data(group_id, force=True)

    def add_memory(
        self,
        group_id: str,
        user_id: str,
        memory_type: str,
        content: str,
        max_per_user: int = 20
    ) -> bool:
        """添加用户记忆"""
        if memory_type not in self.TYPE_PRIORITY:
            logger.warning(f"[lark_enhance] Invalid memory type: {memory_type}")
            return False

        data = self._load_group_data(group_id)

        if user_id not in data["users"]:
            data["users"][user_id] = {"memories": []}

        user_data = data["users"][user_id]
        memories = user_data["memories"]

        # 检查是否存在相似记忆（简单去重：相同类型且内容包含关系）
        for mem in memories:
            if mem["type"] == memory_type:
                # 如果新内容包含旧内容或旧内容包含新内容，更新而非新增
                if content in mem["content"] or mem["content"] in content:
                    mem["content"] = content
                    mem["updated_at"] = time.time()
                    self._save_group_data(group_id)
                    logger.info(f"[lark_enhance] Updated memory for user {user_id}: {content[:30]}...")
                    return True

        # 新增记忆
        new_memory = {
            "id": str(uuid.uuid4()),
            "type": memory_type,
            "content": content,
            "created_at": time.time(),
            "updated_at": time.time()
        }
        memories.append(new_memory)

        # 超出限制时，删除最旧的记忆
        if len(memories) > max_per_user:
            # 按 updated_at 排序，删除最旧的
            memories.sort(key=lambda x: x["updated_at"], reverse=True)
            removed = memories[max_per_user:]
            user_data["memories"] = memories[:max_per_user]
            logger.debug(f"[lark_enhance] Removed {len(removed)} old memories for user {user_id}")

        self._save_group_data(group_id)
        logger.info(f"[lark_enhance] Added memory for user {user_id}: {content[:30]}...")
        return True

    def get_memories(
        self,
        group_id: str,
        user_id: str,
        limit: int = 10
    ) -> list[dict]:
        """获取用户记忆（按优先级和时间排序）"""
        data = self._load_group_data(group_id)

        if user_id not in data["users"]:
            return []

        memories = data["users"][user_id]["memories"]

        # 按优先级和更新时间排序
        sorted_memories = sorted(
            memories,
            key=lambda x: (
                self.TYPE_PRIORITY.get(x["type"], 99),
                -x["updated_at"]
            )
        )

        return sorted_memories[:limit]

    def delete_memories(
        self,
        group_id: str,
        user_id: str,
        target: str = "all"
    ) -> int:
        """删除用户记忆，返回删除数量"""
        data = self._load_group_data(group_id)

        if user_id not in data["users"]:
            return 0

        user_data = data["users"][user_id]
        memories = user_data["memories"]
        original_count = len(memories)

        if target == "all":
            # 一键清除所有记忆
            user_data["memories"] = []
            deleted_count = original_count
        else:
            # 匹配删除：检查 content 是否包含 target 关键词
            target_lower = target.lower()
            user_data["memories"] = [
                mem for mem in memories
                if target_lower not in mem["content"].lower()
            ]
            deleted_count = original_count - len(user_data["memories"])

        if deleted_count > 0:
            self._save_group_data(group_id, force=True)
            logger.info(f"[lark_enhance] Deleted {deleted_count} memories for user {user_id}")

        return deleted_count

    def format_memories_for_prompt(self, memories: list[dict]) -> str:
        """格式化记忆列表用于 prompt 注入"""
        if not memories:
            return ""

        type_labels = {
            "instruction": "指令",
            "preference": "偏好",
            "fact": "事实"
        }

        lines = []
        for mem in memories:
            type_label = type_labels.get(mem["type"], mem["type"])
            lines.append(f"- {mem['content']}（{type_label}）")

        return "\n".join(lines)


class Main(star.Star):
    # 缓存 TTL (秒)
    _CACHE_TTL = 300  # 5 分钟
    # 历史保存防抖间隔 (秒)
    _SAVE_DEBOUNCE = 5
    # 用户缓存最大容量
    _USER_CACHE_MAX_SIZE = 5000
    # 内容清洗最大长度限制（防止解析炸弹）
    _CLEAN_CONTENT_MAX_LEN = 10000

    def __init__(self, context: star.Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

        # open_id -> (nickname, cache_time)
        self.user_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        # group_id -> {nickname -> open_id} (群成员映射)
        self.group_members_cache: dict[str, dict[str, str]] = {}
        # group_id -> cache_time (缓存时间戳)
        self._group_members_cache_time: dict[str, float] = {}
        # group_id -> compiled regex pattern
        self._mention_pattern_cache: dict[str, tuple[re.Pattern, float]] = {}
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

        # 持久化存储路径（使用框架规范方法）
        self._data_dir: Path = StarTools.get_data_dir("astrbot_plugin_lark_enhance")
        self._history_file: Path = self._data_dir / "group_history.json"

        # 历史保存防抖
        self._last_save_time: float = 0
        self._pending_save: bool = False

        # 加载持久化的历史记录
        self._load_history()

        # 初始化用户记忆存储
        self._memory_store = UserMemoryStore(self._data_dir)

        # 注册退出时保存
        atexit.register(self._atexit_save)

        # 设置全局配置引用（用于 monkey patch）
        global _streaming_config, _clean_content_func
        _streaming_config = self.config
        _clean_content_func = self._clean_content

        # 设置流式卡片的 monkey patch（当 AstrBot 启用流式输出时自动使用卡片展示）
        self._setup_streaming_patch()

    def _atexit_save(self):
        """程序退出时保存历史记录和用户记忆"""
        if self._pending_save or self.group_history:
            self._save_history(force=True)
        # 保存用户记忆
        self._memory_store.flush_all()

    def _load_history(self):
        """从文件加载历史记录"""
        if not self._history_file.exists():
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
        """设置流式卡片的 monkey patch

        当 AstrBot 框架启用流式输出时，自动使用飞书卡片展示打字机效果。
        注意：Monkey Patch 是一种临时方案，可能在框架更新后失效。
        长期方案是向 AstrBot 框架提交 PR，提供官方的流式输出 Hook。
        """
        global _original_lark_send_streaming

        try:
            from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
            from astrbot.core.platform.astr_message_event import AstrMessageEvent as BaseEvent

            # 验证 LarkMessageEvent 具有预期的 send_streaming 方法
            if not hasattr(LarkMessageEvent, "send_streaming"):
                logger.warning(
                    "[lark_enhance] LarkMessageEvent 没有 send_streaming 方法，"
                    "可能框架版本不兼容，跳过流式卡片补丁"
                )
                return

            # 检查方法签名是否符合预期（简单检查）
            sig = inspect.signature(LarkMessageEvent.send_streaming)
            params = list(sig.parameters.keys())
            if "generator" not in params:
                logger.warning(
                    "[lark_enhance] send_streaming 方法签名不符合预期，"
                    "可能框架版本不兼容，跳过流式卡片补丁"
                )
                return

            # 避免重复 patch
            if _original_lark_send_streaming is not None:
                logger.debug("[lark_enhance] Streaming patch already applied")
                return

            _original_lark_send_streaming = LarkMessageEvent.send_streaming

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
                                elif hasattr(comp, "type"):
                                    # 对非文本组件添加占位符
                                    full_content += f" [{comp.type}] "
                            # 更新卡片
                            await streaming_card.update_card(full_content)

                    # 清洗最终内容
                    if _clean_content_func:
                        full_content = _clean_content_func(full_content)

                    # 如果内容为空（例如只使用了表情回复工具），删除卡片
                    if not full_content.strip():
                        await streaming_card.delete_card()
                        logger.info("[lark_enhance] Deleted empty streaming card (no text content)")
                    else:
                        # 完成卡片
                        await streaming_card.finalize_card(full_content)
                        logger.info(f"[lark_enhance] Streaming card completed, length: {len(full_content)}")

                    # 调用父类方法更新统计
                    await BaseEvent.send_streaming(event_self, _empty_generator(), use_fallback)

                except Exception as e:
                    logger.error(f"[lark_enhance] Streaming card error: {e}")
                    if full_content:
                        await streaming_card.finalize_card(full_content + "\n\n*（输出中断）*")
                    else:
                        # 出错且无内容时也删除卡片
                        await streaming_card.delete_card()

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
            self._data_dir.mkdir(parents=True, exist_ok=True)

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

    def _get_user_from_cache(self, open_id: str) -> str | None:
        """从缓存获取用户昵称（带 TTL 检查）"""
        if open_id in self.user_cache:
            nickname, cache_time = self.user_cache[open_id]
            if self._is_cache_valid(cache_time):
                # 移动到末尾（LRU）
                self.user_cache.move_to_end(open_id)
                return nickname
            else:
                # 缓存过期，删除
                del self.user_cache[open_id]
        return None

    def _set_user_cache(self, open_id: str, nickname: str):
        """设置用户缓存（带容量限制）"""
        # 如果已存在，先删除（保证 move_to_end 效果）
        if open_id in self.user_cache:
            del self.user_cache[open_id]

        # 检查容量，移除最老的条目
        while len(self.user_cache) >= self._USER_CACHE_MAX_SIZE:
            self.user_cache.popitem(last=False)

        self.user_cache[open_id] = (nickname, time.time())

    async def _get_user_nickname(
        self, lark_client: Any, open_id: str, event: AstrMessageEvent | None = None
    ) -> str | None:
        # 检查缓存
        cached = self._get_user_from_cache(open_id)
        if cached is not None:
            return cached

        # 避免查询机器人自己
        if event and open_id == event.get_self_id():
            bot_name = self.config.get("bot_name", "助手")
            self._set_user_cache(open_id, bot_name)
            return bot_name

        logger.debug(f"[lark_enhance] Querying Lark user info for open_id: {open_id}")

        try:
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
                self._set_user_cache(open_id, nickname)
                return nickname
            elif response.code == 41050:
                logger.debug(
                    f"获取飞书用户信息失败 (权限不足): {response.msg}。可能是机器人ID或外部联系人。"
                )
                # 使用占位名并缓存，同时返回该占位名
                placeholder = f"用户({open_id[-4:]})"
                self._set_user_cache(open_id, placeholder)
                return placeholder
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
                            self._set_user_cache(member_id, name)

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
            # 清除该群的正则缓存
            self._mention_pattern_cache.pop(chat_id, None)
            logger.info(
                f"[lark_enhance] Loaded {len(members_map)} members for group {chat_id}"
            )

        except Exception as e:
            logger.error(f"获取飞书群成员异常: {e}")

        return members_map

    def _clean_content(self, content_str: str) -> str:
        """清洗消息内容，仅处理 AstrBot 序列化的消息组件格式。

        只清洗形如 [{'type': 'text', 'text': '...'}] 的 AstrBot 内部格式，
        不会影响用户讨论的普通代码或 JSON 数据。
        """
        if not content_str:
            return content_str

        content_str = content_str.strip()

        # 长度限制，防止解析炸弹
        if len(content_str) > self._CLEAN_CONTENT_MAX_LEN:
            return content_str

        # 快速检查：如果不是以 [ 开头，肯定不是消息组件格式
        if not content_str.startswith("["):
            return content_str

        # 尝试解析为 JSON
        try:
            data = json.loads(content_str)
        except json.JSONDecodeError:
            return content_str

        # 严格检测：只处理 AstrBot 消息组件格式
        # 格式必须是：列表，且列表中的字典包含 'type' 键
        if not self._is_astrbot_message_format(data):
            return content_str

        result = self._extract_text_from_data(data, depth=0)
        return result if result else content_str

    def _is_astrbot_message_format(self, data: Any) -> bool:
        """检测数据是否为 AstrBot 消息组件格式。

        AstrBot 格式特征：
        - 是一个列表
        - 列表中至少有一个字典包含 'type' 键
        - type 值为 'text', 'image', 'at' 等消息类型
        """
        if not isinstance(data, list):
            return False

        if not data:
            return False

        # 检查列表中是否有符合消息组件格式的字典
        valid_types = {'text', 'image', 'at', 'plain', 'face', 'record', 'video', 'file'}
        for item in data:
            if isinstance(item, dict) and 'type' in item:
                type_value = item.get('type', '').lower()
                if type_value in valid_types:
                    return True

        return False

    def _extract_text_from_data(self, data: Any, depth: int = 0) -> str:
        """递归从 list/dict 中提取 text 字段"""
        # 深度限制，防止过深嵌套
        if depth > 10:
            return ""

        texts = []
        if isinstance(data, list):
            for item in data:
                result = self._extract_text_from_data(item, depth + 1)
                if result:
                    texts.append(result)
        elif isinstance(data, dict):
            if "text" in data:
                text_value = data["text"]
                if isinstance(text_value, str):
                    if text_value.strip().startswith("[") or text_value.strip().startswith("{"):
                        return self._clean_content(text_value)
                    return text_value
                return str(text_value)
            for value in data.values():
                if isinstance(value, (list, dict)):
                    result = self._extract_text_from_data(value, depth + 1)
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

        # 0. 输出格式约束（防止 LLM 输出序列化格式）
        prompts_to_inject.append(
            "[输出格式要求]\n"
            "请直接用自然语言回复，不要输出任何序列化格式如 JSON、Python 列表/字典等。"
            "禁止输出类似 [{'type': 'text', 'text': '...'}] 这样的格式。"
        )

        # 0.5 记忆功能提示（仅在启用时）
        if self.config.get("enable_user_memory", True):
            prompts_to_inject.append(
                "[记忆功能]\n"
                "你具有记忆用户信息的能力。当用户要求你记住某些信息（如称呼、职业、偏好等）时，"
                "请使用 lark_save_memory 工具保存。当用户询问你记得什么时，使用 lark_list_memory 工具查询。"
                "当用户要求忘记信息时，使用 lark_forget_memory 工具删除。"
            )

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

        # 4. 注入用户记忆
        if self.config.get("enable_user_memory", True) and group_id:
            sender_id = event.get_sender_id()
            if sender_id:
                inject_limit = self.config.get("memory_inject_limit", 10)
                memories = self._memory_store.get_memories(group_id, sender_id, limit=inject_limit)
                if memories:
                    memory_str = self._memory_store.format_memories_for_prompt(memories)
                    sender_name = event.message_obj.sender.nickname or sender_id
                    prompts_to_inject.append(
                        f"[关于当前用户「{sender_name}」的记忆]\n{memory_str}"
                    )
                    logger.debug(f"[lark_enhance] Injected {len(memories)} memories for user {sender_id}")

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

        常用表情代码（基于飞书官方 API）：
        THUMBSUP(点赞), THUMBSDOWN(踩), JIAYI(+1), OK, YES, NO, DONE(完成), CHECKMARK(勾), CROSSMARK(叉)
        SMILE(微笑), LAUGH(大笑), JOYFUL(开心), BLUSH(脸红), WINK(眨眼), SHY(害羞), SMIRK(坏笑), PROUD(得意)
        THANKS(感谢), HEART(比心), KISS(飞吻), LOVE(爱心), HUG(拥抱), ROSE(玫瑰), FINGERHEARD(比心手势)
        APPLAUSE(鼓掌), CLAP(鼓掌), HIGHFIVE(击掌), FISTBUMP(碰拳), SHAKE(握手), SALUTE(敬礼), WAVE(再见)
        AWESOME(666), MUSCLE(强), FIRE(火), TROPHY(奖杯), LGTM(LGTM), GET(收到), ONIT(搞定), PRAISE(赞)
        CRY(流泪), SOB(大哭), TEARS(流泪), HEARTBROKEN(心碎), WRONGED(委屈), COMFORT(安慰)
        ANGRY(生气), SCOWL(皱眉), FROWN(不高兴), SPEECHLESS(无语), SWEAT(汗), FACEPALM(捂脸)
        WOW(哇), SHOCKED(震惊), PETRIFIED(石化), TERROR(恐惧), DIZZY(晕), SKULL(骷髅)
        GLANCE(斜眼), DULL(呆), SMART(机智), THINKING(思考), SHHH(嘘), SILENT(沉默)
        BEER(啤酒), COFFEE(咖啡), CAKE(蛋糕), GIFT(礼物), REDPACKET(红包), PARTY(派对)
        CUCUMBER(吃瓜), SLAP(打脸), POOP(便便), SPITBLOOD(吐血), RAINBOWPUKE(彩虹吐)
        SLEEP(睡觉), YAWN(打哈欠), EATING(吃饭), SICK(生病), DROWSY(困)
        BEAR(熊), HUSKY(哈士奇), BULL(牛), CALF(小牛), SNOWMAN(雪人), LUCK(锦鲤)

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

    @filter.llm_tool(name="lark_save_memory")
    async def lark_save_memory(
        self,
        event: AstrMessageEvent,
        memory_type: str,
        content: str
    ):
        """保存当前群内用户的记忆。当用户明确要求记住某些信息时使用此工具。

        记忆仅在当前群生效，不会影响用户在其他群的交互。

        Args:
            memory_type(string): 记忆类型，必须是以下之一：
                - preference: 用户偏好（如称呼、回复风格、语言偏好）
                - fact: 用户事实（如职业、负责的项目、技能特长）
                - instruction: 持久指令（如"总是用英文回复"、"不要用表情"）
            content(string): 要记住的内容，用简洁的陈述句描述（如"希望被称呼为小王"、"是后端开发工程师"）
        """
        if not self._is_lark_event(event):
            return "不是飞书平台，无法使用记忆功能。"

        if not self.config.get("enable_user_memory", True):
            return "记忆功能未启用。"

        group_id = event.message_obj.group_id
        if not group_id:
            return "记忆功能仅在群聊中可用。"

        sender_id = event.get_sender_id()
        if not sender_id:
            return "无法获取用户信息。"

        # 验证记忆类型
        valid_types = {"preference", "fact", "instruction"}
        if memory_type not in valid_types:
            return f"无效的记忆类型。请使用: {', '.join(valid_types)}"

        max_per_user = self.config.get("memory_max_per_user", 20)
        success = self._memory_store.add_memory(
            group_id=group_id,
            user_id=sender_id,
            memory_type=memory_type,
            content=content,
            max_per_user=max_per_user
        )

        if success:
            return f"好的，我记住了：{content}"
        else:
            return "保存记忆失败，请稍后重试。"

    @filter.llm_tool(name="lark_list_memory")
    async def lark_list_memory(self, event: AstrMessageEvent):
        """查询用户在当前群的所有记忆。当用户询问"你记得我什么"、"你对我有什么印象"时使用此工具。

        返回该用户在当前群的所有记忆列表。
        """
        if not self._is_lark_event(event):
            return "不是飞书平台，无法使用记忆功能。"

        if not self.config.get("enable_user_memory", True):
            return "记忆功能未启用。"

        group_id = event.message_obj.group_id
        if not group_id:
            return "记忆功能仅在群聊中可用。"

        sender_id = event.get_sender_id()
        if not sender_id:
            return "无法获取用户信息。"

        memories = self._memory_store.get_memories(group_id, sender_id, limit=50)

        if not memories:
            return "我还没有记住关于你的任何信息。"

        memory_str = self._memory_store.format_memories_for_prompt(memories)
        return f"在这个群里，我记得关于你的以下信息：\n{memory_str}"

    @filter.llm_tool(name="lark_forget_memory")
    async def lark_forget_memory(self, event: AstrMessageEvent, target: str = "all"):
        """删除用户在当前群的记忆。当用户要求忘记某些信息或清除所有记忆时使用此工具。

        Args:
            target(string): 删除目标
                - "all": 一键清除所有记忆
                - 具体关键词: 删除包含该关键词的记忆（如"称呼"、"职业"、"英文"）
        """
        if not self._is_lark_event(event):
            return "不是飞书平台，无法使用记忆功能。"

        if not self.config.get("enable_user_memory", True):
            return "记忆功能未启用。"

        group_id = event.message_obj.group_id
        if not group_id:
            return "记忆功能仅在群聊中可用。"

        sender_id = event.get_sender_id()
        if not sender_id:
            return "无法获取用户信息。"

        deleted_count = self._memory_store.delete_memories(group_id, sender_id, target)

        if deleted_count == 0:
            if target == "all":
                return "没有找到任何记忆需要删除。"
            else:
                return f"没有找到包含「{target}」的记忆。"

        if target == "all":
            return f"好的，我已经清除了在这个群里关于你的所有记忆（共 {deleted_count} 条）。"
        else:
            return f"好的，我已经删除了包含「{target}」的记忆（共 {deleted_count} 条）。"

    def _get_mention_pattern(self, group_id: str, members_map: dict[str, str]) -> re.Pattern | None:
        """获取或创建 @ 提及匹配的正则表达式（带缓存）

        匹配 @名字，同时捕获前后可能的 Markdown 格式符号（如 **、*、__、_ 等）
        以便在转换时一并移除这些格式符号。
        """
        # 检查缓存
        if group_id in self._mention_pattern_cache:
            pattern, cache_time = self._mention_pattern_cache[group_id]
            if self._is_cache_valid(cache_time):
                return pattern

        if not members_map:
            return None

        # 构建正则模式
        sorted_names = sorted(members_map.keys(), key=len, reverse=True)
        if not sorted_names:
            return None

        escaped_names = [re.escape(name) for name in sorted_names]
        # 匹配模式：可选的前置Markdown符号 + 可选空白/换行 + @ + 名字 + 可选空白/换行 + 可选的后置Markdown符号
        # Markdown 符号：**、*、__、_、~~、`
        md_pattern = r"(?:\*{1,2}|_{1,2}|~~|`)?"
        ws_pattern = r"[\s]*"  # 可选的空白字符（包括换行）
        pattern = re.compile(
            md_pattern + ws_pattern +
            r"@(" + "|".join(escaped_names) + r")" +
            ws_pattern + md_pattern
        )

        self._mention_pattern_cache[group_id] = (pattern, time.time())
        return pattern

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在消息发送前处理，清洗消息格式并将文本中的 @名字 转换为飞书 At 组件"""
        if not self._is_lark_event(event):
            return

        result = event.get_result()
        if result is None or not result.chain:
            return

        # 如果是流式完成状态，跳过处理（已由流式卡片处理）
        if result.result_content_type == ResultContentType.STREAMING_FINISH:
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

        # 获取缓存的正则模式
        pattern = self._get_mention_pattern(group_id, members_map)
        if not pattern:
            return

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
