from __future__ import annotations

import datetime
import json
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.core.message.message_event_result import ResultContentType


async def handle_on_message(plugin: Any, event: AstrMessageEvent):
    """监听飞书平台的所有消息事件。"""
    logger.debug(
        f"[lark_enhance] Processing message: {event.message_obj.message_id}"
    )

    lark_client = plugin._get_lark_client(event)
    if lark_client is None:
        logger.warning("[lark_enhance] lark_client is None")
        return

    sender_id = event.get_sender_id()
    enable_real_name = plugin.config.get("enable_real_name", True)

    if sender_id and enable_real_name:
        nickname = await plugin._get_user_nickname(lark_client, sender_id, event)
        if nickname:
            logger.debug(f"[lark_enhance] Found nickname: {nickname} for {sender_id}")
            event.message_obj.sender.nickname = nickname

        for comp in event.message_obj.message:
            if isinstance(comp, At) and comp.qq:
                real_name = await plugin._get_user_nickname(
                    lark_client,
                    comp.qq,
                    event,
                )
                if real_name:
                    logger.debug(f"[lark_enhance] Resolve At: {comp.qq} -> {real_name}")
                    comp.name = real_name

        new_msg_str = ""
        for comp in event.message_obj.message:
            if isinstance(comp, At):
                new_msg_str += f"@{comp.name or comp.qq} "
            elif hasattr(comp, "text"):
                new_msg_str += comp.text

        if new_msg_str:
            event.message_obj.message_str = new_msg_str
            event.message_str = new_msg_str

    group_id = event.message_obj.group_id
    sender_name_for_meme = event.message_obj.sender.nickname or sender_id or "未知用户"
    cleaned_content_for_meme = plugin._clean_content(event.message_str or "")
    if group_id and cleaned_content_for_meme:
        plugin._try_capture_group_meme(
            group_id,
            sender_name_for_meme,
            cleaned_content_for_meme,
        )

    history_count = plugin.config.get("history_inject_count", 20)
    if group_id and history_count and history_count > 0:
        try:
            plugin._ensure_history_deque(group_id, history_count)

            time_str = datetime.datetime.now().strftime("%H:%M:%S")
            sender_name = (
                event.message_obj.sender.nickname or sender_id or "未知用户"
            )
            content_str = plugin._clean_content(event.message_str)

            if content_str:
                record_item = {
                    "msg_id": event.message_obj.message_id,
                    "time": time_str,
                    "sender": sender_name,
                    "sender_id": sender_id or "",
                    "content": content_str,
                }
                plugin.group_history[group_id].append(record_item)
                plugin._save_history()
                logger.debug(
                    f"[lark_enhance] Recorded message for group {group_id}: {content_str[:20]}..."
                )
        except Exception as e:
            logger.error(f"[lark_enhance] Failed to record message history: {e}")

    if group_id and plugin.config.get("enable_group_info", True):
        group_info = await plugin._get_group_info(lark_client, group_id)
        if group_info:
            event.set_extra("lark_group_info", group_info)

    if plugin.config.get("enable_quoted_content", True):
        raw_msg = event.message_obj.raw_message
        parent_id = getattr(raw_msg, "parent_id", None)
        if parent_id:
            logger.debug(
                f"[lark_enhance] Found parent_id: {parent_id}, fetching quoted content..."
            )
            result = await plugin._get_message_content(lark_client, parent_id)

            if result:
                quoted_content, sender_name, quoted_images = result
                logger.debug(
                    f"[lark_enhance] Fetched quoted content: {quoted_content}, "
                    f"sender: {sender_name}, quoted_images={len(quoted_images)}"
                )
                event.set_extra("lark_quoted_content", quoted_content)
                event.set_extra("lark_quoted_sender", sender_name)
                if quoted_images:
                    event.set_extra("lark_quoted_images", quoted_images)


