from __future__ import annotations

import inspect
from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import Plain

from ..services.streaming_card import LarkStreamingCard, empty_generator


_original_lark_send_streaming = None
_streaming_config: dict | None = None
_clean_content_func = None


def configure_streaming_runtime(config: dict, clean_content_func):
    """设置流式补丁需要的全局引用。"""
    global _streaming_config, _clean_content_func
    _streaming_config = config
    _clean_content_func = clean_content_func


class StreamingMixin:
    """流式卡片 monkey patch 能力。"""

    def _setup_streaming_patch(self):
        """设置流式卡片的 monkey patch。"""
        global _original_lark_send_streaming

        try:
            from astrbot.core.platform.astr_message_event import AstrMessageEvent as BaseEvent
            from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

            if not hasattr(LarkMessageEvent, "send_streaming"):
                logger.warning(
                    "[lark_enhance] LarkMessageEvent 没有 send_streaming 方法，"
                    "可能框架版本不兼容，跳过流式卡片补丁"
                )
                return

            sig = inspect.signature(LarkMessageEvent.send_streaming)
            params = list(sig.parameters.keys())
            if "generator" not in params:
                logger.warning(
                    "[lark_enhance] send_streaming 方法签名不符合预期，"
                    "可能框架版本不兼容，跳过流式卡片补丁"
                )
                return

            if _original_lark_send_streaming is not None:
                logger.debug("[lark_enhance] Streaming patch already applied")
                return

            _original_lark_send_streaming = LarkMessageEvent.send_streaming

            async def patched_send_streaming(event_self, generator, use_fallback: bool = False):
                if not (_streaming_config or {}).get("enable_streaming_card", False):
                    return await _original_lark_send_streaming(event_self, generator, use_fallback)

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

                if not await streaming_card.create_initial_card():
                    logger.warning("[lark_enhance] Failed to create streaming card, using fallback")
                    return await _original_lark_send_streaming(event_self, generator, use_fallback)

                full_content = ""
                try:
                    async for chain in generator:
                        if chain and chain.chain:
                            for comp in chain.chain:
                                if isinstance(comp, Plain):
                                    full_content += comp.text
                                elif hasattr(comp, "type"):
                                    full_content += f" [{comp.type}] "
                            await streaming_card.update_card(full_content)

                    if _clean_content_func:
                        full_content = _clean_content_func(full_content)

                    if not full_content.strip():
                        await streaming_card.delete_card()
                        logger.info("[lark_enhance] Deleted empty streaming card (no text content)")
                    else:
                        await streaming_card.finalize_card(full_content)
                        logger.info(
                            f"[lark_enhance] Streaming card completed, length: {len(full_content)}"
                        )

                    await BaseEvent.send_streaming(event_self, empty_generator(), use_fallback)

                except Exception as e:
                    logger.error(f"[lark_enhance] Streaming card error: {e}")
                    if full_content:
                        await streaming_card.finalize_card(full_content + "\n\n*（输出中断）*")
                    else:
                        await streaming_card.delete_card()

            LarkMessageEvent.send_streaming = patched_send_streaming
            logger.info("[lark_enhance] Streaming card patch applied successfully")

        except ImportError as e:
            logger.warning(f"[lark_enhance] Failed to import LarkMessageEvent: {e}")
        except Exception as e:
            logger.error(f"[lark_enhance] Failed to setup streaming patch: {e}")
