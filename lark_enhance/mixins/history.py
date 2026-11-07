from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import deque
from datetime import datetime

from astrbot.api import logger

from ..services.persistence import atomic_write_json


class HistoryMixin:
    """群历史与氛围分析相关逻辑。"""

    @staticmethod
    def _format_history_time(timestamp: float | str | None = None) -> str:
        """使用消息时间（秒或毫秒），按 AstrBot 所在系统时区记录完整日期。"""
        if timestamp is not None:
            try:
                value = float(timestamp)
                if value >= 100_000_000_000:
                    value /= 1000
                if value > 0:
                    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, OverflowError, OSError):
                pass
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _atexit_save(self):
        """程序退出时保存历史记录。"""
        if getattr(self, "_closed", False):
            return
        if self._pending_save or self.group_history:
            self._save_history(force=True)

    def _load_history(self):
        """从文件加载历史记录。"""
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

    def _analyze_group_vibe(
        self, group_id: str, history_count: int = 12
    ) -> tuple[str, str]:
        """基于近期群聊内容做轻量氛围识别。"""
        history_list = list(self.group_history.get(group_id, []))
        if not history_list:
            return "日常聊天", "语气自然、轻松一点，优先短句接话。"

        recent = history_list[-history_count:]
        text_blob = "\n".join(item.get("content", "") for item in recent).lower()

        playful_score = len(
            re.findall(r"(哈哈|笑死|233|666|草|狗头|lol|hh|😂|🤣|😆)", text_blob)
        )
        help_score = len(
            re.findall(r"(怎么|如何|帮|求助|报错|出错|不会|咋办|解决)", text_blob)
        )
        debate_score = len(
            re.findall(r"(不对|但是|不过|其实|我觉得|离谱|争议|你这)", text_blob)
        )

        if playful_score >= max(help_score, debate_score) and playful_score >= 2:
            return "欢乐整活", "可以先接梗再回答，语气活泼，避免一本正经。"
        if help_score >= max(playful_score, debate_score) and help_score >= 2:
            return "轻求助模式", "先同理再给步骤，少说教，给可执行建议。"
        if debate_score >= max(playful_score, help_score) and debate_score >= 2:
            return "观点碰撞", "先复述对方观点再表达看法，避免攻击性语气。"
        return "日常聊天", "像群友一样自然回复，保持口语感和互动感。"

    def _try_capture_group_meme(self, group_id: str, sender_name: str, content: str):
        """从群消息中自动捕获明确声明的群梗。"""
        if not self.config.get("enable_meme_memory", True):
            return

        if not group_id or not content:
            return

        text = content.strip()
        if not text or text.startswith("/"):
            return

        for pattern in self._MEME_CAPTURE_PATTERNS:
            match = pattern.match(text)
            if not match:
                continue

            meme_content = (match.group(1) or "").strip()
            if not meme_content or len(meme_content) > 120:
                return

            max_memes = self.config.get("memory_max_per_group", 30)
            saved = self._memory_store.add_group_memory(
                group_id=group_id,
                memory_type="meme",
                content=meme_content,
                max_per_group=max_memes,
            )
            if saved:
                logger.info("[lark_enhance] Captured group meme")
            return

    def _format_history_sender(self, item: dict) -> str:
        """格式化历史记录中的发送者标识：昵称(open_id后4位)。"""
        sender_name = item.get("sender", "未知用户")
        sender_id = (item.get("sender_id") or "").strip()
        if not sender_id:
            return sender_name
        tail = sender_id[-4:] if len(sender_id) > 4 else sender_id
        return f"{sender_name}({tail})"

    def _save_history(self, force: bool = False):
        """将历史记录保存到文件（带防抖机制）。"""
        if getattr(self, "_closed", False):
            return False
        now = time.monotonic()
        self._pending_save = True

        if not force and now - self._last_save_time < self._SAVE_DEBOUNCE:
            self._schedule_history_save(
                self._SAVE_DEBOUNCE - (now - self._last_save_time)
            )
            return False

        self._cancel_history_save()
        try:
            data = {
                group_id: list(items)
                for group_id, items in self.group_history.items()
                if items
            }

            atomic_write_json(self._history_file, data)

            self._last_save_time = now
            self._pending_save = False
            logger.debug(f"[lark_enhance] Saved history for {len(data)} groups")
            return True
        except Exception as e:
            logger.error(f"[lark_enhance] Failed to save history: {e}")
            self._schedule_history_save(self._SAVE_DEBOUNCE)
            return False

    def _schedule_history_save(self, delay: float):
        handle = getattr(self, "_history_save_handle", None)
        if handle is not None and not handle.cancelled():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # atexit / synchronous callers still have the force-save path
        self._history_save_handle = loop.call_later(delay, self._delayed_history_save)

    def _delayed_history_save(self):
        self._history_save_handle = None
        if self._pending_save and not getattr(self, "_closed", False):
            self._save_history(force=True)

    def _cancel_history_save(self):
        handle = getattr(self, "_history_save_handle", None)
        if handle is not None:
            handle.cancel()
        self._history_save_handle = None

    def _flush_pending_save(self):
        """强制保存待保存的历史记录。"""
        if self._pending_save:
            return self._save_history(force=True)
        return True

    def _ensure_history_deque(self, group_id: str, history_count: int):
        """确保 deque 长度符合配置。"""
        if self.group_history[group_id].maxlen != history_count:
            old_data = list(self.group_history[group_id])
            self.group_history[group_id] = deque(old_data, maxlen=history_count)
            self._history_maxlen = history_count

    def _clear_history_for_session(self, unified_msg_origin: str):
        """清空指定会话的历史记录。"""
        parts = unified_msg_origin.split(":", 2)
        if len(parts) == 3:
            self._clear_history_for_group(parts[2])

    def _clear_history_for_group(self, group_id: str):
        if group_id in self.group_history:
            self.group_history[group_id].clear()
            return self._save_history(force=True)
        return True

    def _record_sent_text(self, event, content_str: str):
        """Shared by the normal send hook and the per-event stream observer."""
        if getattr(self, "_closed", False):
            return

        group_id = event.message_obj.group_id
        if not group_id:
            return

        history_count = self.config.get("history_inject_count", 20)
        if not history_count or history_count <= 0:
            return

        try:
            self._ensure_history_deque(group_id, history_count)

            time_str = self._format_history_time()
            sender_name = self.config.get("bot_name", "助手")

            if not content_str:
                return

            content_str = self._clean_content(content_str)
            if not content_str:
                return

            msg_id = f"sent_{uuid.uuid4().hex}"
            record_item = {
                "msg_id": msg_id,
                "time": time_str,
                "sender": sender_name,
                "sender_id": "__bot__",
                "content": content_str,
            }

            self.group_history[group_id].append(record_item)
            self._save_history()
            logger.debug(
                f"[lark_enhance] Recorded bot reply ({len(content_str)} characters)"
            )
        except Exception as e:
            logger.error(f"[lark_enhance] Failed to record self message history: {e}")
