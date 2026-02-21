from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
)
from lark_oapi.api.im.v1.model import Emoji


async def handle_lark_emoji_reply(plugin: Any, event: AstrMessageEvent, emoji: str):
    if not plugin._is_lark_event(event):
        return "不是飞书平台，无法使用表情回复。"

    message_id = event.message_obj.message_id
    if message_id in plugin._reacted_messages:
        logger.debug(
            f"[lark_enhance] Message {message_id} already has emoji reaction, skipping"
        )
        return "该消息已添加过表情回复，每条消息只能添加一个表情。"

    lark_client = plugin._get_lark_client(event)
    if lark_client is None:
        logger.warning("[lark_enhance] lark_client is None, cannot add emoji reaction")
        return "无法获取飞书客户端，添加表情失败。"

    try:
        request = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type(emoji).build())
                .build()
            )
            .build()
        )

        im = getattr(lark_client, "im", None)
        if im is None or im.v1 is None or im.v1.message_reaction is None:
            logger.warning(
                "[lark_enhance] lark_client.im.v1.message_reaction 未初始化，无法添加表情"
            )
            return "飞书客户端未正确初始化，添加表情失败。"

        response = await im.v1.message_reaction.acreate(request)

        if response.success():
            plugin._reacted_messages[message_id] = True

            while len(plugin._reacted_messages) > 1000:
                plugin._reacted_messages.popitem(last=False)

            logger.info(f"[lark_enhance] Reacted with {emoji} to message {message_id}")
            return None

        logger.error(
            f"[lark_enhance] React failed: {response.code} - {response.msg}"
        )
        return f"添加 {emoji} 表情失败: {response.msg}"

    except Exception as e:
        logger.error(f"[lark_enhance] React failed: {e}")
        return f"添加 {emoji} 表情失败"


async def handle_lark_save_memory(
    plugin: Any,
    event: AstrMessageEvent,
    memory_type: str,
    content: str,
    scope: str = "user",
):
    if not plugin._is_lark_event(event):
        return "不是飞书平台，无法使用记忆功能。"

    if not plugin.config.get("enable_user_memory", True):
        return "记忆功能未启用。"

    group_id = event.message_obj.group_id
    if not group_id:
        return "记忆功能仅在群聊中可用。"

    valid_types = {"preference", "fact", "instruction", "meme"}
    if memory_type not in valid_types:
        return f"无效的记忆类型。请使用: {', '.join(valid_types)}"

    valid_scopes = {"user", "group"}
    if scope not in valid_scopes:
        return f"无效的记忆范围。请使用: {', '.join(valid_scopes)}"

    if scope == "group":
        max_per_group = plugin.config.get("memory_max_per_group", 30)
        success = plugin._memory_store.add_group_memory(
            group_id=group_id,
            memory_type=memory_type,
            content=content,
            max_per_group=max_per_group,
        )
        scope_desc = "群记忆"
    else:
        sender_id = event.get_sender_id()
        if not sender_id:
            return "无法获取用户信息。"

        max_per_user = plugin.config.get("memory_max_per_user", 20)
        success = plugin._memory_store.add_memory(
            group_id=group_id,
            user_id=sender_id,
            memory_type=memory_type,
            content=content,
            max_per_user=max_per_user,
        )
        scope_desc = "个人记忆"

    if success:
        return f"好的，我记住了（{scope_desc}）：{content}"
    return "保存记忆失败，请稍后重试。"


async def handle_lark_list_memory(
    plugin: Any,
    event: AstrMessageEvent,
    scope: str = "user",
    memory_type: str = "all",
):
    if not plugin._is_lark_event(event):
        return "不是飞书平台，无法使用记忆功能。"

    if not plugin.config.get("enable_user_memory", True):
        return "记忆功能未启用。"

    group_id = event.message_obj.group_id
    if not group_id:
        return "记忆功能仅在群聊中可用。"

    valid_scopes = {"user", "group", "all"}
    if scope not in valid_scopes:
        return f"无效的查询范围。请使用: {', '.join(valid_scopes)}"
    valid_types = {"all", "preference", "fact", "instruction", "meme"}
    if memory_type not in valid_types:
        return f"无效的记忆类型。请使用: {', '.join(valid_types)}"
    type_filter = None if memory_type == "all" else memory_type

    results = []

    if scope in ("user", "all"):
        sender_id = event.get_sender_id()
        if sender_id:
            user_memories = plugin._memory_store.get_memories(
                group_id,
                sender_id,
                limit=50,
                memory_type=type_filter,
            )
            if user_memories:
                user_memory_str = plugin._memory_store.format_memories_for_prompt(user_memories)
                results.append(f"【个人记忆】\n{user_memory_str}")

    if scope in ("group", "all"):
        group_memories = plugin._memory_store.get_group_memories(
            group_id,
            limit=50,
            memory_type=type_filter,
        )
        if group_memories:
            group_memory_str = plugin._memory_store.format_memories_for_prompt(group_memories)
            results.append(f"【群记忆】\n{group_memory_str}")

    if not results:
        if scope == "user":
            return "我还没有记住关于你的任何个人信息。"
        if scope == "group":
            return "这个群还没有任何群记忆。"
        return "我还没有记住任何信息（包括个人记忆和群记忆）。"

    return "在这个群里，我记得以下信息：\n\n" + "\n\n".join(results)


async def handle_lark_forget_memory(
    plugin: Any,
    event: AstrMessageEvent,
    target: str = "all",
    scope: str = "user",
    memory_type: str = "all",
):
    if not plugin._is_lark_event(event):
        return "不是飞书平台，无法使用记忆功能。"

    if not plugin.config.get("enable_user_memory", True):
        return "记忆功能未启用。"

    group_id = event.message_obj.group_id
    if not group_id:
        return "记忆功能仅在群聊中可用。"

    valid_scopes = {"user", "group"}
    if scope not in valid_scopes:
        return f"无效的删除范围。请使用: {', '.join(valid_scopes)}"
    valid_types = {"all", "preference", "fact", "instruction", "meme"}
    if memory_type not in valid_types:
        return f"无效的记忆类型。请使用: {', '.join(valid_types)}"
    type_filter = None if memory_type == "all" else memory_type

    if scope == "group":
        deleted_count = plugin._memory_store.delete_group_memories(
            group_id,
            target,
            memory_type=type_filter,
        )
        scope_desc = "群记忆"
    else:
        sender_id = event.get_sender_id()
        if not sender_id:
            return "无法获取用户信息。"
        deleted_count = plugin._memory_store.delete_memories(
            group_id,
            sender_id,
            target,
            memory_type=type_filter,
        )
        scope_desc = "个人记忆"

    if deleted_count == 0:
        if target == "all":
            return f"没有找到任何{scope_desc}需要删除。"
        return f"没有找到包含「{target}」的{scope_desc}。"

    if target == "all":
        return f"好的，我已经清除了所有{scope_desc}（共 {deleted_count} 条）。"
    return f"好的，我已经删除了包含「{target}」的{scope_desc}（共 {deleted_count} 条）。"
