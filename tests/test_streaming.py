import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.support import At, BaseEvent, Event, Plain, Plugin, astrbot_stubs

with astrbot_stubs():
    from lark_enhance.mixins import streaming
    from lark_enhance.services.mentions import StreamMentions


def chain(text="", kind=None, components=None):
    return SimpleNamespace(
        chain=[Plain(text)] if components is None else components, type=kind
    )


async def tokens(*parts):
    for part in parts:
        yield chain(part) if isinstance(part, str) else part


class StreamPlugin(Plugin, streaming.StreamingMixin):
    pass


class StreamEvent(Event, BaseEvent):
    def __init__(self):
        super().__init__()
        self.bot = object()
        self.sent = []
        self.chunks = []

    async def send(self, message):
        self.sent.append(message)

    async def send_streaming(self, generator, use_fallback=False):
        self.chunks.extend([chunk async for chunk in generator])
        self._has_send_oper = True


class NativeEvent(StreamEvent):
    _create_streaming_card = AsyncMock()
    _update_streaming_text = AsyncMock()
    _close_streaming_mode = AsyncMock()


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plugin = StreamPlugin(Path(self.temp.name))
        self.addCleanup(self.plugin._cancel_history_save)
        self.plugin.config.update(
            enable_streaming_card=True, enable_mention_convert=True
        )
        self.plugin._get_group_members = AsyncMock(return_value={"张三": "ou_zhangsan"})

    def card(self, **overrides):
        fields = {
            name: AsyncMock(return_value=True)
            for name in (
                "create_initial_card",
                "update_card",
                "finalize_card",
                "delete_card",
            )
        }
        fields.update(overrides)
        return SimpleNamespace(**fields)

    async def test_native_class_method_is_untouched_and_chunks_are_not_mutated(self):
        event = NativeEvent()
        original_method = NativeEvent.send_streaming
        original_chunk = chain("你好")
        self.plugin._wrap_event_streaming(event)
        with patch.object(streaming, "LarkStreamingCard") as card:
            await event.send_streaming(tokens(original_chunk))
        card.assert_not_called()
        self.assertIs(NativeEvent.send_streaming, original_method)
        self.assertEqual(original_chunk.chain[0].text, "你好")
        self.assertEqual(event.chunks[0].chain[0].text, "你好")
        self.assertEqual(self.plugin.group_history["oc_test"][-1]["content"], "你好")

    async def test_legacy_success_lazy_card_and_sent_side_effect(self):
        event = StreamEvent()
        card = self.card()
        self.plugin._wrap_event_streaming(event)
        with (
            astrbot_stubs(),
            patch.object(streaming, "LarkStreamingCard", return_value=card),
        ):
            await event.send_streaming(tokens("你好"))
        card.finalize_card.assert_awaited_once_with("你好")
        self.assertTrue(event._has_send_oper)

    async def test_empty_legacy_stream_never_creates_a_card(self):
        event = StreamEvent()
        self.plugin._wrap_event_streaming(event)
        with astrbot_stubs(), patch.object(streaming, "LarkStreamingCard") as card:
            await event.send_streaming(tokens(chain(kind="break", components=[])))
        card.assert_not_called()

    async def test_disabled_legacy_card_uses_original_sender(self):
        self.plugin.config["enable_streaming_card"] = False
        event = StreamEvent()
        self.plugin._wrap_event_streaming(event)
        with patch.object(streaming, "LarkStreamingCard") as card:
            await event.send_streaming(tokens("你好"))
        card.assert_not_called()
        self.assertEqual(event.chunks[0].chain[0].text, "你好")

    async def test_legacy_create_failure_replays_all_chunks_to_original(self):
        event = StreamEvent()
        card = self.card(create_initial_card=AsyncMock(return_value=False))
        self.plugin._wrap_event_streaming(event)
        with patch.object(streaming, "LarkStreamingCard", return_value=card):
            await event.send_streaming(tokens("第一段", "第二段"))
        self.assertEqual("".join(c.chain[0].text for c in event.chunks), "第一段第二段")
        card.finalize_card.assert_not_awaited()

    async def test_legacy_finalize_failure_sends_buffered_normal_reply(self):
        event = StreamEvent()
        card = self.card(finalize_card=AsyncMock(return_value=False))
        self.plugin._wrap_event_streaming(event)
        with (
            astrbot_stubs(),
            patch.object(streaming, "LarkStreamingCard", return_value=card),
        ):
            await event.send_streaming(tokens("@张", "三 收到"))
        card.delete_card.assert_awaited_once()
        self.assertIsInstance(event.sent[0].chain[0], At)
        self.assertEqual(event.sent[0].chain[0].qq, "ou_zhangsan")
        self.assertEqual(event.sent[0].chain[1].text, " 收到")

    async def test_legacy_update_failure_falls_back_once(self):
        event = StreamEvent()
        card = self.card(update_card=AsyncMock(return_value=False))
        self.plugin._wrap_event_streaming(event)
        with (
            astrbot_stubs(),
            patch.object(streaming, "LarkStreamingCard", return_value=card),
        ):
            await event.send_streaming(tokens("第一段", "第二段"))
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(event.sent[0].chain[0].text, "第一段第二段")
        card.update_card.assert_awaited_once()
        card.finalize_card.assert_not_awaited()

    async def test_tool_break_finishes_each_legacy_card(self):
        event = StreamEvent()
        first, second = self.card(), self.card()
        self.plugin._wrap_event_streaming(event)
        with (
            astrbot_stubs(),
            patch.object(streaming, "LarkStreamingCard", side_effect=[first, second]),
        ):
            await event.send_streaming(
                tokens("第一段", chain(kind="break", components=[]), "第二段")
            )
        first.finalize_card.assert_awaited_once_with("第一段")
        second.finalize_card.assert_awaited_once_with("第二段")

    async def test_media_is_sent_as_component_in_both_modes(self):
        for event_cls, kind in (
            (StreamEvent, None),
            (NativeEvent, None),
            (NativeEvent, "tool_result"),
        ):
            with self.subTest(event_cls=event_cls, kind=kind):
                event = event_cls()
                file = SimpleNamespace(type="file", file="/tmp/example.pdf")
                card = self.card()
                self.plugin._wrap_event_streaming(event)
                with (
                    astrbot_stubs(),
                    patch.object(streaming, "LarkStreamingCard", return_value=card),
                ):
                    await event.send_streaming(
                        tokens(chain(kind=kind, components=[Plain("文件"), file]))
                    )
                self.assertIs(event.sent[0].chain[0], file)
                self.assertNotIn("[file]", str(card.finalize_card.await_args))

    async def test_split_name_becomes_cardkit_mention_without_changing_history_text(
        self,
    ):
        event = NativeEvent()
        self.plugin._wrap_event_streaming(event)
        await event.send_streaming(tokens("@", "张", "三", " 收到"))
        text = "".join(component.text for c in event.chunks for component in c.chain)
        self.assertEqual(text, '<at id="ou_zhangsan"></at> 收到')
        self.assertEqual(
            self.plugin.group_history["oc_test"][-1]["content"], "@张三 收到"
        )

    async def test_streaming_name_at_end_and_longer_unknown_name(self):
        event = NativeEvent()
        self.plugin._wrap_event_streaming(event)
        await event.send_streaming(tokens("@张三丰 和 @张", "三"))
        text = "".join(component.text for c in event.chunks for component in c.chain)
        self.assertEqual(text, '@张三丰 和 <at id="ou_zhangsan"></at>')

    async def test_native_fallback_restores_at_components(self):
        class FallbackEvent(NativeEvent):
            async def send_streaming(self, generator, use_fallback=False):
                components = [
                    component async for c in generator for component in c.chain
                ]
                await self.send(chain(components=components))

        event = FallbackEvent()
        self.plugin._wrap_event_streaming(event)
        await event.send_streaming(tokens("@张三 收到"))
        self.assertIsInstance(event.sent[0].chain[0], At)

    async def test_observer_is_idempotent_and_closed_owner_delegates(self):
        event = NativeEvent()
        self.plugin._wrap_event_streaming(event)
        wrapped = event.send_streaming
        self.plugin._wrap_event_streaming(event)
        self.assertIs(event.send_streaming, wrapped)
        self.plugin._closed = True
        await event.send_streaming(tokens("@张三"))
        self.assertEqual(event.chunks[0].chain[0].text, "@张三")
        self.assertEqual(len(self.plugin.group_history), 0)

    async def test_reasoning_is_not_recorded_as_final_reply(self):
        event = NativeEvent()
        self.plugin._wrap_event_streaming(event)
        await event.send_streaming(tokens(chain("推理", kind="reasoning"), "回答"))
        self.assertEqual(self.plugin.group_history["oc_test"][-1]["content"], "回答")

    def test_incremental_converter_preserves_code_email_and_unicode_boundaries(self):
        tick = chr(96)
        source = f"邮箱 a@张三.com {tick}@张三{tick} {tick * 3}\n@张三\n{tick * 3} @张三丰 @张三！"
        converter = StreamMentions({"张三": "ou_zhangsan"})
        result = "".join(converter.feed(char) for char in source) + converter.feed(
            "", final=True
        )
        self.assertEqual(result, source[:-4] + '<at id="ou_zhangsan"></at>！')

    def test_longer_name_with_spaces_is_matched_across_chunks(self):
        converter = StreamMentions({"Alice": "ou_a", "Alice Smith": "ou_as"})
        result = (
            converter.feed("@Alice ") + converter.feed("Smith") + converter.feed(" 好")
        )
        result += converter.feed("", final=True)
        self.assertEqual(result, '<at id="ou_as"></at> 好')