async def handle_on_message_sent(plugin: Any, event: AstrMessageEvent):
    """记录机器人自己发送的消息到群聊历史，并处理 /reset 命令。"""
    if not plugin._is_lark_event(event):
        return

    plugin._flush_pending_save()

    if event.get_extra("_clean_ltm_session", False):
        unified_msg_origin = event.unified_msg_origin
        if unified_msg_origin:
            plugin._clear_history_for_session(unified_msg_origin)

    group_id = event.message_obj.group_id
    if not group_id:
        return

    history_count = plugin.config.get("history_inject_count", 20)
    if not history_count or history_count <= 0:
        return

    try:
        plugin._ensure_history_deque(group_id, history_count)

        time_str = datetime.datetime.now().strftime("%H:%M:%S")
        sender_name = plugin.config.get("bot_name", "助手")

        content_str = ""
        result = event.get_result()
        if result and result.chain:
            texts = [c.text for c in result.chain if isinstance(c, Plain)]
            content_str = "".join(texts)

        if not content_str:
            return

        content_str = plugin._clean_content(content_str)
        if not content_str:
            return

        msg_id = f"sent_{int(datetime.datetime.now().timestamp())}"
        record_item = {
            "msg_id": msg_id,
            "time": time_str,
            "sender": sender_name,
            "sender_id": "__bot__",
            "content": content_str,
        }

        plugin.group_history[group_id].append(record_item)
        plugin._save_history()
        logger.debug(
            f"[lark_enhance] Recorded SELF message for group {group_id}: {content_str[:20]}..."
        )
    except Exception as e:
        logger.error(f"[lark_enhance] Failed to record self message history: {e}")


