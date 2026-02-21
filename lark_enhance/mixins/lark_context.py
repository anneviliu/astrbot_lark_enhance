from __future__ import annotations

import json
import time
import uuid
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from lark_oapi.api.contact.v3 import GetUserRequest
from lark_oapi.api.im.v1 import (
    GetChatMembersRequest,
    GetChatRequest,
    GetMessageRequest,
    GetMessageResourceRequest,
)


class LarkContextMixin:
    """飞书上下文信息查询与解析能力。"""

    @staticmethod
    def _is_lark_event(event: AstrMessageEvent) -> bool:
        return event.get_platform_name() == "lark"

    @staticmethod
    def _get_lark_client(event: AstrMessageEvent) -> Any | None:
        return getattr(event, "bot", None)

    def _is_cache_valid(self, cache_time: float) -> bool:
        """检查缓存是否有效。"""
        return time.time() - cache_time < self._CACHE_TTL

    def _get_user_from_cache(self, open_id: str) -> str | None:
        """从缓存获取用户昵称（带 TTL 检查）。"""
        if open_id in self.user_cache:
            nickname, cache_time = self.user_cache[open_id]
            if self._is_cache_valid(cache_time):
                self.user_cache.move_to_end(open_id)
                return nickname
            del self.user_cache[open_id]
        return None

    def _set_user_cache(self, open_id: str, nickname: str):
        """设置用户缓存（带容量限制）。"""
        if open_id in self.user_cache:
            del self.user_cache[open_id]

        while len(self.user_cache) >= self._USER_CACHE_MAX_SIZE:
            self.user_cache.popitem(last=False)

        self.user_cache[open_id] = (nickname, time.time())

    async def _get_user_nickname(
        self,
        lark_client: Any,
        open_id: str,
        event: AstrMessageEvent | None = None,
    ) -> str | None:
        cached = self._get_user_from_cache(open_id)
        if cached is not None:
            return cached

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
            if response.code == 41050:
                logger.debug(
                    f"获取飞书用户信息失败 (权限不足): {response.msg}。可能是机器人ID或外部联系人。"
                )
                placeholder = f"用户({open_id[-4:]})"
                self._set_user_cache(open_id, placeholder)
                return placeholder
            logger.warning(f"获取飞书用户信息失败: {response.code} - {response.msg}")
        except Exception as e:
            logger.error(f"获取飞书用户信息异常: {e}")

        return None

    async def _get_group_info(self, lark_client: Any, chat_id: str) -> dict | None:
        """获取群组信息（名称和描述）。"""
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
            logger.warning(f"获取飞书群组信息失败: {response.code} - {response.msg}")
        except Exception as e:
            logger.error(f"获取飞书群组信息异常: {e}")

        return None

    async def _get_group_members(self, lark_client: Any, chat_id: str) -> dict[str, str]:
        """获取群成员列表，返回 nickname -> open_id 的映射。"""
        if chat_id in self.group_members_cache:
            if self._is_cache_valid(self._group_members_cache_time.get(chat_id, 0)):
                return self.group_members_cache[chat_id]

        logger.debug(f"[lark_enhance] Querying Lark group members for chat_id: {chat_id}")
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
                    logger.warning(f"获取飞书群成员失败: {response.code} - {response.msg}")
                    break

                if response.data and response.data.items:
                    for member in response.data.items:
                        member_id = getattr(member, "member_id", None)
                        name = getattr(member, "name", None)
                        if member_id and name:
                            members_map[name] = member_id
                            self._set_user_cache(member_id, name)

                if response.data and response.data.has_more and response.data.page_token:
                    page_token = response.data.page_token
                else:
                    break

            self.group_members_cache[chat_id] = members_map
            self._group_members_cache_time[chat_id] = time.time()
            self._mention_pattern_cache.pop(chat_id, None)
            logger.info(
                f"[lark_enhance] Loaded {len(members_map)} members for group {chat_id}"
            )

        except Exception as e:
            logger.error(f"获取飞书群成员异常: {e}")

        return members_map

    def _extract_image_keys_from_content_json(self, content_json: Any) -> list[str]:
        """从飞书消息 JSON 中提取 image_key 列表。"""
        image_keys: list[str] = []

        if isinstance(content_json, dict):
            image_key = content_json.get("image_key")
            if isinstance(image_key, str) and image_key:
                image_keys.append(image_key)

            content = content_json.get("content")
            if isinstance(content, list):
                for line in content:
                    if not isinstance(line, list):
                        continue
                    for segment in line:
                        if not isinstance(segment, dict):
                            continue
                        if segment.get("tag") == "img":
                            key = segment.get("image_key")
                            if isinstance(key, str) and key:
                                image_keys.append(key)

        seen = set()
        deduped: list[str] = []
        for key in image_keys:
            if key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        return deduped

    async def _download_quoted_images(
        self,
        lark_client: Any,
        message_id: str,
        image_keys: list[str],
    ) -> list[str]:
        """下载引用消息中的图片并返回本地文件路径。"""
        file_paths: list[str] = []
        if not image_keys:
            return file_paths

        im = getattr(lark_client, "im", None)
        if im is None or im.v1 is None or im.v1.message_resource is None:
            logger.warning("[lark_enhance] lark_client.im.message_resource 未初始化，无法下载引用图片")
            return file_paths

        quoted_dir = self._data_dir / "quoted_images"
        quoted_dir.mkdir(parents=True, exist_ok=True)

        for idx, image_key in enumerate(image_keys):
            try:
                request = (
                    GetMessageResourceRequest.builder()
                    .message_id(message_id)
                    .file_key(image_key)
                    .type("image")
                    .build()
                )
                response = await im.v1.message_resource.aget(request)
                if not response.success() or response.file is None:
                    logger.warning(
                        f"[lark_enhance] Failed to download quoted image: "
                        f"message_id={message_id}, image_key={image_key}, "
                        f"code={getattr(response, 'code', 'unknown')}, msg={getattr(response, 'msg', 'unknown')}"
                    )
                    continue

                image_bytes = response.file.read()
                if not image_bytes:
                    continue

                file_path = quoted_dir / f"{message_id}_{idx}_{uuid.uuid4().hex[:8]}.jpg"
                file_path.write_bytes(image_bytes)
                file_paths.append(str(file_path))
            except Exception as e:
                logger.error(
                    f"[lark_enhance] Download quoted image failed: "
                    f"message_id={message_id}, image_key={image_key}, err={e}"
                )

        return file_paths

    async def _get_message_content(
        self,
        lark_client: Any,
        message_id: str,
    ) -> tuple[str | None, str | None, list[str]] | None:
        """获取消息内容和发送者信息。"""
        try:
            request = GetMessageRequest.builder().message_id(message_id).build()

            im = getattr(lark_client, "im", None)
            if im is None or im.v1 is None or im.v1.message is None:
                logger.warning("[lark_enhance] lark_client.im 未初始化，无法获取引用消息")
                return None

            response = await im.v1.message.aget(request)

            if response.success() and response.data and response.data.items:
                msg_item = response.data.items[0]

                sender_name = None
                sender = getattr(msg_item, "sender", None)
                if sender:
                    sender_id_obj = getattr(sender, "sender_id", None)
                    if sender_id_obj:
                        sender_open_id = getattr(sender_id_obj, "open_id", None)
                        if sender_open_id:
                            sender_name = await self._get_user_nickname(
                                lark_client,
                                sender_open_id,
                            )

                mentions = getattr(msg_item, "mentions", None)

                body = getattr(msg_item, "body", None)
                content, image_keys = await self._parse_message_body(lark_client, body, mentions)
                quoted_images = await self._download_quoted_images(
                    lark_client=lark_client,
                    message_id=message_id,
                    image_keys=image_keys,
                )

                if not content and quoted_images:
                    content = "（引用消息包含图片）"

                return content, sender_name, quoted_images

            logger.warning(f"获取飞书消息内容失败: {response.code} - {response.msg}")
        except Exception as e:
            logger.error(f"获取飞书消息内容异常: {e}")

        return None

    async def _parse_message_body(
        self,
        lark_client: Any,
        body: Any,
        mentions: Any = None,
    ) -> tuple[str | None, list[str]]:
        """解析 Lark 消息体 content。"""
        content = getattr(body, "content", None)
        if not content:
            return None, []

        try:
            content_json = json.loads(content)
            image_keys = self._extract_image_keys_from_content_json(content_json)

            if "text" in content_json:
                text_content = content_json["text"]
                if mentions:
                    for mention in mentions:
                        key = getattr(mention, "key", None)
                        name = getattr(mention, "name", None)
                        if key and name:
                            text_content = text_content.replace(key, f"@{name}")
                return self._clean_content(text_content), image_keys

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
                                real_name = await self._get_user_nickname(lark_client, user_id)
                                texts.append(f"@{real_name or user_id}")
                            else:
                                texts.append("@未知用户")
                return "".join(texts), image_keys

            return content, image_keys
        except Exception:
            return content, []
