from __future__ import annotations

import json
import re
import time
from collections import deque
from datetime import datetime

from astrbot.api import logger


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

    def _analyze_group_vibe(self, group_id: str, history_count: int = 12) -> tuple[str, str]:
        """基于近期群聊内容做轻量氛围识别。"""
        history_list = list(self.group_history.get(group_id, []))
        if not history_list:
            return "日常聊天", "语气自然、轻松一点，优先短句接话。"

        recent = history_list[-history_count:]
        text_blob = "\n".join(item.get("content", "") for item in recent).lower()

        playful_score = len(re.findall(r"(哈哈|笑死|233|666|草|狗头|lol|hh|😂|🤣|😆)", text_blob))
        help_score = len(re.findall(r"(怎么|如何|帮|求助|报错|出错|不会|咋办|解决)", text_blob))
        debate_score = len(re.findall(r"(不对|但是|不过|其实|我觉得|离谱|争议|你这)", text_blob))

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
                logger.info(
                    f"[lark_enhance] Captured group meme for {group_id} "
                    f"by {sender_name}: {meme_content[:50]}..."
                )
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
        now = time.time()

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
        """强制保存待保存的历史记录。"""
        if self._pending_save:
            self._save_history(force=True)

    def _ensure_history_deque(self, group_id: str, history_count: int):
        """确保 deque 长度符合配置。"""
        if self.group_history[group_id].maxlen != history_count:
            old_data = list(self.group_history[group_id])
            self.group_history[group_id] = deque(old_data, maxlen=history_count)
            self._history_maxlen = history_count

    def _clear_history_for_session(self, unified_msg_origin: str):
        """清空指定会话的历史记录。"""
        parts = unified_msg_origin.split(":")
        if len(parts) >= 3 and parts[0] == "lark":
            target_id = parts[2]
            if target_id in self.group_history:
                self.group_history[target_id].clear()
                self._save_history(force=True)
                logger.info(
                    f"[lark_enhance] Cleared history for session: {unified_msg_origin}"
                )