async def handle_on_llm_request(plugin: Any, event: AstrMessageEvent, req: ProviderRequest):
    """在请求 LLM 前，将增强的信息注入到 prompt 中。"""
    if not plugin._is_lark_event(event):
        return

    prompts_to_inject = []

    prompts_to_inject.append(
        "[输出格式要求]\n"
        "请直接用自然语言回复，不要输出任何序列化格式如 JSON、Python 列表/字典等。"
        "禁止输出类似 [{'type': 'text', 'text': '...'}] 这样的格式。"
    )

    if plugin.config.get("enable_user_memory", True):
        prompts_to_inject.append(
            "[记忆功能]\n"
            "你具有记忆信息的能力，支持两种范围：\n"
            "- scope=\"user\"（默认）：个人记忆，仅对当前用户生效。用于记住用户个人信息（称呼、偏好、职业等）。\n"
            "- scope=\"group\"：群记忆，对群内所有人生效。用于记住群相关信息（群规、项目背景、约定、群内通用知识等）。\n"
            "记忆类型 memory_type 支持：instruction / preference / fact / meme（群梗）。\n"
            "当用户要求记住信息时，根据信息性质选择合适的 scope 调用 lark_save_memory 工具。"
            "当用户询问记忆时，使用 lark_list_memory 工具（支持 scope=\"all\"、memory_type 过滤）。"
            "当用户要求忘记信息时，使用 lark_forget_memory 工具（支持 memory_type 过滤）。"
        )

    sender_id = event.get_sender_id() or ""
    sender_name = event.message_obj.sender.nickname or sender_id or "未知用户"
    sender_id_tail = sender_id[-4:] if sender_id and len(sender_id) > 4 else sender_id
    prompts_to_inject.append(
        "[当前发言者]\n"
        f"- 昵称：{sender_name}\n"
        f"- 标识：{sender_id or '未知'}\n"
        f"- 简写：{sender_id_tail or '未知'}"
    )

    if plugin.config.get("enable_group_info", True):
        group_info = event.get_extra("lark_group_info")
        if group_info:
            group_name = group_info.get("name")
            group_desc = group_info.get("description")
            if group_name:
                info_parts = [f"群名称：{group_name}"]
                if group_desc:
                    info_parts.append(f"群描述：{group_desc}")
                prompts_to_inject.append(
                    f"[当前群组信息]\n" + "\n".join(info_parts)
                )

    if plugin.config.get("enable_quoted_content", True):
        quoted_content = event.get_extra("lark_quoted_content")
        quoted_sender = event.get_extra("lark_quoted_sender")
        quoted_images = event.get_extra("lark_quoted_images") or []
        quoted_image_count = len(quoted_images) if isinstance(quoted_images, list) else 0

        if quoted_content or quoted_image_count > 0:
            logger.debug("[lark_enhance] Injecting quoted content into user prompt context.")
            if quoted_sender:
                header = f"（用户回复了「{quoted_sender}」的消息"
            else:
                header = "（用户回复了一条消息"

            if quoted_content and quoted_image_count > 0:
                quoted_prefix = (
                    f"{header}（其中包含 {quoted_image_count} 张图片）：\n"
                    f"{quoted_content}\n）\n\n"
                )
            elif quoted_content:
                quoted_prefix = f"{header}：\n{quoted_content}\n）\n\n"
            else:
                quoted_prefix = (
                    f"{header}（其中包含 {quoted_image_count} 张图片）。\n"
                    "请结合附带图片理解用户当前问题。\n）\n\n"
                )
            req.prompt = quoted_prefix + (req.prompt or "")

        if quoted_image_count > 0:
            if req.image_urls is None:
                req.image_urls = []
            req.image_urls.extend(quoted_images)
            logger.debug(
                f"[lark_enhance] Injected {quoted_image_count} quoted image(s) into req.image_urls"
            )

    history_count = plugin.config.get("history_inject_count", 0)
    group_id = event.message_obj.group_id

    if history_count > 0 and group_id:
        history_list = list(plugin.group_history.get(group_id, []))
        if history_list:
            current_msg_id = event.message_obj.message_id
            filtered_history = [
                f"[{item['time']}] {plugin._format_history_sender(item)}: {item['content']}"
                for item in history_list
                if item["msg_id"] != current_msg_id
            ]

            if filtered_history:
                recent_history = filtered_history[-history_count:]
                history_str = "\n".join(recent_history)
                prompts_to_inject.append(
                    f"\n[当前群聊最近 {len(recent_history)} 条消息记录（仅供参考，不包含当前消息）]\n{history_str}\n"
                )

    if plugin.config.get("enable_user_memory", True) and group_id:
        inject_limit = plugin.config.get("memory_inject_limit", 10)
        sender_id = event.get_sender_id()
        if sender_id:
            memories = plugin._memory_store.get_memories(group_id, sender_id, limit=inject_limit)
            if memories:
                memory_str = plugin._memory_store.format_memories_for_prompt(memories)
                sender_name = event.message_obj.sender.nickname or sender_id
                prompts_to_inject.append(
                    f"[关于当前用户「{sender_name}」的记忆]\n{memory_str}"
                )
                logger.debug(f"[lark_enhance] Injected {len(memories)} memories for user {sender_id}")

        group_memories = plugin._memory_store.get_group_memories(group_id, limit=inject_limit)
        if group_memories:
            group_memory_str = plugin._memory_store.format_memories_for_prompt(group_memories)
            prompts_to_inject.append(f"[关于当前群的记忆]\n{group_memory_str}")
            logger.debug(f"[lark_enhance] Injected {len(group_memories)} group memories for {group_id}")

    if plugin.config.get("enable_vibe_sense", True) and group_id:
        vibe_label, vibe_strategy = plugin._analyze_group_vibe(group_id)
        prompts_to_inject.append(
            "[群聊氛围]\n"
            f"- 当前氛围：{vibe_label}\n"
            f"- 回复策略：{vibe_strategy}\n"
            "- 尽量像群友接话，不要像客服模板。"
        )

    if plugin.config.get("enable_meme_memory", True) and group_id:
        meme_limit = plugin.config.get("memory_inject_limit", 10)
        memes = plugin._memory_store.get_group_memories(
            group_id=group_id,
            limit=meme_limit,
            memory_type="meme",
        )
        if memes:
            meme_prompt = plugin._memory_store.format_memories_for_prompt(memes)
            prompts_to_inject.append(
                "[当前群常用梗]\n"
                f"{meme_prompt}\n"
                "使用要求：自然、少量、相关时再用；不要强行玩梗。"
            )

        prompts_to_inject.append(
            "[群梗工具]\n"
            "当用户明确要求“记住这个梗/这个群梗是...”时，使用 lark_save_memory，"
            "并设置 scope=\"group\"、memory_type=\"meme\"。"
            "当用户要求查看群梗时，使用 lark_list_memory（scope=\"group\", memory_type=\"meme\"）。"
            "当用户要求删除群梗时，使用 lark_forget_memory（scope=\"group\", memory_type=\"meme\"）。"
        )

    if plugin.config.get("enable_human_rhythm", True):
        prompts_to_inject.append(
            "[拟人节奏]\n"
            "- 先接话再回答，像在群里聊天，不要上来就长段科普。\n"
            "- 优先 1~3 句短句，必要时再补充细节。\n"
            "- 可适度口语化（如“我懂你意思”“哈哈这个点很真实”），但不要油腻。\n"
            "- 避免每次都同一模板，句式和节奏要有变化。"
        )

    if prompts_to_inject:
        final_inject = (
            "\n----------------\n".join(prompts_to_inject) + "\n----------------\n\n"
        )
        req.system_prompt = (req.system_prompt or "") + "\n\n" + final_inject

    logger.info("=" * 20 + " [lark_enhance] LLM Request Payload " + "=" * 20)
    logger.info(f"System Prompt:\n{req.system_prompt}")
    logger.info(f"Contexts (History):\n{json.dumps(req.contexts, ensure_ascii=False, indent=2)}")
    logger.info(f"Current Prompt:\n{req.prompt}")
    logger.info("=" * 60)


