from __future__ import annotations

import json
import time
import uuid
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path

from astrbot.api import logger

from ..services.persistence import atomic_write_json


class MemoryPersistenceError(RuntimeError):
    """A requested mutation could not be committed to durable storage."""


class UserMemoryStore:
    """Group-scoped memory with copy-on-write commits and bounded read caching."""

    TYPE_PRIORITY = {"instruction": 0, "preference": 1, "fact": 2, "meme": 3}
    _CACHE_MAX_SIZE = 100

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir / "user_memory"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._cache: OrderedDict[str, dict] = OrderedDict()

    def _get_file_path(self, group_id: str) -> Path:
        safe_id = group_id.replace("/", "_").replace(chr(92), "_")
        return self._data_dir / f"{safe_id}.json"

    def _load_group_data(self, group_id: str) -> dict:
        if group_id in self._cache:
            self._cache.move_to_end(group_id)
            return self._cache[group_id]
        path = self._get_file_path(group_id)
        if path.exists():
            try:
                with path.open(encoding="utf-8") as stream:
                    data = json.load(stream)
                if not isinstance(data, dict) or not isinstance(
                    data.get("users"), dict
                ):
                    raise ValueError("invalid memory schema")
                self._set_cache(group_id, data)
                return data
            except (OSError, ValueError, TypeError) as exc:
                # Do not replace an unreadable/corrupt file with empty data.
                raise MemoryPersistenceError("无法读取群记忆文件") from exc
        data = {"group_id": group_id, "users": {}, "updated_at": time.time()}
        self._set_cache(group_id, data)
        return data

    def _set_cache(self, group_id: str, data: dict) -> None:
        self._cache.pop(group_id, None)
        while len(self._cache) >= self._CACHE_MAX_SIZE:
            self._cache.popitem(last=False)
        self._cache[group_id] = data

    def _save_group_data(self, group_id: str, data: dict | None = None) -> bool:
        candidate = deepcopy(data if data is not None else self._cache[group_id])
        candidate["updated_at"] = time.time()
        try:
            atomic_write_json(self._get_file_path(group_id), candidate)
        except (OSError, ValueError, TypeError) as exc:
            logger.error(f"[lark_enhance] Failed to save memory: {type(exc).__name__}")
            return False
        self._set_cache(group_id, candidate)
        logger.debug("[lark_enhance] Memory committed")
        return True

    def _add(
        self,
        group_id: str,
        user_id: str | None,
        memory_type: str,
        content: str,
        maximum: int,
    ) -> bool:
        if memory_type not in self.TYPE_PRIORITY or not isinstance(content, str):
            return False
        content = content.strip()
        if not content or maximum <= 0:
            return False
        try:
            data = deepcopy(self._load_group_data(group_id))
        except MemoryPersistenceError as exc:
            logger.error(f"[lark_enhance] {exc}")
            return False
        if user_id is None:
            memories = data.setdefault("group_memories", [])
        else:
            memories = data["users"].setdefault(user_id, {"memories": []})["memories"]
        # Only exact duplicates are safe to merge; a substring is another fact.
        for memory in memories:
            if memory["type"] == memory_type and memory["content"] == content:
                memory["updated_at"] = time.time()
                return self._save_group_data(group_id, data)
        now = time.time()
        memories.append(
            {
                "id": str(uuid.uuid4()),
                "type": memory_type,
                "content": content,
                "created_at": now,
                "updated_at": now,
            }
        )
        memories.sort(key=lambda memory: memory["updated_at"], reverse=True)
        del memories[maximum:]
        return self._save_group_data(group_id, data)

    def add_memory(
        self,
        group_id: str,
        user_id: str,
        memory_type: str,
        content: str,
        max_per_user: int = 20,
    ) -> bool:
        return self._add(group_id, user_id, memory_type, content, max_per_user)

    def add_group_memory(
        self,
        group_id: str,
        memory_type: str,
        content: str,
        max_per_group: int = 30,
    ) -> bool:
        return self._add(group_id, None, memory_type, content, max_per_group)

    def _select(
        self,
        memories: list[dict],
        limit: int,
        memory_type: str | None,
        memory_types: set[str] | None,
    ) -> list[dict]:
        selected = [
            memory
            for memory in memories
            if (memory_type is None or memory.get("type") == memory_type)
            and (memory_types is None or memory.get("type") in memory_types)
        ]
        selected.sort(
            key=lambda memory: (
                self.TYPE_PRIORITY.get(memory["type"], 99),
                -memory["updated_at"],
            )
        )
        return deepcopy(selected[: max(0, limit)])

    def get_memories(
        self,
        group_id: str,
        user_id: str,
        limit: int = 10,
        memory_type: str | None = None,
        memory_types: set[str] | None = None,
    ) -> list[dict]:
        data = self._load_group_data(group_id)
        memories = data["users"].get(user_id, {}).get("memories", [])
        return self._select(memories, limit, memory_type, memory_types)

    def get_group_memories(
        self,
        group_id: str,
        limit: int = 10,
        memory_type: str | None = None,
        memory_types: set[str] | None = None,
    ) -> list[dict]:
        return self._select(
            self._load_group_data(group_id).get("group_memories", []),
            limit,
            memory_type,
            memory_types,
        )

    def _delete(
        self,
        group_id: str,
        user_id: str | None,
        target: str,
        memory_type: str | None,
        memory_types: set[str] | None,
    ) -> int:
        data = deepcopy(self._load_group_data(group_id))
        if user_id is None:
            memories = data.get("group_memories", [])
        else:
            memories = data["users"].get(user_id, {}).get("memories", [])
        kept = [
            memory
            for memory in memories
            if not (
                (memory_type is None or memory.get("type") == memory_type)
                and (memory_types is None or memory.get("type") in memory_types)
                and (target == "all" or target.lower() in memory["content"].lower())
            )
        ]
        count = len(memories) - len(kept)
        if count:
            if user_id is None:
                data["group_memories"] = kept
            else:
                data["users"][user_id]["memories"] = kept
            if not self._save_group_data(group_id, data):
                raise MemoryPersistenceError("删除记忆未能保存，请稍后重试。")
        return count

    def delete_memories(
        self,
        group_id: str,
        user_id: str,
        target: str = "all",
        memory_type: str | None = None,
        memory_types: set[str] | None = None,
    ) -> int:
        return self._delete(group_id, user_id, target, memory_type, memory_types)

    def delete_group_memories(
        self,
        group_id: str,
        target: str = "all",
        memory_type: str | None = None,
        memory_types: set[str] | None = None,
    ) -> int:
        return self._delete(group_id, None, target, memory_type, memory_types)

    def format_memories_for_prompt(self, memories: list[dict]) -> str:
        labels = {
            "instruction": "指令",
            "preference": "偏好",
            "fact": "事实",
            "meme": "群梗",
        }
        return "\n".join(
            f"- {memory['content']}（{labels.get(memory['type'], memory['type'])}）"
            for memory in memories
        )
