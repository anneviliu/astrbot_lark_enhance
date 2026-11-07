import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.support import BaseEvent, Plain, astrbot_stubs

with astrbot_stubs():
    from lark_enhance.mixins import streaming


async def chunks():
    yield SimpleNamespace(chain=[Plain("你好")], type="plain")


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = patch.multiple(
            streaming,
            _original_lark_send_streaming=None,
            _streaming_config={"enable_streaming_card": True},
            _clean_content_func=None,
        )
        self.state.start()
        self.addCleanup(self.state.stop)

    def legacy_event_class(self):
        class LegacyEvent(BaseEvent):
            def __init__(self):
                self.bot = object()
                self.message_obj = SimpleNamespace(
                    group_id="oc_test", message_id="om_test"
                )
                self.calls = []

            def get_sender_id(self):
                return "ou_test"

            async def send_streaming(self, generator, use_fallback=False):
                self.calls.append((generator, use_fallback))
                self.received = [item async for item in generator]
                return "original result"

        return LegacyEvent

    async def test_native_cardkit_method_is_left_untouched(self):
        class NativeEvent(self.legacy_event_class()):
            _create_streaming_card = AsyncMock()
            _update_streaming_text = AsyncMock()
            _close_streaming_mode = AsyncMock()

        original = NativeEvent.send_streaming
        with astrbot_stubs(NativeEvent):
            streaming.StreamingMixin()._setup_streaming_patch()
            streaming.StreamingMixin()._setup_streaming_patch()
        self.assertIs(NativeEvent.send_streaming, original)
        self.assertIsNone(streaming._original_lark_send_streaming)
        event = NativeEvent()
        generator = chunks()
        result = await event.send_streaming(generator, use_fallback=True)
        self.assertEqual(result, "original result")
        self.assertEqual(event.calls, [(generator, True)])
        self.assertEqual(event.received[0].chain[0].text, "你好")

    def test_partial_or_noncallable_cardkit_helpers_still_patch_legacy(self):
        for missing in (
            "_create_streaming_card",
            "_update_streaming_text",
            "_close_streaming_mode",
        ):
            with self.subTest(missing=missing):
                event_cls = self.legacy_event_class()
                for name in (
                    "_create_streaming_card",
                    "_update_streaming_text",
                    "_close_streaming_mode",
                ):
                    setattr(event_cls, name, None if name == missing else AsyncMock())
                original = event_cls.send_streaming
                streaming._original_lark_send_streaming = None
                with astrbot_stubs(event_cls):
                    streaming.StreamingMixin()._setup_streaming_patch()
                self.assertIsNot(event_cls.send_streaming, original)

    async def test_legacy_disabled_config_delegates_without_consuming_generator(self):
        event_cls = self.legacy_event_class()
        with astrbot_stubs(event_cls):
            streaming.StreamingMixin()._setup_streaming_patch()
        streaming.configure_streaming_runtime({"enable_streaming_card": False}, None)
        event = event_cls()
        generator = chunks()
        with patch.object(streaming, "LarkStreamingCard") as card:
            self.assertEqual(
                await event.send_streaming(generator, True), "original result"
            )
            card.assert_not_called()
        self.assertEqual(event.calls, [(generator, True)])
        self.assertEqual(event.received[0].chain[0].text, "你好")

    async def test_legacy_card_creation_failure_preserves_original_stream(self):
        event_cls = self.legacy_event_class()
        with astrbot_stubs(event_cls):
            streaming.StreamingMixin()._setup_streaming_patch()
        event = event_cls()
        generator = chunks()
        card = SimpleNamespace(create_initial_card=AsyncMock(return_value=False))
        with patch.object(streaming, "LarkStreamingCard", return_value=card):
            self.assertEqual(
                await event.send_streaming(generator, True), "original result"
            )
        self.assertEqual(event.calls, [(generator, True)])
        self.assertEqual(event.received[0].chain[0].text, "你好")

    async def test_legacy_success_still_finalizes_card_and_marks_event_sent(self):
        event_cls = self.legacy_event_class()
        with astrbot_stubs(event_cls):
            streaming.StreamingMixin()._setup_streaming_patch()
        card = SimpleNamespace(
            create_initial_card=AsyncMock(return_value=True),
            update_card=AsyncMock(return_value=True),
            finalize_card=AsyncMock(return_value=True),
        )
        event = event_cls()
        with patch.object(streaming, "LarkStreamingCard", return_value=card):
            await event.send_streaming(chunks())
        card.finalize_card.assert_awaited_once_with("你好")
        self.assertTrue(event._has_send_oper)
        self.assertEqual(event.calls, [])

    def test_legacy_patch_is_idempotent(self):
        event_cls = self.legacy_event_class()
        original = event_cls.send_streaming
        with astrbot_stubs(event_cls):
            plugin = streaming.StreamingMixin()
            plugin._setup_streaming_patch()
            patched = event_cls.send_streaming
            plugin._setup_streaming_patch()
        self.assertIs(event_cls.send_streaming, patched)
        self.assertIs(streaming._original_lark_send_streaming, original)
