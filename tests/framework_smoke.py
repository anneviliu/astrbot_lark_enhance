"""Run separately with a real AstrBot source tree on PYTHONPATH.

The plugin loader, configuration, decorators, messages and streaming method are
real. Only outbound Feishu API calls are mocked; no credentials are required.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


async def run_checks(runtime: Path) -> None:
    # Imports happen after isolating all AstrBot data from the caller's install.
    import lark_oapi as lark

    from astrbot.api.event import MessageChain, MessageEventResult
    from astrbot.api.message_components import At, File, Plain
    from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
    from astrbot.api.provider import ProviderRequest
    from astrbot.core import astrbot_config, db_helper, sp
    from astrbot.core.config import VERSION
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
    from astrbot.core.message.message_event_result import ResultContentType
    from astrbot.core.pipeline.respond.stage import RespondStage
    from astrbot.builtin_stars.builtin_commands.commands.conversation import (
        ConversationCommands,
    )
    from astrbot.core.provider.register import llm_tools
    from astrbot.core.star.context import Context
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import star_handlers_registry
    from astrbot.core.star.star_manager import PluginManager

    root_name = "astrbot_lark_enhance"
    module_name = f"data.plugins.{root_name}.main"
    config_dir = runtime / "data" / "config"
    config_dir.mkdir(parents=True)
    plugin_path = runtime / "data" / "plugins" / root_name
    schema = json.loads((plugin_path / "_conf_schema.json").read_text())
    config = {key: value["default"] for key, value in schema.items()}
    config.update(
        enable_streaming_card=True, enable_group_info=False, enable_quoted_content=False
    )
    config_path = config_dir / f"{root_name}_config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    await db_helper.initialize()
    if callable(getattr(sp, "initialize", None)):
        await sp.initialize()
    context = Context(
        event_queue=asyncio.Queue(),
        config=astrbot_config,
        db=db_helper,
        provider_manager=SimpleNamespace(llm_tools=llm_tools),
        platform_manager=SimpleNamespace(get_insts=lambda: [], platform_insts=[]),
        conversation_manager=None,
        message_history_manager=None,
        persona_manager=None,
        astrbot_config_mgr=None,
        knowledge_base_manager=None,
        cron_manager=None,
    )
    manager = PluginManager(context, astrbot_config)
    original_streaming = LarkMessageEvent.send_streaming
    instances = []

    def response(data=None, code=0):
        return SimpleNamespace(
            success=lambda: code == 0, data=data, code=code, msg="test"
        )

    bot = lark.Client.builder().app_id("test_app").app_secret("test_secret").build()
    bot.contact.v3.user.aget = AsyncMock(return_value=response(code=41050))
    bot.im.v1.chat_members.aget = AsyncMock(
        return_value=response(
            SimpleNamespace(
                items=[SimpleNamespace(member_id="ou_test4ada", name="张三")],
                has_more=False,
                page_token=None,
            )
        )
    )
    bot.im.v1.message.areply = AsyncMock(
        return_value=response(SimpleNamespace(message_id="om_reply"))
    )
    bot.cardkit.v1.card.acreate = AsyncMock(
        return_value=response(SimpleNamespace(card_id="card_test"))
    )
    bot.cardkit.v1.card.asettings = AsyncMock(return_value=response())
    bot.cardkit.v1.card_element.acontent = AsyncMock(return_value=response())

    def event(msg_id="om_test", text="昨天完成了第一版"):
        message = AstrBotMessage()
        message.type = MessageType.GROUP_MESSAGE
        message.self_id = "ou_bot"
        message.session_id = message.group_id = "oc_test"
        message.message_id = msg_id
        message.sender = MessageMember(user_id="ou_test4ada", nickname="ou_test4")
        message.message = [Plain(text)]
        message.message_str = text
        message.raw_message = SimpleNamespace(parent_id=None)
        message.timestamp = 1788536384
        return LarkMessageEvent(
            text,
            message,
            PlatformMetadata("lark", "framework test", "lark_primary"),
            "oc_test",
            bot,
        )

    def handlers():
        return star_handlers_registry.get_handlers_by_module_name(module_name)

    def handler(name):
        return next(h.handler for h in handlers() if h.handler_name == name)

    try:
        success, error = await manager.load(specified_dir_name=root_name)
        assert success, error
        metadata = star_map[module_name]
        plugin = metadata.star_cls
        instances.append(plugin)
        assert plugin is not None and metadata.name == "astrbot_plugin_lark_enhance"
        assert plugin.config is metadata.config
        assert plugin.config["enable_streaming_card"] is True
        assert (
            plugin._data_dir == runtime / "data/plugin_data/astrbot_plugin_lark_enhance"
        )
        assert LarkMessageEvent.send_streaming is original_streaming
        print(
            f"PASS AstrBot {VERSION}: PluginManager.load, metadata, config, data path"
        )

        expected_handlers = {
            "on_message",
            "on_message_sent",
            "on_llm_request",
            "on_decorating_result",
            "lark_emoji_reply",
            "lark_save_memory",
            "lark_list_memory",
            "lark_forget_memory",
        }
        assert {h.handler_name for h in handlers()} == expected_handlers
        for name in (
            "lark_emoji_reply",
            "lark_save_memory",
            "lark_list_memory",
            "lark_forget_memory",
        ):
            tool = llm_tools.get_func(name)
            assert tool is not None and tool.handler is not None
        print("PASS real decorators, four event hooks, four LLM tool registrations")

        incoming = event()
        await handler("on_message")(incoming)
        plugin._flush_pending_save()
        saved = json.loads(plugin._history_file.read_text())["oc_test"][0]
        expected_time = datetime.datetime.fromtimestamp(incoming.message_obj.timestamp)
        assert saved["time"] == expected_time.strftime("%Y-%m-%d %H:%M:%S")
        assert saved["sender"] == "张三"
        assert isinstance(incoming.message_obj.message[0], Plain)
        print(
            "PASS real LarkMessageEvent, Plain component, name fallback, dated history"
        )

        request = ProviderRequest(prompt="总结昨天的工作")
        await handler("on_llm_request")(event("om_next"), request)
        assert (
            saved["time"] in request.system_prompt and "张三" in request.system_prompt
        )
        incoming.set_result(MessageEventResult().message("**@张三** 收到"))
        await handler("on_decorating_result")(incoming)
        assert any(
            isinstance(c, At) and c.qq == "ou_test4ada"
            for c in incoming.get_result().chain
        )
        print(
            "PASS real ProviderRequest, result decoration and At component conversion"
        )

        incoming.set_result(MessageEventResult().message("测试回复"))
        await handler("on_message_sent")(incoming)
        plugin._flush_pending_save()
        last = json.loads(plugin._history_file.read_text())["oc_test"][-1]
        assert last["sender_id"] == "__bot__" and last["content"] == "测试回复"
        datetime.datetime.strptime(last["time"], "%Y-%m-%d %H:%M:%S")
        save_tool = llm_tools.get_func("lark_save_memory")
        result = await save_tool.handler(
            incoming, memory_type="fact", content="负责测试", scope="user"
        )
        assert "记住了" in result
        print("PASS after_message_sent and framework-bound LLM tool invocation")

        async def tokens():
            yield MessageChain().message("第一段")
            await asyncio.sleep(0)
            yield MessageChain(type="break")
            yield MessageChain().message("第二段")

        stream_event = event("om_stream")
        await stream_event.send_streaming(tokens())
        await asyncio.sleep(0)
        assert stream_event._has_send_oper
        assert bot.cardkit.v1.card.acreate.await_count == 2
        assert bot.cardkit.v1.card.asettings.await_count == 2
        contents = [
            c.args[0].request_body.content
            for c in bot.cardkit.v1.card_element.acontent.await_args_list
        ]
        assert "第一段" in contents and "第二段" in contents
        print("PASS unmodified native CardKit streaming and tool-call break boundaries")

        bot.cardkit.v1.card.acreate.return_value = response(code=41050)
        before_replies = bot.im.v1.message.areply.await_count
        fallback_event = event("om_fallback")
        await fallback_event.send_streaming(tokens())
        await asyncio.sleep(0)
        assert fallback_event._has_send_oper
        assert bot.im.v1.message.areply.await_count > before_replies
        bot.cardkit.v1.card.acreate.return_value = response(
            SimpleNamespace(card_id="card_test")
        )
        print("PASS native streaming permission failure falls back to normal sending")

        # Exercise the actual RespondStage branch: it does not call after_message_sent.
        async def mentioned_tokens():
            for part in ("@", "张", "三", " 收到"):
                yield MessageChain().message(part)

        observed = event("om_observed")
        await handler("on_message")(observed)
        observed.set_result(
            MessageEventResult()
            .set_result_content_type(ResultContentType.STREAMING_RESULT)
            .set_async_stream(mentioned_tokens())
        )
        stage = RespondStage()
        stage.config = astrbot_config
        before_updates = bot.cardkit.v1.card_element.acontent.await_count
        await stage.process(observed)
        updates = bot.cardkit.v1.card_element.acontent.await_args_list[before_updates:]
        assert any(
            '<at id="ou_test4ada"></at> 收到' in call.args[0].request_body.content
            for call in updates
        )
        assert plugin.group_history["oc_test"][-1]["content"] == "@张三 收到"
        count = len(plugin.group_history["oc_test"])
        observed.set_result(
            MessageEventResult(
                chain=[Plain("@张三 收到")],
                result_content_type=ResultContentType.STREAMING_FINISH,
            )
        )
        await handler("on_decorating_result")(observed)
        assert len(plugin.group_history["oc_test"]) == count
        assert LarkMessageEvent.send_streaming is original_streaming
        print(
            "PASS real RespondStage streaming, split mentions, bot history, no duplicate finish record"
        )

        bot.cardkit.v1.card.acreate.return_value = response(code=41050)
        fallback = event("om_observed_fallback")
        await handler("on_message")(fallback)
        before_replies = bot.im.v1.message.areply.await_count
        await fallback.send_streaming(mentioned_tokens())
        replies = bot.im.v1.message.areply.await_args_list[before_replies:]
        assert any(
            '"tag": "at"' in call.args[0].request_body.content
            and "ou_test4ada" in call.args[0].request_body.content
            for call in replies
        )
        bot.cardkit.v1.card.acreate.return_value = response(
            SimpleNamespace(card_id="card_test")
        )
        print("PASS real native fallback restores At components in ordinary messages")

        attachment = runtime / "attachment.txt"
        attachment.write_text("offline attachment")
        bot.im.v1.file.acreate = AsyncMock(
            return_value=response(SimpleNamespace(file_key="file_test"))
        )
        bot.im.v1.message.apatch = AsyncMock(return_value=response())
        bot.im.v1.message.adelete = AsyncMock(return_value=response())

        async def media_tokens():
            yield MessageChain().message("文件说明")
            yield MessageChain(
                chain=[File(file=str(attachment), name="attachment.txt")]
            )

        media_event = event("om_media")
        await handler("on_message")(media_event)
        before_replies = bot.im.v1.message.areply.await_count
        await media_event.send_streaming(media_tokens())
        replies = bot.im.v1.message.areply.await_args_list[before_replies:]
        assert any(call.args[0].request_body.msg_type == "file" for call in replies)
        assert bot.im.v1.file.acreate.await_count == 1
        print("PASS real File component is uploaded and sent from a streaming response")

        # Force only the plugin's capability choice; the real framework stays intact.
        native_check = plugin._has_native_cardkit
        plugin._has_native_cardkit = lambda _: False
        try:
            legacy = event("om_legacy")
            await handler("on_message")(legacy)
            before_patches = bot.im.v1.message.apatch.await_count
            await legacy.send_streaming(mentioned_tokens())
            assert bot.im.v1.message.apatch.await_count > before_patches
            final_card = json.loads(
                bot.im.v1.message.apatch.await_args.args[0].request_body.content
            )
            assert (
                final_card["body"]["elements"][0]["content"]
                == '<at id="ou_test4ada"></at> 收到'
            )
            bot.im.v1.message.apatch.return_value = response(code=41050)
            failed = event("om_legacy_fallback")
            await handler("on_message")(failed)
            before_replies = bot.im.v1.message.areply.await_count
            await failed.send_streaming(mentioned_tokens())
            replies = bot.im.v1.message.areply.await_args_list[before_replies:]
            assert any(call.args[0].request_body.msg_type == "post" for call in replies)
            assert bot.im.v1.message.adelete.await_count >= 1
        finally:
            plugin._has_native_cardkit = native_check
            bot.im.v1.message.apatch.return_value = response()
        print("PASS real legacy card requests, failed update fallback and cleanup")

        reset_event = event("om_reset", "/reset")
        reset_event.role = "admin"
        reset_config = {
            "platform_settings": {"unique_session": False},
            "provider_settings": {"agent_runner_type": "local"},
            "agent_runner": {"runner_type": "local"},
        }
        reset_context = SimpleNamespace(
            get_config=lambda **kwargs: reset_config,
            get_using_provider=lambda *args: object(),
            conversation_manager=SimpleNamespace(
                get_curr_conversation_id=AsyncMock(return_value="test_conversation"),
                update_conversation=AsyncMock(),
            ),
        )
        plugin.group_history["oc_other"].append({"content": "保留其他群"})
        await ConversationCommands(reset_context).reset(reset_event)
        assert reset_event.get_extra("_clean_group_context_session")
        await handler("on_message_sent")(reset_event)
        assert not plugin.group_history["oc_test"]
        assert plugin.group_history["oc_other"]
        print(
            "PASS actual framework reset flag, custom platform ID, other group isolation"
        )

        # Leave this write dirty: only production terminate() may flush it.
        plugin._record_sent_text(incoming, "重载前最后一条")
        assert plugin._pending_save

        success, error = await manager.reload("astrbot_plugin_lark_enhance")
        assert success, error
        reloaded = star_map[module_name].star_cls
        instances.append(reloaded)
        assert reloaded is not plugin
        assert {h.handler_name for h in handlers()} == expected_handlers
        assert len(handlers()) == len(expected_handlers)
        assert LarkMessageEvent.send_streaming is original_streaming
        assert reloaded.group_history["oc_test"][-1]["content"] == "重载前最后一条"
        assert plugin._closed and plugin._history_save_handle is None
        reloaded._record_sent_text(incoming, "重载后的新历史")
        reloaded._flush_pending_save()
        latest = reloaded._history_file.read_text()
        plugin._atexit_save()
        assert reloaded._history_file.read_text() == latest
        print(
            "PASS PluginManager.reload, pending write flushed, old callback disabled, no duplicate handlers"
        )
    finally:
        # The smoke runner owns these instances and the isolated database.
        for instance in instances:
            await instance.terminate()
        if callable(getattr(sp, "close", None)):
            await sp.close()
        else:
            sp._scheduler.shutdown(wait=False)
            sp._sync_loop.call_soon_threadsafe(sp._sync_loop.stop)
        await db_helper.engine.dispose()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="astrbot-plugin-smoke-") as temp:
        runtime = Path(temp)
        os.environ["ASTRBOT_ROOT"] = temp
        os.environ["ASTRBOT_DISABLE_METRICS"] = "1"
        os.environ["ASTRBOT_RELOAD"] = "0"
        sys.path.insert(0, temp)
        plugins = runtime / "data" / "plugins"
        plugins.mkdir(parents=True)
        (plugins / "astrbot_lark_enhance").symlink_to(
            Path(__file__).resolve().parents[1], target_is_directory=True
        )
        asyncio.run(run_checks(runtime))


if __name__ == "__main__":
    main()