async def handle_on_decorating_result(plugin: Any, event: AstrMessageEvent):
    """在消息发送前处理，清洗消息格式并将文本中的 @名字 转换为飞书 At 组件。"""
    if not plugin._is_lark_event(event):
        return

    result = event.get_result()
    if result is None or not result.chain:
        return

    if result.result_content_type == ResultContentType.STREAMING_FINISH:
        return

    cleaned_chain = []
    for comp in result.chain:
        if isinstance(comp, Plain):
            cleaned_text = plugin._clean_content(comp.text)
            cleaned_text = plugin._clean_mention_markdown(cleaned_text)
            if cleaned_text != comp.text:
                logger.debug(
                    f"[lark_enhance] Cleaned message: {comp.text[:50]}... -> {cleaned_text[:50]}..."
                )
            cleaned_chain.append(Plain(cleaned_text))
        else:
            cleaned_chain.append(comp)
    result.chain = cleaned_chain

    if not plugin.config.get("enable_mention_convert", True):
        return

    lark_client = plugin._get_lark_client(event)
    if lark_client is None:
        return

    group_id = event.message_obj.group_id
    if not group_id:
        return

    members_map = await plugin._get_group_members(lark_client, group_id)
    if not members_map:
        logger.debug(
            "[lark_enhance] No group members found, skipping mention conversion"
        )
        return

    pattern = plugin._get_mention_pattern(group_id, members_map)
    if not pattern:
        return

    new_chain = []
    for comp in result.chain:
        if not isinstance(comp, Plain):
            new_chain.append(comp)
            continue

        text = comp.text
        last_end = 0
        segments = []

        for match in pattern.finditer(text):
            name = match.group(1)
            open_id = members_map.get(name)
            if not open_id:
                continue

            before_text = text[last_end: match.start()]
            if before_text:
                before_text = before_text.rstrip()
                if before_text:
                    before_text += " "
                segments.append(Plain(before_text))

            segments.append(At(qq=open_id, name=name))
            last_end = match.end()

            logger.debug(
                f"[lark_enhance] Converted @{name} to At component (open_id: {open_id})"
            )

        if last_end < len(text):
            after_text = text[last_end:]
            after_text = after_text.lstrip()
            if after_text:
                segments.append(Plain(" " + after_text))

        if segments:
            new_chain.extend(segments)
        else:
            new_chain.append(comp)

    result.chain = new_chain
