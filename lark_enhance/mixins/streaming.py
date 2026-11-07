from __future__ import annotations

import re
import weakref
from copy import copy
from functools import wraps

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Plain

from ..services.mentions import StreamMentions
from ..services.streaming_card import LarkStreamingCard, empty_generator


def _chain_with(chain, components, kind=None):
    derived = copy(chain)
    derived.chain = components
    if kind is not None:
        derived.type = kind
    return derived


def _restore_mentions(chain, by_id: dict[str, str]):
    """CardKit uses id=; ordinary post messages require actual At components."""
    pattern = re.compile(r'<at id="([^"]+)"></at>')
    components = []
    for component in chain.chain:
        if not isinstance(component, Plain):
            components.append(component)
            continue
        end = 0
        for match in pattern.finditer(component.text):
            if match.group(1) not in by_id:
                continue
            if match.start() > end:
                components.append(Plain(component.text[end : match.start()]))
            components.append(At(qq=match.group(1), name=by_id[match.group(1)]))
            end = match.end()
        if end < len(component.text):
            components.append(Plain(component.text[end:]))
    return _chain_with(chain, components)


class StreamingMixin:
    """Observe individual events; never replace the framework's class methods."""

    @staticmethod
    def _has_native_cardkit(event) -> bool:
        return all(
            callable(getattr(event, name, None))
            for name in (
                "_create_streaming_card",
                "_update_streaming_text",
                "_close_streaming_mode",
            )
        )

    def _wrap_event_streaming(self, event):
        if not callable(getattr(event, "send_streaming", None)) or not callable(
            getattr(event, "send", None)
        ):
            return
        if event.get_extra("_lark_stream_observer", False):
            return
        event.set_extra("_lark_stream_observer", True)
        owner_ref = weakref.ref(self)
        original_stream = event.send_streaming
        original_send = event.send
        card_members: dict[str, str] = {}

        @wraps(original_send)
        async def send(chain, *args, **kwargs):
            if card_members:
                chain = _restore_mentions(chain, card_members)
            return await original_send(chain, *args, **kwargs)

        @wraps(original_stream)
        async def send_streaming(generator, use_fallback=False):
            owner = owner_ref()
            if owner is None or getattr(owner, "_closed", False):
                return await original_stream(generator, use_fallback)
            group_id = event.message_obj.group_id
            members = {}
            if group_id and owner.config.get("enable_mention_convert", True):
                try:
                    members = await owner._get_group_members(event.bot, group_id)
                except Exception as exc:
                    logger.warning(
                        f"[lark_enhance] Streaming mentions unavailable: {type(exc).__name__}"
                    )
            card_members.update({open_id: name for name, open_id in members.items()})
            text_parts = []
            prepared = owner._prepare_stream(
                event, generator, members, card_members, text_parts
            )
            if owner.config.get(
                "enable_streaming_card", False
            ) and not owner._has_native_cardkit(event):
                await owner._send_legacy_stream(
                    event, prepared, original_stream, use_fallback
                )
            else:
                await original_stream(prepared, use_fallback)
            if text_parts and not getattr(owner, "_closed", False):
                owner._record_sent_text(event, "".join(text_parts))
                event.set_extra("_lark_stream_history_recorded", True)

        event.send = send
        event.send_streaming = send_streaming

    async def _prepare_stream(
        self, event, generator, members, card_members, text_parts
    ):
        converter = StreamMentions(members)
        template = None
        async for chain in generator:
            if not hasattr(chain, "chain"):
                yield chain
                continue
            kind = getattr(chain, "type", None) or "plain"
            if kind != "plain":
                pending = converter.feed("", final=True)
                if pending and template is not None:
                    yield _chain_with(template, [Plain(pending)])
                converter = StreamMentions(members)
                media_types = {"file", "image", "record", "video"}
                has_media = any(
                    str(
                        getattr(
                            getattr(c, "type", None), "value", getattr(c, "type", "")
                        )
                    ).lower()
                    in media_types
                    for c in chain.chain
                )
                if not has_media:
                    yield chain
                    continue
            template = chain
            for component in chain.chain:
                if isinstance(component, Plain):
                    if kind == "plain":
                        text_parts.append(component.text)
                    stable = (
                        converter.feed(component.text)
                        if kind == "plain"
                        else component.text
                    )
                    if stable:
                        yield _chain_with(chain, [Plain(stable)])
                elif isinstance(component, At):
                    pending = converter.feed("", final=True)
                    if pending:
                        yield _chain_with(chain, [Plain(pending)])
                    converter = StreamMentions(members)
                    card_members[str(component.qq)] = component.name or str(
                        component.qq
                    )
                    text_parts.append(f"@{component.name or component.qq}")
                    yield _chain_with(chain, [Plain(StreamMentions.tag(component.qq))])
                else:
                    pending = converter.feed("", final=True)
                    if pending:
                        yield _chain_with(chain, [Plain(pending)])
                    converter = StreamMentions(members)
                    # Close a preceding card before delivering a file/image normally.
                    yield _chain_with(chain, [], "break")
                    await event.send(_chain_with(chain, [component]))
        pending = converter.feed("", final=True)
        if pending and template is not None:
            yield _chain_with(template, [Plain(pending)])

    async def _send_legacy_stream(
        self, event, generator, original_stream, use_fallback
    ):
        card = None
        buffer = ""
        template = None
        failed = False

        async def finish():
            nonlocal card, buffer, failed
            delivered = False
            if card is not None:
                if not failed:
                    delivered = await card.finalize_card(buffer)
                if not delivered:
                    await card.delete_card()
            if buffer and not delivered and template is not None:
                await event.send(_chain_with(template, [Plain(buffer)]))
            card, buffer, failed = None, "", False

        try:
            async for chain in generator:
                if not hasattr(chain, "chain"):
                    continue
                if getattr(chain, "type", None) == "break":
                    await finish()
                    continue
                template = chain
                text = "".join(
                    component.text
                    for component in chain.chain
                    if isinstance(component, Plain)
                )
                if not text:
                    continue
                if card is None:
                    card = LarkStreamingCard(
                        event.bot,
                        event.message_obj.group_id or event.get_sender_id(),
                        event.message_obj.message_id,
                    )
                    if not await card.create_initial_card():

                        async def remaining(first=chain):
                            yield first
                            async for rest in generator:
                                yield rest

                        return await original_stream(remaining(), use_fallback)
                buffer += text
                if not failed and not await card.update_card(buffer):
                    failed = True
            await finish()
            await AstrMessageEvent.send_streaming(
                event, empty_generator(), use_fallback
            )
        except BaseException:
            if card is not None:
                await card.delete_card()
            raise
