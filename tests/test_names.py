import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.support import Event, Plugin, client, response


class NameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plugin = Plugin(Path(self.temp.name))
        self.event = Event()
        self.open_id = self.event.get_sender_id()

    async def test_contact_name_is_preferred_and_cached(self):
        bot = client(response(SimpleNamespace(user=SimpleNamespace(name="通讯录姓名"))))
        for _ in range(2):
            self.assertEqual(
                await self.plugin._get_user_nickname(bot, self.open_id, self.event),
                "通讯录姓名",
            )
        bot.contact.v3.user.aget.assert_awaited_once()
        bot.im.v1.chat_members.aget.assert_not_awaited()

    async def test_permission_denied_falls_back_to_members_and_caches_name(self):
        bot = client()
        for _ in range(2):
            self.assertEqual(
                await self.plugin._get_user_nickname(bot, self.open_id, self.event),
                "张三",
            )
        bot.contact.v3.user.aget.assert_awaited_once()
        bot.im.v1.chat_members.aget.assert_awaited_once()
        request = bot.im.v1.chat_members.aget.call_args.args[0]
        self.assertEqual(request.chat_id, "oc_test")
        self.assertEqual(request.member_id_type, "open_id")

    async def test_existing_placeholder_can_be_replaced_by_member_name(self):
        self.plugin._set_user_cache(self.open_id, "用户(4ada)")
        bot = client()
        self.assertEqual(
            await self.plugin._get_user_nickname(bot, self.open_id, self.event), "张三"
        )
        self.assertEqual(self.plugin._get_user_from_cache(self.open_id), "张三")
        bot.contact.v3.user.aget.assert_not_awaited()

    async def test_uninitialized_contact_still_uses_members(self):
        bot = client()
        bot.contact = None
        self.assertEqual(
            await self.plugin._get_user_nickname(bot, self.open_id, self.event), "张三"
        )

    async def test_contact_exception_still_uses_members(self):
        bot = client()
        bot.contact.v3.user.aget.side_effect = RuntimeError("offline")
        self.assertEqual(
            await self.plugin._get_user_nickname(bot, self.open_id, self.event), "张三"
        )

    async def test_member_pagination_can_resolve_sender_on_later_page(self):
        bot = client(
            members_responses=[
                response(SimpleNamespace(items=[], has_more=True, page_token="next")),
                response(
                    SimpleNamespace(
                        items=[
                            SimpleNamespace(member_id=self.open_id, name="第二页姓名")
                        ],
                        has_more=False,
                        page_token=None,
                    )
                ),
            ]
        )
        self.assertEqual(
            await self.plugin._get_user_nickname(bot, self.open_id, self.event),
            "第二页姓名",
        )
        self.assertEqual(bot.im.v1.chat_members.aget.call_count, 2)
        self.assertEqual(
            bot.im.v1.chat_members.aget.call_args.args[0].page_token, "next"
        )

    async def test_permission_failures_preserve_known_sender_name(self):
        bot = client(members_responses=[response(code=41050)])
        self.event.message_obj.sender.nickname = "已有昵称"
        self.assertEqual(
            await self.plugin._get_user_nickname(bot, self.open_id, self.event),
            "已有昵称",
        )

    async def test_permission_failures_degrade_to_identifier(self):
        bot = client(members_responses=[response(code=41050)])
        self.assertEqual(
            await self.plugin._get_user_nickname(bot, self.open_id, self.event),
            "用户(4ada)",
        )

    async def test_direct_message_does_not_query_group_members(self):
        self.event.message_obj.group_id = ""
        bot = client()
        self.assertEqual(
            await self.plugin._get_user_nickname(bot, self.open_id, self.event),
            "用户(4ada)",
        )
        bot.im.v1.chat_members.aget.assert_not_awaited()
