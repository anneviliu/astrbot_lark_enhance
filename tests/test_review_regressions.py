import asyncio
import io
import json
import os
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from tests.support import At, Event, Plain, Plugin, astrbot_stubs, client, response

with astrbot_stubs():
    from lark_enhance.handlers import events, tools
    from lark_enhance.stores.user_memory_store import UserMemoryStore

from lark_oapi.api.im.v1.model import Message


class ReviewRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plugin = Plugin(Path(self.temp.name))
        self.plugin._memory_store = UserMemoryStore(self.plugin._data_dir)
        self.plugin._reacted_messages = OrderedDict()
        self.addCleanup(self.plugin._cancel_history_save)

    async def test_reset_both_framework_markers_clear_only_current_group(self):
        for marker in ("_clean_group_context_session", "_clean_ltm_session"):
            with self.subTest(marker=marker):
                self.plugin.group_history["oc_test"].append({"content": "旧记录"})
                self.plugin.group_history["oc_other"].append({"content": "保留"})
                event = Event()
                event.unified_msg_origin = (
                    "custom_adapter:GroupMessage:arbitrary_session"
                )
                event.set_extra(marker, True)
                event.result = NS(chain=[Plain("会话已重置")])
                await events.handle_on_message_sent(self.plugin, event)
                self.assertEqual(list(self.plugin.group_history["oc_test"]), [])
                self.assertTrue(self.plugin.group_history["oc_other"])

    async def test_quote_real_sdk_sender_uses_id_and_group_name_fallback(self):
        quote = Message(
            {
                "message_id": "om_quote",
                "sender": {"id": "ou_test4ada", "id_type": "open_id"},
                "body": {"content": json.dumps({"text": "被引用内容"})},
            }
        )
        bot = client()
        bot.im.v1.message = NS(aget=AsyncMock(return_value=response(NS(items=[quote]))))
        result = await self.plugin._get_message_content(bot, "om_quote", Event())
        self.assertEqual(result, ("被引用内容", "张三", []))
        request = bot.im.v1.message.aget.await_args.args[0]
        self.assertEqual(request.user_id_type, "open_id")

    async def test_duplicate_names_cannot_be_auto_mentioned_but_can_resolve_sender(
        self,
    ):
        bot = client(
            members_responses=[
                response(
                    NS(
                        items=[
                            NS(member_id="ou_first", name="张三"),
                            NS(member_id="ou_second", name="张三"),
                        ],
                        has_more=False,
                        page_token=None,
                    )
                )
            ]
        )
        members = await self.plugin._get_group_members(bot, "oc_test")
        self.assertNotIn("张三", members)
        for user in ("ou_first", "ou_second"):
            self.assertEqual(
                await self.plugin._get_user_nickname(bot, user, Event()), "张三"
            )

    async def test_normal_mentions_keep_boundaries_and_code(self):
        tick = chr(96)
        self.plugin._get_group_members = AsyncMock(return_value={"张三": "ou_zhangsan"})
        event = Event()
        event.bot = object()
        event.result = NS(
            chain=[Plain(f"@张三丰 {tick}@张三{tick} a@张三.com @张三，收到")],
            result_content_type="llm_result",
        )
        await events.handle_on_decorating_result(self.plugin, event)
        at = [c for c in event.result.chain if isinstance(c, At)]
        self.assertEqual(len(at), 1)
        self.assertEqual(at[0].qq, "ou_zhangsan")
        self.assertIn("@张三丰", event.result.chain[0].text)
        self.assertEqual(event.result.chain[-1].text, "，收到")

    async def test_stream_finish_fallback_records_only_once(self):
        event = Event()
        event.result = NS(
            chain=[Plain("流式最终回复")], result_content_type="streaming_finish"
        )
        await events.handle_on_decorating_result(self.plugin, event)
        await events.handle_on_decorating_result(self.plugin, event)
        self.assertEqual(len(self.plugin.group_history["oc_test"]), 1)

    async def test_quoted_images_are_reused_expired_and_size_limited(self):
        get = AsyncMock(
            side_effect=lambda request: NS(
                success=lambda: True, file=io.BytesIO(b"image")
            )
        )
        bot = NS(im=NS(v1=NS(message_resource=NS(aget=get))))
        first = await self.plugin._download_quoted_images(bot, "om_quote", ["img"])
        second = await self.plugin._download_quoted_images(bot, "om_quote", ["img"])
        self.assertEqual(first, second)
        self.assertEqual(get.await_count, 1)
        os.utime(first[0], (0, 0))
        self.plugin._cleanup_quoted_images()
        self.assertFalse(Path(first[0]).exists())
        self.plugin._QUOTED_IMAGE_MAX_BYTES = 1
        self.assertEqual(
            await self.plugin._download_quoted_images(bot, "om_quote", ["img"]), []
        )

    async def test_meme_disabled_excludes_existing_memes_from_prompt_and_tools(self):
        self.plugin.config.update(enable_user_memory=True, enable_meme_memory=False)
        self.plugin._memory_store.add_group_memory("oc_test", "meme", "群梗标记")
        self.plugin._memory_store.add_group_memory("oc_test", "fact", "事实标记")
        request = NS(system_prompt="", prompt="你好", contexts=[], image_urls=[])
        await events.handle_on_llm_request(self.plugin, Event(), request)
        self.assertNotIn("群梗标记", request.system_prompt)
        self.assertIn("事实标记", request.system_prompt)
        result = await tools.handle_lark_save_memory(
            self.plugin, Event(), "meme", "新群梗", "group"
        )
        self.assertIn("未启用", result)

    async def test_meme_only_mode_supports_save_list_delete_and_retains_disabled_facts(
        self,
    ):
        self.plugin.config.update(enable_user_memory=False, enable_meme_memory=True)
        self.plugin._memory_store.add_group_memory("oc_test", "fact", "保留事实")
        saved = await tools.handle_lark_save_memory(
            self.plugin, Event(), "meme", "可管理群梗", "group"
        )
        self.assertIn("记住了", saved)
        listed = await tools.handle_lark_list_memory(self.plugin, Event(), "group")
        self.assertIn("可管理群梗", listed)
        self.assertNotIn("保留事实", listed)
        deleted = await tools.handle_lark_forget_memory(
            self.plugin, Event(), scope="group"
        )
        self.assertIn("共 1 条", deleted)
        self.assertEqual(
            self.plugin._memory_store.get_group_memories("oc_test")[0]["content"],
            "保留事实",
        )

    async def test_memory_save_and_delete_do_not_report_success_on_io_failure(self):
        self.plugin.config["enable_user_memory"] = True
        with patch.object(
            self.plugin._memory_store, "_save_group_data", return_value=False
        ):
            result = await tools.handle_lark_save_memory(
                self.plugin, Event(), "fact", "测试事实"
            )
        self.assertIn("失败", result)
        self.plugin._memory_store.add_memory(
            "oc_test", Event().get_sender_id(), "fact", "原事实"
        )
        with patch.object(
            self.plugin._memory_store, "_save_group_data", return_value=False
        ):
            result = await tools.handle_lark_forget_memory(self.plugin, Event())
        self.assertIn("未完成", result)
        self.assertEqual(
            len(
                self.plugin._memory_store.get_memories(
                    "oc_test", Event().get_sender_id()
                )
            ),
            1,
        )

    async def test_concurrent_reactions_are_limited_and_failure_allows_retry(self):
        async def create(request):
            await asyncio.sleep(0)
            return response()

        add = AsyncMock(side_effect=create)
        event = Event()
        event.bot = NS(im=NS(v1=NS(message_reaction=NS(acreate=add))))
        await asyncio.gather(
            tools.handle_lark_emoji_reply(self.plugin, event, "THUMBSUP"),
            tools.handle_lark_emoji_reply(self.plugin, event, "HEART"),
        )
        self.assertEqual(add.await_count, 1)
        event.message_obj.message_id = "om_retry"
        add.side_effect = [response(code=41050), response()]
        await tools.handle_lark_emoji_reply(self.plugin, event, "HEART")
        await tools.handle_lark_emoji_reply(self.plugin, event, "HEART")
        self.assertIn("om_retry", self.plugin._reacted_messages)
        self.assertEqual(self.plugin._reacting_messages, set())

    async def test_payload_logging_requires_explicit_opt_in(self):
        request = NS(
            system_prompt="私密系统标记",
            prompt="私密问题标记",
            contexts=[{"content": "私密历史标记"}],
            image_urls=[],
        )
        with self.assertLogs("lark_enhance.tests", level="DEBUG") as logs:
            await events.handle_on_llm_request(self.plugin, Event(), request)
        self.assertNotIn("私密", str(logs.output))
        self.plugin.config["debug_log_payload"] = True
        with self.assertLogs("lark_enhance.tests", level="DEBUG") as logs:
            await events.handle_on_llm_request(self.plugin, Event(), request)
        self.assertIn("私密问题标记", str(logs.output))

    def test_unrecognized_json_types_are_preserved(self):
        for value in (None, 3, {}, []):
            content = json.dumps([{"type": value, "text": "hello"}])
            self.assertEqual(self.plugin._clean_content(content), content)
