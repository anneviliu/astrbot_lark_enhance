from __future__ import annotations

import json
import re
import time
from typing import Any


class TextMixin:
    """文本清洗与 mention 文本处理能力。"""

    _MENTION_MARKDOWN_PATTERNS = [
        re.compile(r"\*\*\s*(@[^\s\*]+)\s*\*\*"),
        re.compile(r"(?<!\*)\*\s*(@[^\s\*]+)\s*\*(?!\*)"),
        re.compile(r"__\s*(@[^\s_]+)\s*__"),
        re.compile(r"(?<!_)_\s*(@[^\s_]+)\s*_(?!_)"),
        re.compile(r"~~\s*(@[^\s~]+)\s*~~"),
        re.compile(r"`\s*(@[^\s`]+)\s*`"),
    ]

    def _clean_content(self, content_str: str) -> str:
        """清洗消息内容，仅处理 AstrBot 序列化的消息组件格式。"""
        if not content_str:
            return content_str

        content_str = content_str.strip()

        if len(content_str) > self._CLEAN_CONTENT_MAX_LEN:
            return content_str

        if not content_str.startswith("["):
            return content_str

        try:
            data = json.loads(content_str)
        except json.JSONDecodeError:
            return content_str

        if not self._is_astrbot_message_format(data):
            return content_str

        result = self._extract_text_from_data(data, depth=0)
        return result if result else content_str

    def _is_astrbot_message_format(self, data: Any) -> bool:
        """检测数据是否为 AstrBot 消息组件格式。"""
        if not isinstance(data, list):
            return False

        if not data:
            return False

        valid_types = {"text", "image", "at", "plain", "face", "record", "video", "file"}
        for item in data:
            if isinstance(item, dict) and "type" in item:
                type_value = item.get("type", "").lower()
                if type_value in valid_types:
                    return True

        return False

    def _extract_text_from_data(self, data: Any, depth: int = 0) -> str:
        """递归从 list/dict 中提取 text 字段。"""
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

    def _clean_mention_markdown(self, text: str) -> str:
        """清理 @ 提及周围的 Markdown 格式符号。"""
        result = text
        for pattern in self._MENTION_MARKDOWN_PATTERNS:
            result = pattern.sub(r"\1", result)
        return result

    def _get_mention_pattern(self, group_id: str, members_map: dict[str, str]) -> re.Pattern | None:
        """获取或创建 @ 提及匹配的正则表达式（带缓存）。"""
        if group_id in self._mention_pattern_cache:
            pattern, cache_time = self._mention_pattern_cache[group_id]
            if self._is_cache_valid(cache_time):
                return pattern

        if not members_map:
            return None

        sorted_names = sorted(members_map.keys(), key=len, reverse=True)
        if not sorted_names:
            return None

        escaped_names = [re.escape(name) for name in sorted_names]
        pattern = re.compile(r"@(" + "|".join(escaped_names) + r")")

        self._mention_pattern_cache[group_id] = (pattern, time.time())
        return pattern
