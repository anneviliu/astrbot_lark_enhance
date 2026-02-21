from __future__ import annotations

import atexit
import datetime
import json
import re
import time
from collections import OrderedDict, defaultdict, deque
from pathlib import Path

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import StarTools
from astrbot.core.message.message_event_result import ResultContentType

from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
)
from lark_oapi.api.im.v1.model import Emoji

from .mixins import (
    HistoryMixin,
    LarkContextMixin,
    StreamingMixin,
    TextMixin,
    configure_streaming_runtime,
)
from .stores import UserMemoryStore


class Main(HistoryMixin, LarkContextMixin, TextMixin, StreamingMixin, star.Star):
    # 插件版本（用于确认加载的代码版本）
    _VERSION = "0.3.1"

    # 缓存 TTL (秒)
    _CACHE_TTL = 300  # 5 分钟
    # 历史保存防抖间隔 (秒)
    _SAVE_DEBOUNCE = 5
    # 用户缓存最大容量
    _USER_CACHE_MAX_SIZE = 5000
    # 内容清洗最大长度限制（防止解析炸弹）
    _CLEAN_CONTENT_MAX_LEN = 10000
    # 自动识别“记住群梗”指令
    _MEME_CAPTURE_PATTERNS = [
        re.compile(r"^\s*记住这个梗[:：]?\s*(.+)$"),
        re.compile(r"^\s*(?:这|这个)?(?:就是)?(?:我们)?群梗[:：]?\s*(.+)$"),
    ]

    def __init__(self, context: star.Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

        # open_id -> (nickname, cache_time)
        self.user_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        # group_id -> {nickname -> open_id} (群成员映射)
        self.group_members_cache: dict[str, dict[str, str]] = {}
        # group_id -> cache_time (缓存时间戳)
        self._group_members_cache_time: dict[str, float] = {}
        # group_id -> compiled regex pattern
        self._mention_pattern_cache: dict[str, tuple[re.Pattern, float]] = {}
        # group_id -> group info cache
        self.group_info_cache: dict[str, dict] = {}
        # group_id -> cache_time
        self._group_info_cache_time: dict[str, float] = {}

        # 已添加表情回复的消息 ID (使用 OrderedDict 保证 FIFO)
        self._reacted_messages: OrderedDict[str, bool] = OrderedDict()

        # group_id -> deque of messages
        self._history_maxlen = self.config.get("history_inject_count", 20) or 20
        self.group_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self._history_maxlen)
        )

        # 持久化存储路径（使用框架规范方法）
        self._data_dir: Path = StarTools.get_data_dir("astrbot_plugin_lark_enhance")
        self._history_file: Path = self._data_dir / "group_history.json"

        # 历史保存防抖
        self._last_save_time: float = 0
        self._pending_save: bool = False

        # 加载持久化的历史记录
        self._load_history()

        # 初始化用户记忆存储
        self._memory_store = UserMemoryStore(self._data_dir)

        # 注册退出时保存
        atexit.register(self._atexit_save)

        # 设置全局配置引用（用于 monkey patch）
        configure_streaming_runtime(self.config, self._clean_content)

        # 设置流式卡片的 monkey patch（仅在配置开启时启用）
        if self.config.get("enable_streaming_card", False):
            self._setup_streaming_patch()
        else:
            logger.info("[lark_enhance] Streaming card is disabled by config")

        # 插件加载成功日志
        logger.info(
            f"[lark_enhance] ====== Plugin loaded successfully ====== "
            f"Version: {self._VERSION}, "
            f"UserMemory: {self.config.get('enable_user_memory', True)}, "
            f"VibeSense: {self.config.get('enable_vibe_sense', True)}, "
            f"MemeMemory: {self.config.get('enable_meme_memory', True)}"
        )

    @filter.platform_adapter_type(filter.PlatformAdapterType.LARK)
    async def on_message(self, event: AstrMessageEvent):
        """监听飞书平台的所有消息事件"""
        logger.debug(
            f"[lark_enhance] Processing message: {event.message_obj.message_id}"
        )

        lark_client = self._get_lark_client(event)
        if lark_client is None:
            logger.warning("[lark_enhance] lark_client is None")
            return

        sender_id = event.get_sender_id()
        enable_real_name = self.config.get("enable_real_name", True)

        # 1. 增强用户信息 (获取真实昵称)
        if sender_id and enable_real_name:
            nickname = await self._get_user_nickname(lark_client, sender_id, event)
            if nickname:
                logger.debug(f"[lark_enhance] Found nickname: {nickname} for {sender_id}")
                event.message_obj.sender.nickname = nickname

            # 增强 Mention 用户信息
            for comp in event.message_obj.message:
                if isinstance(comp, At) and comp.qq:
                    real_name = await self._get_user_nickname(
                        lark_client, comp.qq, event
                    )
                    if real_name:
                        logger.debug(f"[lark_enhance] Resolve At: {comp.qq} -> {real_name}")
                        comp.name = real_name

            # 重新构建 message_str
            new_msg_str = ""
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    new_msg_str += f"@{comp.name or comp.qq} "
                elif hasattr(comp, "text"):
                    new_msg_str += comp.text

            if new_msg_str:
                event.message_obj.message_str = new_msg_str
                event.message_str = new_msg_str

        # 2. 记录群聊历史
        group_id = event.message_obj.group_id
        sender_name_for_meme = event.message_obj.sender.nickname or sender_id or "未知用户"
        cleaned_content_for_meme = self._clean_content(event.message_str or "")
        if group_id and cleaned_content_for_meme:
            self._try_capture_group_meme(group_id, sender_name_for_meme, cleaned_content_for_meme)

        history_count = self.config.get("history_inject_count", 20)
        if group_id and history_count and history_count > 0:
            try:
                self._ensure_history_deque(group_id, history_count)

                time_str = datetime.datetime.now().strftime("%H:%M:%S")
                sender_name = (
                    event.message_obj.sender.nickname or sender_id or "未知用户"
                )
                content_str = self._clean_content(event.message_str)

                if content_str:  # 只记录非空内容
                    record_item = {
                        "msg_id": event.message_obj.message_id,
                        "time": time_str,
                        "sender": sender_name,
                        "sender_id": sender_id or "",
                        "content": content_str,
                    }
                    self.group_history[group_id].append(record_item)
                    self._save_history()
                    logger.debug(
                        f"[lark_enhance] Recorded message for group {group_id}: {content_str[:20]}..."
                    )
            except Exception as e:
                logger.error(f"[lark_enhance] Failed to record message history: {e}")

        # 3. 获取群组信息
        if group_id and self.config.get("enable_group_info", True):
            group_info = await self._get_group_info(lark_client, group_id)
            if group_info:
                event.set_extra("lark_group_info", group_info)

        # 4. 处理引用消息
        if self.config.get("enable_quoted_content", True):
            raw_msg = event.message_obj.raw_message
            parent_id = getattr(raw_msg, "parent_id", None)
            if parent_id:
                logger.debug(
                    f"[lark_enhance] Found parent_id: {parent_id}, fetching quoted content..."
                )
                result = await self._get_message_content(lark_client, parent_id)

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

    @filter.after_message_sent()
    async def on_message_sent(self, event: AstrMessageEvent):
        """记录机器人自己发送的消息到群聊历史，并处理 /reset 命令"""
        if not self._is_lark_event(event):
            return

        # 强制保存待保存的历史（利用消息发送事件作为触发点）
        self._flush_pending_save()

        # 处理 /reset 命令
        if event.get_extra("_clean_ltm_session", False):
            unified_msg_origin = event.unified_msg_origin
            if unified_msg_origin:
                self._clear_history_for_session(unified_msg_origin)

        # 记录机器人自己发送的消息
        group_id = event.message_obj.group_id
        if not group_id:
            return

        history_count = self.config.get("history_inject_count", 20)
        if not history_count or history_count <= 0:
            return

        try:
            self._ensure_history_deque(group_id, history_count)

            time_str = datetime.datetime.now().strftime("%H:%M:%S")
            sender_name = self.config.get("bot_name", "助手")

            content_str = ""
            result = event.get_result()
            if result and result.chain:
                texts = [c.text for c in result.chain if isinstance(c, Plain)]
                content_str = "".join(texts)

            if not content_str:
                return

            content_str = self._clean_content(content_str)
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

            self.group_history[group_id].append(record_item)
            self._save_history()
            logger.debug(
                f"[lark_enhance] Recorded SELF message for group {group_id}: {content_str[:20]}..."
            )
        except Exception as e:
            logger.error(f"[lark_enhance] Failed to record self message history: {e}")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在请求 LLM 前，将增强的信息注入到 prompt 中。"""
        if not self._is_lark_event(event):
            return

        prompts_to_inject = []

        # 0. 输出格式约束（防止 LLM 输出序列化格式）
        prompts_to_inject.append(
            "[输出格式要求]\n"
            "请直接用自然语言回复，不要输出任何序列化格式如 JSON、Python 列表/字典等。"
            "禁止输出类似 [{'type': 'text', 'text': '...'}] 这样的格式。"
        )

        # 0.5 记忆功能提示（仅在启用时）
        if self.config.get("enable_user_memory", True):
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

        # 1. 注入群组信息
        sender_id = event.get_sender_id() or ""
        sender_name = event.message_obj.sender.nickname or sender_id or "未知用户"
        sender_id_tail = sender_id[-4:] if sender_id and len(sender_id) > 4 else sender_id
        prompts_to_inject.append(
            "[当前发言者]\n"
            f"- 昵称：{sender_name}\n"
            f"- 标识：{sender_id or '未知'}\n"
            f"- 简写：{sender_id_tail or '未知'}"
        )

        # 1. 注入群组信息
        if self.config.get("enable_group_info", True):
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

        # 2. 注入引用消息（注入到 prompt 上下文而非 system prompt）
        if self.config.get("enable_quoted_content", True):
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

            # 将引用图片注入请求，让 AstrBot 当前多模态模型直接理解图片内容
            if quoted_image_count > 0:
                if req.image_urls is None:
                    req.image_urls = []
                req.image_urls.extend(quoted_images)
                logger.debug(
                    f"[lark_enhance] Injected {quoted_image_count} quoted image(s) into req.image_urls"
                )

        # 3. 注入群聊历史
        history_count = self.config.get("history_inject_count", 0)
        group_id = event.message_obj.group_id

        if history_count > 0 and group_id:
            history_list = list(self.group_history.get(group_id, []))
            if history_list:
                current_msg_id = event.message_obj.message_id
                filtered_history = [
                    f"[{item['time']}] {self._format_history_sender(item)}: {item['content']}"
                    for item in history_list
                    if item["msg_id"] != current_msg_id
                ]

                if filtered_history:
                    recent_history = filtered_history[-history_count:]
                    history_str = "\n".join(recent_history)
                    prompts_to_inject.append(
                        f"\n[当前群聊最近 {len(recent_history)} 条消息记录（仅供参考，不包含当前消息）]\n{history_str}\n"
                    )

        # 4. 注入用户记忆
        if self.config.get("enable_user_memory", True) and group_id:
            inject_limit = self.config.get("memory_inject_limit", 10)
            sender_id = event.get_sender_id()
            if sender_id:
                memories = self._memory_store.get_memories(group_id, sender_id, limit=inject_limit)
                if memories:
                    memory_str = self._memory_store.format_memories_for_prompt(memories)
                    sender_name = event.message_obj.sender.nickname or sender_id
                    prompts_to_inject.append(
                        f"[关于当前用户「{sender_name}」的记忆]\n{memory_str}"
                    )
                    logger.debug(f"[lark_enhance] Injected {len(memories)} memories for user {sender_id}")

            # 5. 注入群记忆（对群内所有人生效）
            group_memories = self._memory_store.get_group_memories(group_id, limit=inject_limit)
            if group_memories:
                group_memory_str = self._memory_store.format_memories_for_prompt(group_memories)
                prompts_to_inject.append(f"[关于当前群的记忆]\n{group_memory_str}")
                logger.debug(f"[lark_enhance] Injected {len(group_memories)} group memories for {group_id}")

        # 6. 注入群聊氛围感知
        if self.config.get("enable_vibe_sense", True) and group_id:
            vibe_label, vibe_strategy = self._analyze_group_vibe(group_id)
            prompts_to_inject.append(
                "[群聊氛围]\n"
                f"- 当前氛围：{vibe_label}\n"
                f"- 回复策略：{vibe_strategy}\n"
                "- 尽量像群友接话，不要像客服模板。"
            )

        # 7. 注入群梗记忆
        if self.config.get("enable_meme_memory", True) and group_id:
            meme_limit = self.config.get("memory_inject_limit", 10)
            memes = self._memory_store.get_group_memories(
                group_id=group_id,
                limit=meme_limit,
                memory_type="meme",
            )
            if memes:
                meme_prompt = self._memory_store.format_memories_for_prompt(memes)
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

        # 8. 拟人节奏控制
        if self.config.get("enable_human_rhythm", True):
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

        # 打印输入给 LLM 的原始内容
        logger.info("=" * 20 + " [lark_enhance] LLM Request Payload " + "=" * 20)
        logger.info(f"System Prompt:\n{req.system_prompt}")
        logger.info(f"Contexts (History):\n{json.dumps(req.contexts, ensure_ascii=False, indent=2)}")
        logger.info(f"Current Prompt:\n{req.prompt}")
        logger.info("=" * 60)

    @filter.llm_tool(name="lark_emoji_reply")
    async def lark_emoji_reply(self, event: AstrMessageEvent, emoji: str):
        """飞书表情回复工具。仅在非常有必要对用户的消息表达强烈情感（如点赞、开心、收到）且无需文字回复时使用。请勿滥用此工具，不要对每条消息都进行表情回复。

        常用表情代码（基于飞书官方 API）：
        THUMBSUP(点赞), THUMBSDOWN(踩), JIAYI(+1), OK, YES, NO, DONE(完成), CHECKMARK(勾), CROSSMARK(叉)
        SMILE(微笑), LAUGH(大笑), JOYFUL(开心), BLUSH(脸红), WINK(眨眼), SHY(害羞), SMIRK(坏笑), PROUD(得意)
        THANKS(感谢), HEART(比心), KISS(飞吻), LOVE(爱心), HUG(拥抱), ROSE(玫瑰), FINGERHEARD(比心手势)
        APPLAUSE(鼓掌), CLAP(鼓掌), HIGHFIVE(击掌), FISTBUMP(碰拳), SHAKE(握手), SALUTE(敬礼), WAVE(再见)
        AWESOME(666), MUSCLE(强), FIRE(火), TROPHY(奖杯), LGTM(LGTM), GET(收到), ONIT(搞定), PRAISE(赞)
        CRY(流泪), SOB(大哭), TEARS(流泪), HEARTBROKEN(心碎), WRONGED(委屈), COMFORT(安慰)
        ANGRY(生气), SCOWL(皱眉), FROWN(不高兴), SPEECHLESS(无语), SWEAT(汗), FACEPALM(捂脸)
        WOW(哇), SHOCKED(震惊), PETRIFIED(石化), TERROR(恐惧), DIZZY(晕), SKULL(骷髅)
        GLANCE(斜眼), DULL(呆), SMART(机智), THINKING(思考), SHHH(嘘), SILENT(沉默)
        BEER(啤酒), COFFEE(咖啡), CAKE(蛋糕), GIFT(礼物), REDPACKET(红包), PARTY(派对)
        CUCUMBER(吃瓜), SLAP(打脸), POOP(便便), SPITBLOOD(吐血), RAINBOWPUKE(彩虹吐)
        SLEEP(睡觉), YAWN(打哈欠), EATING(吃饭), SICK(生病), DROWSY(困)
        BEAR(熊), HUSKY(哈士奇), BULL(牛), CALF(小牛), SNOWMAN(雪人), LUCK(锦鲤)

        Args:
            emoji(string): 表情代码。请使用上述列表中的全大写英文代码。
        """
        if not self._is_lark_event(event):
            return "不是飞书平台，无法使用表情回复。"

        message_id = event.message_obj.message_id
        if message_id in self._reacted_messages:
            logger.debug(
                f"[lark_enhance] Message {message_id} already has emoji reaction, skipping"
            )
            return "该消息已添加过表情回复，每条消息只能添加一个表情。"

        lark_client = self._get_lark_client(event)
        if lark_client is None:
            logger.warning("[lark_enhance] lark_client is None, cannot add emoji reaction")
            return "无法获取飞书客户端，添加表情失败。"

        try:
            # 使用 Lark SDK 直接调用 API 添加表情回复
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
                self._reacted_messages[message_id] = True

                # 限制集合大小，FIFO 移除旧记录
                while len(self._reacted_messages) > 1000:
                    self._reacted_messages.popitem(last=False)

                logger.info(f"[lark_enhance] Reacted with {emoji} to message {message_id}")
                return None
            else:
                logger.error(
                    f"[lark_enhance] React failed: {response.code} - {response.msg}"
                )
                return f"添加 {emoji} 表情失败: {response.msg}"

        except Exception as e:
            logger.error(f"[lark_enhance] React failed: {e}")
            return f"添加 {emoji} 表情失败"

    @filter.llm_tool(name="lark_save_memory")
    async def lark_save_memory(
        self,
        event: AstrMessageEvent,
        memory_type: str,
        content: str,
        scope: str = "user"
    ):
        """保存记忆。当用户明确要求记住某些信息时使用此工具。

        记忆仅在当前群生效，不会影响其他群的交互。

        Args:
            memory_type(string): 记忆类型，必须是以下之一：
                - preference: 偏好（如称呼、回复风格、语言偏好）
                - fact: 事实（如职业、负责的项目、技能特长、群的用途）
                - instruction: 持久指令（如"总是用英文回复"、"不要用表情"）
                - meme: 群梗（建议配合 scope="group" 使用）
            content(string): 要记住的内容，用简洁的陈述句描述（如"希望被称呼为小王"、"是后端开发工程师"、"这个群是讨论 X 项目的"）
            scope(string): 记忆范围，必须是以下之一：
                - user（默认）: 个人记忆，仅对当前用户生效。用于用户个人信息（称呼、偏好、职业等）。
                - group: 群记忆，对群内所有人生效。用于群相关信息（群规、项目背景、约定、群内通用知识等）。
        """
        if not self._is_lark_event(event):
            return "不是飞书平台，无法使用记忆功能。"

        if not self.config.get("enable_user_memory", True):
            return "记忆功能未启用。"

        group_id = event.message_obj.group_id
        if not group_id:
            return "记忆功能仅在群聊中可用。"

        # 验证记忆类型
        valid_types = {"preference", "fact", "instruction", "meme"}
        if memory_type not in valid_types:
            return f"无效的记忆类型。请使用: {', '.join(valid_types)}"

        # 验证范围
        valid_scopes = {"user", "group"}
        if scope not in valid_scopes:
            return f"无效的记忆范围。请使用: {', '.join(valid_scopes)}"

        if scope == "group":
            # 群级别记忆
            max_per_group = self.config.get("memory_max_per_group", 30)
            success = self._memory_store.add_group_memory(
                group_id=group_id,
                memory_type=memory_type,
                content=content,
                max_per_group=max_per_group
            )
            scope_desc = "群记忆"
        else:
            # 用户级别记忆
            sender_id = event.get_sender_id()
            if not sender_id:
                return "无法获取用户信息。"

            max_per_user = self.config.get("memory_max_per_user", 20)
            success = self._memory_store.add_memory(
                group_id=group_id,
                user_id=sender_id,
                memory_type=memory_type,
                content=content,
                max_per_user=max_per_user
            )
            scope_desc = "个人记忆"

        if success:
            return f"好的，我记住了（{scope_desc}）：{content}"
        else:
            return "保存记忆失败，请稍后重试。"

    @filter.llm_tool(name="lark_list_memory")
    async def lark_list_memory(
        self,
        event: AstrMessageEvent,
        scope: str = "user",
        memory_type: str = "all",
    ):
        """查询当前群的记忆。当用户询问"你记得我什么"、"你对我有什么印象"、"这个群有什么记忆"时使用此工具。

        Args:
            scope(string): 查询范围，必须是以下之一：
                - user（默认）: 仅查询当前用户的个人记忆
                - group: 仅查询群记忆
                - all: 同时查询个人记忆和群记忆
            memory_type(string): 记忆类型筛选，支持 all/preference/fact/instruction/meme
        """
        if not self._is_lark_event(event):
            return "不是飞书平台，无法使用记忆功能。"

        if not self.config.get("enable_user_memory", True):
            return "记忆功能未启用。"

        group_id = event.message_obj.group_id
        if not group_id:
            return "记忆功能仅在群聊中可用。"

        # 验证范围
        valid_scopes = {"user", "group", "all"}
        if scope not in valid_scopes:
            return f"无效的查询范围。请使用: {', '.join(valid_scopes)}"
        valid_types = {"all", "preference", "fact", "instruction", "meme"}
        if memory_type not in valid_types:
            return f"无效的记忆类型。请使用: {', '.join(valid_types)}"
        type_filter = None if memory_type == "all" else memory_type

        results = []

        # 查询用户记忆
        if scope in ("user", "all"):
            sender_id = event.get_sender_id()
            if sender_id:
                user_memories = self._memory_store.get_memories(
                    group_id,
                    sender_id,
                    limit=50,
                    memory_type=type_filter,
                )
                if user_memories:
                    user_memory_str = self._memory_store.format_memories_for_prompt(user_memories)
                    results.append(f"【个人记忆】\n{user_memory_str}")

        # 查询群记忆
        if scope in ("group", "all"):
            group_memories = self._memory_store.get_group_memories(
                group_id,
                limit=50,
                memory_type=type_filter,
            )
            if group_memories:
                group_memory_str = self._memory_store.format_memories_for_prompt(group_memories)
                results.append(f"【群记忆】\n{group_memory_str}")

        if not results:
            if scope == "user":
                return "我还没有记住关于你的任何个人信息。"
            elif scope == "group":
                return "这个群还没有任何群记忆。"
            else:
                return "我还没有记住任何信息（包括个人记忆和群记忆）。"

        return "在这个群里，我记得以下信息：\n\n" + "\n\n".join(results)

    @filter.llm_tool(name="lark_forget_memory")
    async def lark_forget_memory(
        self,
        event: AstrMessageEvent,
        target: str = "all",
        scope: str = "user",
        memory_type: str = "all",
    ):
        """删除当前群的记忆。当用户要求忘记某些信息或清除所有记忆时使用此工具。

        Args:
            target(string): 删除目标
                - "all": 一键清除所有记忆
                - 具体关键词: 删除包含该关键词的记忆（如"称呼"、"职业"、"英文"）
            scope(string): 删除范围，必须是以下之一：
                - user（默认）: 仅删除当前用户的个人记忆
                - group: 仅删除群记忆
            memory_type(string): 记忆类型筛选，支持 all/preference/fact/instruction/meme
        """
        if not self._is_lark_event(event):
            return "不是飞书平台，无法使用记忆功能。"

        if not self.config.get("enable_user_memory", True):
            return "记忆功能未启用。"

        group_id = event.message_obj.group_id
        if not group_id:
            return "记忆功能仅在群聊中可用。"

        # 验证范围
        valid_scopes = {"user", "group"}
        if scope not in valid_scopes:
            return f"无效的删除范围。请使用: {', '.join(valid_scopes)}"
        valid_types = {"all", "preference", "fact", "instruction", "meme"}
        if memory_type not in valid_types:
            return f"无效的记忆类型。请使用: {', '.join(valid_types)}"
        type_filter = None if memory_type == "all" else memory_type

        if scope == "group":
            # 删除群记忆
            deleted_count = self._memory_store.delete_group_memories(
                group_id,
                target,
                memory_type=type_filter,
            )
            scope_desc = "群记忆"
        else:
            # 删除用户记忆
            sender_id = event.get_sender_id()
            if not sender_id:
                return "无法获取用户信息。"
            deleted_count = self._memory_store.delete_memories(
                group_id,
                sender_id,
                target,
                memory_type=type_filter,
            )
            scope_desc = "个人记忆"

        if deleted_count == 0:
            if target == "all":
                return f"没有找到任何{scope_desc}需要删除。"
            else:
                return f"没有找到包含「{target}」的{scope_desc}。"

        if target == "all":
            return f"好的，我已经清除了所有{scope_desc}（共 {deleted_count} 条）。"
        else:
            return f"好的，我已经删除了包含「{target}」的{scope_desc}（共 {deleted_count} 条）。"

    # 预编译的正则：清理 @ 周围的 Markdown 格式
    # 匹配 **@xxx**、*@xxx*、__@xxx__、_@xxx_ 等模式，包括中间可能有的换行
    _MENTION_MARKDOWN_PATTERNS = [
        re.compile(r"\*\*\s*(@[^\s\*]+)\s*\*\*"),   # **@xxx**
        re.compile(r"(?<!\*)\*\s*(@[^\s\*]+)\s*\*(?!\*)"),  # *@xxx* (非 **)
        re.compile(r"__\s*(@[^\s_]+)\s*__"),         # __@xxx__
        re.compile(r"(?<!_)_\s*(@[^\s_]+)\s*_(?!_)"), # _@xxx_ (非 __)
        re.compile(r"~~\s*(@[^\s~]+)\s*~~"),         # ~~@xxx~~
        re.compile(r"`\s*(@[^\s`]+)\s*`"),           # `@xxx`
    ]

    def _clean_mention_markdown(self, text: str) -> str:
        """清理 @ 提及周围的 Markdown 格式符号

        将 **@名字** 、 *@名字* 、 __@名字__ 等转换为干净的 @名字
        """
        result = text
        for pattern in self._MENTION_MARKDOWN_PATTERNS:
            result = pattern.sub(r"\1", result)
        return result

    def _get_mention_pattern(self, group_id: str, members_map: dict[str, str]) -> re.Pattern | None:
        """获取或创建 @ 提及匹配的正则表达式（带缓存）"""
        # 检查缓存
        if group_id in self._mention_pattern_cache:
            pattern, cache_time = self._mention_pattern_cache[group_id]
            if self._is_cache_valid(cache_time):
                return pattern

        if not members_map:
            return None

        # 构建正则模式
        sorted_names = sorted(members_map.keys(), key=len, reverse=True)
        if not sorted_names:
            return None

        escaped_names = [re.escape(name) for name in sorted_names]
        # 简单匹配 @名字，Markdown 清理在预处理阶段完成
        pattern = re.compile(r"@(" + "|".join(escaped_names) + r")")

        self._mention_pattern_cache[group_id] = (pattern, time.time())
        return pattern

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在消息发送前处理，清洗消息格式并将文本中的 @名字 转换为飞书 At 组件"""
        if not self._is_lark_event(event):
            return

        result = event.get_result()
        if result is None or not result.chain:
            return

        # 如果是流式完成状态，跳过处理（已由流式卡片处理）
        if result.result_content_type == ResultContentType.STREAMING_FINISH:
            return

        # 第一步：清洗消息内容（处理 LLM 输出的序列化格式）
        cleaned_chain = []
        for comp in result.chain:
            if isinstance(comp, Plain):
                cleaned_text = self._clean_content(comp.text)
                # 清理 @ 周围的 Markdown 格式符号（如 **@名字** -> @名字）
                cleaned_text = self._clean_mention_markdown(cleaned_text)
                if cleaned_text != comp.text:
                    logger.debug(
                        f"[lark_enhance] Cleaned message: {comp.text[:50]}... -> {cleaned_text[:50]}..."
                    )
                cleaned_chain.append(Plain(cleaned_text))
            else:
                cleaned_chain.append(comp)
        result.chain = cleaned_chain

        # 第二步：处理 @ 转换（如果启用）
        if not self.config.get("enable_mention_convert", True):
            return

        lark_client = self._get_lark_client(event)
        if lark_client is None:
            return

        group_id = event.message_obj.group_id
        if not group_id:
            return

        members_map = await self._get_group_members(lark_client, group_id)
        if not members_map:
            logger.debug(
                "[lark_enhance] No group members found, skipping mention conversion"
            )
            return

        # 获取缓存的正则模式
        pattern = self._get_mention_pattern(group_id, members_map)
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

                # 获取 @ 前面的文本
                before_text = text[last_end : match.start()]
                # 清理 @ 前面的多余空白和换行（保留一个空格）
                if before_text:
                    before_text = before_text.rstrip()
                    if before_text:
                        # 如果清理后还有内容，加一个空格分隔
                        before_text += " "
                    segments.append(Plain(before_text))

                segments.append(At(qq=open_id, name=name))
                last_end = match.end()

                logger.debug(
                    f"[lark_enhance] Converted @{name} to At component (open_id: {open_id})"
                )

            if last_end < len(text):
                # 清理 @ 后面开头的多余空白和换行
                after_text = text[last_end:]
                after_text = after_text.lstrip()
                if after_text:
                    # 如果有内容，前面加一个空格分隔
                    segments.append(Plain(" " + after_text))

            if segments:
                new_chain.extend(segments)
            else:
                new_chain.append(comp)

        result.chain = new_chain
