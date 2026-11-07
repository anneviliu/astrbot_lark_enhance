import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.support import Event, Plain, Plugin, astrbot_stubs, client

with astrbot_stubs():
    from lark_enhance.handlers.events import (
        handle_on_llm_request,
        handle_on_message,
        handle_on_message_sent,
    )


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plugin = Plugin(Path(self.temp.name))

    async def test_received_history_uses_message_date_and_member_name(self):
        event = Event()
        event.bot = client()
        await handle_on_message(self.plugin, event)
        saved = json.loads(self.plugin._history_file.read_text())["oc_test"][0]
        expected = datetime.datetime.fromtimestamp(event.message_obj.timestamp)
        self.assertEqual(saved["time"], expected.strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(saved["sender"], "张三")
        self.assertEqual(saved["sender_id"], event.get_sender_id())
        self.assertEqual(saved["content"], event.message_str)

    async def test_sent_history_uses_current_date_and_bot_name(self):
        event = Event(timestamp=946684800)  # The reply must not use the incoming date.
        event.result = SimpleNamespace(chain=[Plain("收到")])
        before = datetime.datetime.now()
        await handle_on_message_sent(self.plugin, event)
        after = datetime.datetime.now()
        saved = json.loads(self.plugin._history_file.read_text())["oc_test"][0]
        recorded = datetime.datetime.strptime(saved["time"], "%Y-%m-%d %H:%M:%S")
        self.assertLessEqual(before.replace(microsecond=0), recorded)
        self.assertLessEqual(recorded, after)
        self.assertEqual(saved["sender"], "测试助手")
        self.assertEqual(saved["sender_id"], "__bot__")

    async def test_real_name_disabled_does_not_request_names(self):
        self.plugin.config["enable_real_name"] = False
        event = Event(nickname="原昵称")
        event.bot = client()
        await handle_on_message(self.plugin, event)
        self.assertEqual(self.plugin.group_history["oc_test"][0]["sender"], "原昵称")
        event.bot.contact.v3.user.aget.assert_not_awaited()
        event.bot.im.v1.chat_members.aget.assert_not_awaited()

    async def test_history_disabled_does_not_create_file(self):
        self.plugin.config["history_inject_count"] = 0
        event = Event()
        event.bot = client()
        await handle_on_message(self.plugin, event)
        self.assertFalse(self.plugin._history_file.exists())

    def test_seconds_and_milliseconds_format_identically(self):
        timestamp = 1788536384
        expected = datetime.datetime.fromtimestamp(timestamp).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        for value in (
            timestamp,
            str(timestamp),
            timestamp * 1000,
            str(timestamp * 1000),
        ):
            with self.subTest(value=value):
                self.assertEqual(self.plugin._format_history_time(value), expected)

    def test_missing_or_invalid_timestamp_falls_back_to_now(self):
        for value in (None, "bad", -1, 0, float("inf"), float("nan")):
            with self.subTest(value=value):
                before = datetime.datetime.now().replace(microsecond=0)
                actual = datetime.datetime.strptime(
                    self.plugin._format_history_time(value), "%Y-%m-%d %H:%M:%S"
                )
                self.assertLessEqual(before, actual)
                self.assertLessEqual(actual, datetime.datetime.now())

    async def test_legacy_and_dated_history_survive_reload_and_prompt_injection(self):
        records = [
            {
                "msg_id": "old",
                "time": "15:39:44",
                "sender": "用户(4ada)",
                "sender_id": "ou_test4ada",
                "content": "旧消息",
            },
            {
                "msg_id": "yesterday",
                "time": "2026-09-03 15:39:44",
                "sender": "张三",
                "sender_id": "ou_test4ada",
                "content": "昨天的工作",
            },
            {
                "msg_id": "today",
                "time": "2026-09-04 15:39:44",
                "sender": "李四",
                "sender_id": "ou_test1234",
                "content": "今天的工作",
            },
        ]
        self.plugin._history_file.write_text(json.dumps({"oc_test": records}))
        self.plugin._load_history()
        self.plugin._save_history(force=True)
        self.assertEqual(
            json.loads(self.plugin._history_file.read_text())["oc_test"], records
        )
        req = SimpleNamespace(
            system_prompt="", prompt="总结一下", contexts=[], image_urls=[]
        )
        await handle_on_llm_request(self.plugin, Event(), req)
        self.assertIn("[15:39:44]", req.system_prompt)
        self.assertIn("[2026-09-03 15:39:44] 张三(4ada)", req.system_prompt)
        self.assertIn("[2026-09-04 15:39:44] 李四(1234)", req.system_prompt)
