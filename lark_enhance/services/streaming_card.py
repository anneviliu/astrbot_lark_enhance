from __future__ import annotations

import json
import time
import uuid
from typing import Any

from astrbot.api import logger

from lark_oapi.api.im.v1 import (
    DeleteMessageRequest,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)


async def empty_generator():
    """空的异步生成器，用于调用父类方法。"""
    return
    yield  # 让它成为异步生成器


class LarkCardBuilder:
    """飞书消息卡片构建器 - 提供优雅的链式 API 构建卡片。"""

    def __init__(self):
        self._elements: list[dict] = []
        self._config = {"wide_screen_mode": True}

    def markdown(self, content: str) -> "LarkCardBuilder":
        """添加 Markdown 元素。"""
        self._elements.append({"tag": "markdown", "content": content})
        return self

    def divider(self) -> "LarkCardBuilder":
        """添加分割线。"""
        self._elements.append({"tag": "hr"})
        return self

    def loading_indicator(self, text: str = "正在输入...") -> "LarkCardBuilder":
        """添加加载指示器。"""
        self._elements.append({"tag": "markdown", "content": f"◉ *{text}*"})
        return self

    def thinking_indicator(self) -> "LarkCardBuilder":
        """添加思考中指示器。"""
        return self.loading_indicator("思考中...")

    def build(self) -> str:
        """构建并返回卡片 JSON 字符串。"""
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
        """快速创建流式输出卡片。"""
        builder = cls()
        if text:
            builder.markdown(text)
        if not is_finished:
            builder.loading_indicator()
        return builder.build()


class LarkStreamingCard:
    """飞书流式卡片处理器，用于实现打字机效果。"""

    UPDATE_INTERVAL = 0.3
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
        """创建初始卡片消息。"""
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

            logger.error(
                f"[lark_enhance] Failed to create card: {response.code} - {response.msg}"
            )
            return False
        except Exception as e:
            logger.error(f"[lark_enhance] Create card exception: {e}")
            return False

    async def update_card(self, text: str, force: bool = False) -> bool:
        """更新卡片内容。"""
        if not self.card_message_id:
            return False

        self._content_buffer = text
        now = time.time()

        if not force:
            time_elapsed = now - self._last_update_time
            chars_added = len(text) - self._last_update_length
            if time_elapsed < self.UPDATE_INTERVAL and chars_added < self.MIN_UPDATE_CHARS:
                return True

        try:
            content = LarkCardBuilder.streaming_card(text, is_finished=False)

            request = (
                PatchMessageRequest.builder()
                .message_id(self.card_message_id)
                .request_body(PatchMessageRequestBody.builder().content(content).build())
                .build()
            )

            response = await self.lark_client.im.v1.message.apatch(request)

            if response.success():
                self._last_update_time = now
                self._last_update_length = len(text)
                return True

            logger.warning(
                f"[lark_enhance] Failed to update card: {response.code} - {response.msg}"
            )
            return False
        except Exception as e:
            logger.error(f"[lark_enhance] Update card exception: {e}")
            return False

    async def finalize_card(self, text: str) -> bool:
        """完成卡片，移除加载指示器。"""
        if not self.card_message_id:
            return False

        try:
            content = LarkCardBuilder.streaming_card(text, is_finished=True)

            request = (
                PatchMessageRequest.builder()
                .message_id(self.card_message_id)
                .request_body(PatchMessageRequestBody.builder().content(content).build())
                .build()
            )

            response = await self.lark_client.im.v1.message.apatch(request)

            if response.success():
                logger.debug("[lark_enhance] Finalized streaming card")
                return True

            logger.warning(
                f"[lark_enhance] Failed to finalize card: {response.code} - {response.msg}"
            )
            return False
        except Exception as e:
            logger.error(f"[lark_enhance] Finalize card exception: {e}")
            return False

    async def delete_card(self) -> bool:
        """删除卡片消息（用于内容为空时清理）。"""
        if not self.card_message_id:
            return False

        try:
            request = (
                DeleteMessageRequest.builder().message_id(self.card_message_id).build()
            )

            response = await self.lark_client.im.v1.message.adelete(request)

            if response.success():
                logger.debug("[lark_enhance] Deleted empty streaming card")
                self.card_message_id = None
                return True

            logger.warning(
                f"[lark_enhance] Failed to delete card: {response.code} - {response.msg}"
            )
            return False
        except Exception as e:
            logger.error(f"[lark_enhance] Delete card exception: {e}")
            return False
