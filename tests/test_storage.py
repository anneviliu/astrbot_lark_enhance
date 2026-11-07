import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import Plugin, astrbot_stubs

with astrbot_stubs():
    from lark_enhance.services import persistence
    from lark_enhance.stores.user_memory_store import (
        MemoryPersistenceError,
        UserMemoryStore,
    )


class StorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = UserMemoryStore(self.directory)
        self.plugin = Plugin(self.directory)
        self.addCleanup(self.plugin._cancel_history_save)

    def test_interrupted_write_preserves_previous_file_and_removes_tempfile(self):
        path = self.directory / "state.json"
        persistence.atomic_write_json(path, {"original": 1})

        def partial(data, stream, **kwargs):
            stream.write("{")
            raise OSError("disk full")

        with patch.object(persistence.json, "dump", side_effect=partial):
            with self.assertRaises(OSError):
                persistence.atomic_write_json(path, {"new": 2})
        self.assertEqual(json.loads(path.read_text()), {"original": 1})
        self.assertEqual(list(self.directory.glob(".*.tmp")), [])

    def test_failed_atomic_replace_preserves_previous_file(self):
        path = self.directory / "state.json"
        persistence.atomic_write_json(path, {"original": 1})
        with patch.object(persistence.os, "replace", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                persistence.atomic_write_json(path, {"new": 2})
        self.assertEqual(json.loads(path.read_text()), {"original": 1})

    def test_add_failure_rolls_back_cache_and_survives_restart(self):
        self.assertTrue(self.store.add_memory("oc_test", "ou_a", "fact", "原记忆"))
        with patch.object(persistence.os, "replace", side_effect=OSError("disk full")):
            self.assertFalse(self.store.add_memory("oc_test", "ou_a", "fact", "新记忆"))
            self.assertFalse(self.store.add_group_memory("oc_test", "fact", "群记忆"))
        for store in (self.store, UserMemoryStore(self.directory)):
            self.assertEqual(
                [m["content"] for m in store.get_memories("oc_test", "ou_a")],
                ["原记忆"],
            )
            self.assertEqual(store.get_group_memories("oc_test"), [])

    def test_delete_failure_preserves_memory_and_propagates_error(self):
        self.store.add_memory("oc_test", "ou_a", "fact", "原记忆")
        self.store.add_group_memory("oc_test", "fact", "群记忆")
        with patch.object(persistence.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(MemoryPersistenceError):
                self.store.delete_memories("oc_test", "ou_a")
            with self.assertRaises(MemoryPersistenceError):
                self.store.delete_group_memories("oc_test")
        self.assertEqual(len(self.store.get_memories("oc_test", "ou_a")), 1)
        self.assertEqual(len(self.store.get_group_memories("oc_test")), 1)

    def test_corrupt_memory_is_not_overwritten_by_a_new_fact(self):
        path = self.store._get_file_path("oc_test")
        path.write_text("{broken")
        self.assertFalse(self.store.add_memory("oc_test", "ou_a", "fact", "新事实"))
        self.assertEqual(path.read_text(), "{broken")
        with self.assertRaises(MemoryPersistenceError):
            self.store.get_memories("oc_test", "ou_a")

    def test_substring_fact_is_kept_and_exact_duplicate_is_merged(self):
        self.store.add_memory("oc_test", "ou_a", "fact", "负责甲项目和乙项目")
        self.store.add_memory("oc_test", "ou_a", "fact", "负责甲项目")
        self.store.add_memory("oc_test", "ou_a", "fact", "负责甲项目")
        self.assertEqual(len(self.store.get_memories("oc_test", "ou_a")), 2)
        self.store.add_group_memory("oc_test", "fact", "甲乙项目")
        self.store.add_group_memory("oc_test", "fact", "甲乙")
        self.assertEqual(len(self.store.get_group_memories("oc_test")), 2)

    def test_empty_content_and_zero_capacity_are_rejected(self):
        for content in ("", "   "):
            self.assertFalse(self.store.add_memory("oc_test", "ou_a", "fact", content))
        self.assertFalse(
            self.store.add_group_memory("oc_test", "fact", "事实", max_per_group=0)
        )

    def test_type_filtered_delete_keeps_other_types(self):
        self.store.add_group_memory("oc_test", "meme", "群梗")
        self.store.add_group_memory("oc_test", "fact", "事实")
        self.assertEqual(
            self.store.delete_group_memories("oc_test", memory_types={"meme"}), 1
        )
        self.assertEqual(self.store.get_group_memories("oc_test")[0]["type"], "fact")

    async def test_history_debounce_flushes_without_another_message(self):
        self.plugin._SAVE_DEBOUNCE = 0.02
        self.plugin.group_history["oc_test"].append({"content": "第一条"})
        self.plugin._save_history()
        self.plugin.group_history["oc_test"].append({"content": "第二条"})
        self.plugin._save_history()
        self.assertTrue(self.plugin._pending_save)
        await asyncio.sleep(0.06)
        self.assertEqual(
            len(json.loads(self.plugin._history_file.read_text())["oc_test"]), 2
        )
        self.assertFalse(self.plugin._pending_save)
        self.assertIsNone(self.plugin._history_save_handle)

    def test_history_write_failure_preserves_file_and_pending_state(self):
        self.plugin.group_history["oc_test"].append({"content": "第一条"})
        self.plugin._save_history(force=True)
        self.plugin.group_history["oc_test"].append({"content": "第二条"})
        with patch.object(persistence.os, "replace", side_effect=OSError("disk full")):
            self.assertFalse(self.plugin._save_history(force=True))
        self.assertTrue(self.plugin._pending_save)
        self.assertEqual(
            len(json.loads(self.plugin._history_file.read_text())["oc_test"]), 1
        )
        self.assertTrue(self.plugin._flush_pending_save())
        self.assertEqual(
            len(json.loads(self.plugin._history_file.read_text())["oc_test"]), 2
        )

    def test_closed_instance_exit_callback_cannot_overwrite_new_file(self):
        self.plugin.group_history["oc_test"].append({"content": "旧实例"})
        self.plugin._closed = True
        self.plugin._history_file.write_text('{"oc_test": [{"content": "新实例"}]}')
        self.plugin._atexit_save()
        self.assertEqual(
            json.loads(self.plugin._history_file.read_text())["oc_test"][0]["content"],
            "新实例",
        )
