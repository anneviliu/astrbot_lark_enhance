from __future__ import annotations

import json
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from astrbot.api import logger


class UserMemoryStore:
    """用户记忆存储管理器 - 按群隔离的用户记忆系统。"""

    TYPE_PRIORITY = {"instruction": 0, "preference": 1, "fact": 2, "meme": 3}
    _CACHE_MAX_SIZE = 100

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir / "user_memory"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._cache: OrderedDict[str, dict] = OrderedDict()

    def _get_file_path(self, group_id: str) -> Path:
        safe_id = group_id.replace("/", "_").replace("\\", "_")
        return self._data_dir / f"{safe_id}.json"

    def _load_group_data(self, group_id: str) -> dict:
        if group_id in self._cache:
            self._cache.move_to_end(group_id)
            return self._cache[group_id]

        file_path = self._get_file_path(group_id)
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._set_cache(group_id, data)
                    return data
            except Exception as e:
                logger.error(f"[lark_enhance] Failed to load memory for group {group_id}: {e}")

        data = {"group_id": group_id, "users": {}, "updated_at": time.time()}
        self._set_cache(group_id, data)
        return data

    def _set_cache(self, group_id: str, data: dict):
        if group_id in self._cache:
            del self._cache[group_id]

        while len(self._cache) >= self._CACHE_MAX_SIZE:
            self._cache.popitem(last=False)

        self._cache[group_id] = data

    def _save_group_data(self, group_id: str):
        if group_id not in self._cache:
            return

        try:
            data = self._cache[group_id]
            data["updated_at"] = time.time()
            file_path = self._get_file_path(group_id)

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            logger.debug(f"[lark_enhance] Saved memory for group {group_id}")
        except Exception as e:
            logger.error(f"[lark_enhance] Failed to save memory for group {group_id}: {e}")

    def add_memory(
        self,
        group_id: str,
        user_id: str,
        memory_type: str,
        content: str,
        max_per_user: int = 20,
    ) -> bool:
        if memory_type not in self.TYPE_PRIORITY:
            logger.warning(f"[lark_enhance] Invalid memory type: {memory_type}")
            return False

        data = self._load_group_data(group_id)

        if user_id not in data["users"]:
            data["users"][user_id] = {"memories": []}

        user_data = data["users"][user_id]
        memories = user_data["memories"]

        for mem in memories:
            if mem["type"] == memory_type:
                if content in mem["content"] or mem["content"] in content:
                    mem["content"] = content
                    mem["updated_at"] = time.time()
                    self._save_group_data(group_id)
                    logger.info(f"[lark_enhance] Updated memory for user {user_id}: {content[:30]}...")
                    return True

        new_memory = {
            "id": str(uuid.uuid4()),
            "type": memory_type,
            "content": content,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        memories.append(new_memory)

        if len(memories) > max_per_user:
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
        limit: int = 10,
        memory_type: str | None = None,
    ) -> list[dict]:
        data = self._load_group_data(group_id)

        if user_id not in data["users"]:
            return []

        memories = data["users"][user_id]["memories"]
        if memory_type:
            memories = [m for m in memories if m.get("type") == memory_type]

        sorted_memories = sorted(
            memories,
            key=lambda x: (self.TYPE_PRIORITY.get(x["type"], 99), -x["updated_at"]),
        )

        return sorted_memories[:limit]

    def delete_memories(
        self,
        group_id: str,
        user_id: str,
        target: str = "all",
        memory_type: str | None = None,
    ) -> int:
        data = self._load_group_data(group_id)

        if user_id not in data["users"]:
            return 0

        user_data = data["users"][user_id]
        memories = user_data["memories"]
        if memory_type:
            memories = [m for m in memories if m.get("type") == memory_type]
        original_count = len(memories)

        if target == "all":
            if memory_type:
                user_data["memories"] = [
                    mem for mem in user_data["memories"] if mem.get("type") != memory_type
                ]
            else:
                user_data["memories"] = []
            deleted_count = original_count
        else:
            target_lower = target.lower()
            kept = [mem for mem in memories if target_lower not in mem["content"].lower()]
            deleted_count = original_count - len(kept)
            if memory_type:
                others = [mem for mem in user_data["memories"] if mem.get("type") != memory_type]
                user_data["memories"] = others + kept
            else:
                user_data["memories"] = kept

        if deleted_count > 0:
            self._save_group_data(group_id)
            logger.info(f"[lark_enhance] Deleted {deleted_count} memories for user {user_id}")

        return deleted_count

    def add_group_memory(
        self,
        group_id: str,
        memory_type: str,
        content: str,
        max_per_group: int = 30,
    ) -> bool:
        if memory_type not in self.TYPE_PRIORITY:
            logger.warning(f"[lark_enhance] Invalid memory type: {memory_type}")
            return False

        data = self._load_group_data(group_id)

        if "group_memories" not in data:
            data["group_memories"] = []

        memories = data["group_memories"]

        for mem in memories:
            if mem["type"] == memory_type:
                if content in mem["content"] or mem["content"] in content:
                    mem["content"] = content
                    mem["updated_at"] = time.time()
                    self._save_group_data(group_id)
                    logger.info(
                        f"[lark_enhance] Updated group memory for {group_id}: {content[:30]}..."
                    )
                    return True

        new_memory = {
            "id": str(uuid.uuid4()),
            "type": memory_type,
            "content": content,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        memories.append(new_memory)

        if len(memories) > max_per_group:
            memories.sort(key=lambda x: x["updated_at"], reverse=True)
            removed = memories[max_per_group:]
            data["group_memories"] = memories[:max_per_group]
            logger.debug(f"[lark_enhance] Removed {len(removed)} old group memories for {group_id}")

        self._save_group_data(group_id)
        logger.info(f"[lark_enhance] Added group memory for {group_id}: {content[:30]}...")
        return True

    def get_group_memories(
        self,
        group_id: str,
        limit: int = 10,
        memory_type: str | None = None,
    ) -> list[dict]:
        data = self._load_group_data(group_id)

        if "group_memories" not in data:
            return []

        memories = data["group_memories"]
        if memory_type:
            memories = [m for m in memories if m.get("type") == memory_type]

        sorted_memories = sorted(
            memories,
            key=lambda x: (self.TYPE_PRIORITY.get(x["type"], 99), -x["updated_at"]),
        )

        return sorted_memories[:limit]

    def delete_group_memories(
        self,
        group_id: str,
        target: str = "all",
        memory_type: str | None = None,
    ) -> int:
        data = self._load_group_data(group_id)

        if "group_memories" not in data:
            return 0

        memories = data["group_memories"]
        if memory_type:
            memories = [m for m in memories if m.get("type") == memory_type]
        original_count = len(memories)

        if target == "all":
            if memory_type:
                data["group_memories"] = [
                    mem for mem in data["group_memories"] if mem.get("type") != memory_type
                ]
            else:
                data["group_memories"] = []
            deleted_count = original_count
        else:
            target_lower = target.lower()
            kept = [mem for mem in memories if target_lower not in mem["content"].lower()]
            deleted_count = original_count - len(kept)
            if memory_type:
                other = [mem for mem in data["group_memories"] if mem.get("type") != memory_type]
                data["group_memories"] = other + kept
            else:
                data["group_memories"] = kept

        if deleted_count > 0:
            self._save_group_data(group_id)
            logger.info(f"[lark_enhance] Deleted {deleted_count} group memories for {group_id}")

        return deleted_count

    def format_memories_for_prompt(self, memories: list[dict]) -> str:
        if not memories:
            return ""

        type_labels = {
            "instruction": "指令",
            "preference": "偏好",
            "fact": "事实",
            "meme": "群梗",
        }

        lines = []
        for mem in memories:
            type_label = type_labels.get(mem["type"], mem["type"])
            lines.append(f"- {mem['content']}（{type_label}）")

        return "\n".join(lines)
