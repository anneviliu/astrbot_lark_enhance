from __future__ import annotations

import json
import ast
import copy
import datetime
from typing import Any
from collections import deque, defaultdict

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.core import logger

class Main(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None):
        super().__init__(context)
        self.config = config
        # open_id -> nickname
        self.user_cache: dict[str, str] = {}
        
        # group_id -> deque of messages
        # Max history size will be determined by config when used, but we init with a reasonable default
        self.group_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=50))

    @staticmethod
    def _is_lark_event(event: AstrMessageEvent) -> bool:
        return event.get_platform_name() == "lark"

    @staticmethod
    def _get_lark_client(event: AstrMessageEvent) -> Any | None:
        # Lark 平台的 Event 子类会注入 `bot` (lark_oapi.Client)。其它平台没有。
        return getattr(event, "bot", None)

    async def _get_user_nickname(self, lark_client: Any, open_id: str, event: AstrMessageEvent = None) -> str | None:
        if open_id in self.user_cache:
            return self.user_cache[open_id]
        
        # 避免查询机器人自己，防止权限错误
        if event and open_id == event.get_self_id():
            return "GH 助手" # 或者返回机器人的名字

        logger.info(f"[lark_enhance] Querying Lark user info for open_id: {open_id}")
        
        try:
            from lark_oapi.api.contact.v3 import GetUserRequest
            
            # 这里的 user_id_type 根据实际情况可能需要调整，通常 open_id 是最安全的
            request = GetUserRequest.builder() \
                .user_id(open_id) \
                .user_id_type("open_id") \
                .build()
                
            contact = getattr(lark_client, "contact", None)
            if contact is None or contact.v3 is None or contact.v3.user is None:
                logger.warning("[lark_enhance] lark_client.contact 未初始化，无法获取用户信息")
                return None

            response = await contact.v3.user.aget(request)
            
            if response.success() and response.data and response.data.user:
                nickname = response.data.user.name
                self.user_cache[open_id] = nickname
                return nickname
            elif response.code == 41050:
                logger.debug(f"获取飞书用户信息失败 (权限不足): {response.msg}。可能是机器人ID或外部联系人。")
                self.user_cache[open_id] = f"用户({open_id[-4:]})" # 缓存一个fallback，避免重复查询报错
            else:
                logger.warning(f"获取飞书用户信息失败: {response.code} - {response.msg}")
        except Exception as e:
            logger.error(f"获取飞书用户信息异常: {e}")
            
        return None

    def _clean_content(self, content_str: str) -> str:
        """清洗消息内容，尝试从 JSON/Python-repr 中提取纯文本"""
        if not content_str:
            return content_str
            
        content_str = content_str.strip()
        
        # 快速检查：如果不是以 [ 或 { 开头，大概率是普通文本
        if not (content_str.startswith("[") or content_str.startswith("{")):
            return content_str

        # 尝试解析为 JSON
        try:
            data = json.loads(content_str)
            return self._extract_text_from_data(data)
        except json.JSONDecodeError:
            pass
            
        # 尝试解析为 Python 字面量 (处理单引号情况)
        try:
            data = ast.literal_eval(content_str)
            return self._extract_text_from_data(data)
        except (ValueError, SyntaxError):
            pass
            
        # 如果解析失败，原样返回（或者可以考虑正则提取，但这里先保守处理）
        return content_str

    def _extract_text_from_data(self, data: Any) -> str:
        """递归从 list/dict 中提取 text 字段"""
        texts = []
        if isinstance(data, list):
            for item in data:
                texts.append(self._extract_text_from_data(item))
        elif isinstance(data, dict):
            if "type" in data and data["type"] == "text" and "text" in data:
                return data["text"]
            # 也可以遍历 values 递归，但目前主要针对 AstrBot 消息格式
            # 如果是其他结构的 dict，这里暂不处理
        elif isinstance(data, str):
            # 有时候 content 本身是 JSON 字符串，需要二次解析？
            # 观察用户日志：{'type': 'text', 'text': "[{'type': ...}]"}
            # 这里如果不做递归解析，可能会漏。但为了防止死循环或过度解析，暂时只做一层。
            # 如果 data 是字符串，尝试再次清洗？
            if data.strip().startswith("[") or data.strip().startswith("{"):
                 return self._clean_content(data)
            return data
            
        return "".join(texts)

    async def _parse_lark_message_body(self, lark_client: Any, body: Any) -> str | None:
        """解析 Lark 消息体 content"""
        content = getattr(body, "content", None)
        if not content:
            return None

        # 简单解析文本内容，这里可能需要更复杂的解析逻辑来处理富文本
        try:
            content_json = json.loads(content)
            if "text" in content_json:
                text_content = content_json["text"]
                # 尝试检测并修复双重编码的 AstrBot 消息格式
                if isinstance(text_content, str) and text_content.startswith("[") and "type" in text_content:
                    try:
                        # 只有当它看起来像是一个列表结构时才尝试解析
                        parsed = ast.literal_eval(text_content)
                        if isinstance(parsed, list):
                            # 提取所有 text 类型的片段
                            extracted_text = ""
                            for item in parsed:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    extracted_text += item.get("text", "")
                            if extracted_text:
                                text_content = extracted_text
                    except Exception:
                        # 解析失败则保留原文本
                        pass
                
                return text_content
            
            # 如果是 post 消息，尝试提取所有文本并处理 at
            if "content" in content_json: # post structure
                    texts = []
                    for line in content_json.get("content", []):
                        for segment in line:
                            tag = segment.get("tag")
                            if tag == "text":
                                texts.append(segment.get("text", ""))
                            elif tag == "at":
                                user_id = segment.get("user_id")
                                if user_id:
                                    real_name = await self._get_user_nickname(lark_client, user_id)
                                    texts.append(f"@{real_name or user_id}")
                                else:
                                    texts.append("@未知用户")
                            
                    return "".join(texts)
            return content # fallback
        except Exception:
            return content

    async def _get_message_content(
        self,
        lark_client: Any,
        message_id: str,
    ) -> tuple[str, str | None] | None:
        """
        获取消息内容和发送者信息
        Return: (content, sender_name)
        """
        try:
            from lark_oapi.api.im.v1 import GetMessageRequest
            
            request = GetMessageRequest.builder() \
                .message_id(message_id) \
                .build()
                
            im = getattr(lark_client, "im", None)
            if im is None or im.v1 is None or im.v1.message is None:
                logger.warning("[lark_enhance] lark_client.im 未初始化，无法获取引用消息")
                return None

            response = await im.v1.message.aget(request)
            
            if response.success() and response.data and response.data.items:
                msg_item = response.data.items[0]
                
                # 获取发送者信息
                sender_name = None
                sender = getattr(msg_item, "sender", None)
                if sender:
                    sender_id_obj = getattr(sender, "sender_id", None)
                    if sender_id_obj:
                        sender_open_id = getattr(sender_id_obj, "open_id", None)
                        if sender_open_id:
                            sender_name = await self._get_user_nickname(lark_client, sender_open_id)

                body = getattr(msg_item, "body", None)
                content = await self._parse_lark_message_body(lark_client, body)
                
                return content, sender_name
            else:
                logger.warning(f"获取飞书消息内容失败: {response.code} - {response.msg}")
        except Exception as e:
            logger.error(f"获取飞书消息内容异常: {e}")
            
        return None

    # 监听飞书平台的所有消息事件
    @filter.platform_adapter_type(filter.PlatformAdapterType.LARK)
    async def on_message(self, event: AstrMessageEvent):
        if not self._is_lark_event(event):
            return
        
        logger.info(f"[lark_enhance] Processing message: {event.message_obj.message_id}")

        lark_client = self._get_lark_client(event)
        if lark_client is None:
            logger.warning("[lark_enhance] lark_client is None")
            return

        # 1. 增强用户信息 (获取真实昵称)
        sender_id = event.get_sender_id()
        # 默认实现可能是 open_id 截断；这里尽量补全为飞书通讯录里的真实名字
        if sender_id:
            nickname = await self._get_user_nickname(lark_client, sender_id, event)
            if nickname:
                logger.info(f"[lark_enhance] Found nickname: {nickname} for {sender_id}")
                event.message_obj.sender.nickname = nickname
        
        # 增强 Mention 用户信息
        from astrbot.api.message_components import At
        for comp in event.message_obj.message:
            if isinstance(comp, At) and comp.qq: # comp.qq 存储的是 open_id
                # 如果没有名字，或者名字是 ID (AstrBot 默认行为不确定，但 adapter 那边似乎有 name)
                # 无论如何，尝试获取真实姓名覆盖，因为 Lark Adapter 里的 name 可能是 mention 下发的，也可能是 open_id
                real_name = await self._get_user_nickname(lark_client, comp.qq, event)
                if real_name:
                    logger.info(f"[lark_enhance] Resolve At: {comp.qq} -> {real_name}")
                    comp.name = real_name

        # 重新构建 message_str 以包含正确的名字，方便 LLM 理解
        # 注意：这里我们简单地用 [At:Name] 替换原来的文本可能比较困难，因为 message_str 是纯文本
        # AstrBot 的 message_str 默认是不含 At 信息的 (只含 Plain) 或者 adapter 已经处理了
        # 我们这里尝试重新生成 message_str，把 At 组件变成 @Name
        new_msg_str = ""
        for comp in event.message_obj.message:
            if isinstance(comp, At):
                new_msg_str += f"@{comp.name or comp.qq} "
            elif hasattr(comp, "text"):
                new_msg_str += comp.text
        
        if new_msg_str:
             event.message_obj.message_str = new_msg_str
             event.message_str = new_msg_str # 同步更新外层封装

        # --- 新增：记录群聊历史 ---
        group_id = event.message_obj.group_id
        if group_id:
            try:
                # 获取配置的历史数量
                history_count = 20
                if self.config:
                    history_count = self.config.get("history_inject_count", 20)
                
                if history_count > 0:
                    # 确保 deque 长度符合配置
                    if self.group_history[group_id].maxlen != history_count:
                        # 如果配置变更，需要重建 deque (保留旧数据)
                        old_data = list(self.group_history[group_id])
                        self.group_history[group_id] = deque(old_data, maxlen=history_count)

                    # 构造历史记录项
                    time_str = datetime.datetime.now().strftime("%H:%M:%S")
                    sender_name = event.message_obj.sender.nickname or sender_id or "未知用户"
                    
                    # 尝试从 raw_message 中解析更原始内容，或者直接用 message_str
                    # 这里的 message_str 已经是经过前面 At 增强后的版本
                    # 关键修改：尝试清洗 message_str，防止脏数据污染
                    
                    content_str = self._clean_content(event.message_str)

                    record_item = {
                        "msg_id": event.message_obj.message_id,
                        "time": time_str,
                        "sender": sender_name,
                        "content": content_str
                    }
                    
                    self.group_history[group_id].append(record_item)
                    logger.debug(f"[lark_enhance] Recorded message for group {group_id}: {content_str[:20]}...")
            except Exception as e:
                logger.error(f"[lark_enhance] Failed to record message history: {e}")
        # -----------------------

        # 2. 处理引用消息
        # 检查 raw_message 中是否有 parent_id
        raw_msg = event.message_obj.raw_message
        
        parent_id = getattr(raw_msg, "parent_id", None)
        if parent_id:
            logger.info(f"[lark_enhance] Found parent_id: {parent_id}, fetching quoted content...")
            result = await self._get_message_content(lark_client, parent_id)
            
            if result:
                quoted_content, sender_name = result
                logger.info(f"[lark_enhance] Fetched quoted content: {quoted_content}, sender: {sender_name}")
                
                # 将引用内容注入到 session 或者 message_obj 的 extra 字段中
                event.set_extra("lark_quoted_content", quoted_content)
                event.set_extra("lark_quoted_sender", sender_name) # 存储发送者名字

    @filter.after_message_sent()
    async def on_message_sent(self, event: AstrMessageEvent):
        """记录机器人自己发送的消息到群聊历史"""
        if not self._is_lark_event(event):
            return
            
        group_id = event.message_obj.group_id
        if not group_id:
            return

        try:
            # 获取配置的历史数量
            history_count = 20
            if self.config:
                history_count = self.config.get("history_inject_count", 20)
            
            if history_count > 0:
                # 确保 deque 长度符合配置
                if self.group_history[group_id].maxlen != history_count:
                    # 如果配置变更，需要重建 deque (保留旧数据)
                    old_data = list(self.group_history[group_id])
                    self.group_history[group_id] = deque(old_data, maxlen=history_count)

                # 构造历史记录项
                time_str = datetime.datetime.now().strftime("%H:%M:%S")
                # 机器人自己发送的消息，sender 为 "GH 助手"
                sender_name = "GH 助手"
                
                # event.message_str 在发送后通常是发送的内容
                # 警告：AstrMessageEvent 在发送后，message_str 可能仍然是用户发送的内容，而不是 Bot 回复的内容。
                # 我们需要优先从 result 中获取。
                
                content_str = ""
                result = event.get_result()
                if result:
                    # 尝试从 result 中提取文本
                    from astrbot.api.message_components import Plain
                    chain = result.chain
                    if chain:
                        texts = [c.text for c in chain if isinstance(c, Plain)]
                        content_str = "".join(texts)
                
                if not content_str:
                    # 如果 result 为空，再尝试 message_str，但要小心
                    # 为了避免记录用户的话，我们加上日志调试
                    logger.debug(f"[lark_enhance] Result chain empty. event.message_str: {event.message_str}")
                    # 只有当 message_str 与 event.message_obj.message_str (用户原始输入) 不同时才使用？
                    # 或者干脆如果不从 result 拿到就不记录了，宁缺毋滥。
                    return 

                # 清洗内容
                content_str = self._clean_content(content_str)

                if not content_str:
                    return

                # 使用当前时间戳作为 msg_id (因为发送后不一定能马上拿到 msg_id，且主要是为了去重和排序，用时间戳近似也可以)
                # 或者我们可以留空 msg_id，但在读取时需要处理
                msg_id = f"sent_{int(datetime.datetime.now().timestamp())}"
                
                record_item = {
                    "msg_id": msg_id,
                    "time": time_str,
                    "sender": sender_name,
                    "content": content_str
                }
                
                self.group_history[group_id].append(record_item)
                logger.debug(f"[lark_enhance] Recorded SELF message for group {group_id}: {content_str[:20]}...")
        except Exception as e:
            logger.error(f"[lark_enhance] Failed to record self message history: {e}")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在请求 LLM 前，将增强的信息注入到 prompt 中。"""

        if not self._is_lark_event(event):
            return

        # 清理 Context 中的 tool_calls，以避免 Gemini thought_signature 错误
        if req.contexts:
            # 使用 deepcopy 防止修改原始 history 对象
            new_contexts = copy.deepcopy(req.contexts)
            for ctx in new_contexts:
                if ctx.get("role") == "assistant" and "tool_calls" in ctx:
                    logger.debug(f"[lark_enhance] Cleaning tool_calls from context: {ctx.get('tool_calls')}")
                    ctx.pop("tool_calls", None)
                    # 如果 content 为空，给一个占位符，防止 API 报错
                    if not ctx.get("content"):
                        ctx["content"] = "（已执行工具调用）"
                
                # 同时也需要清理对应的 tool 消息，否则会有 orphan tool result
                if ctx.get("role") == "tool":
                    logger.debug(f"[lark_enhance] Cleaning tool message from context: {ctx}")
                    ctx["role"] = "assistant" # 改为 assistant
                    
                    content = ctx.get("content", "")
                    if "The tool has no return value" in content:
                        ctx["content"] = "（已执行动作）"
                    else:
                        ctx["content"] = f"（工具执行结果：{content}）"
                    
                    ctx.pop("tool_call_id", None) # 移除关联 ID
            
            # 更新 req.contexts
            req.contexts = new_contexts

        prompts_to_inject = []

        # 1. 注入引用消息
        quoted_content = event.get_extra("lark_quoted_content")
        quoted_sender = event.get_extra("lark_quoted_sender")

        if quoted_content:
            logger.info(f"[lark_enhance] Injecting quoted content into LLM prompt.")
            if quoted_sender:
                prompts_to_inject.append(f"「{quoted_sender}」在回复的消息中说道：\n{quoted_content}\n")
            else:
                prompts_to_inject.append(f"[引用消息]\n{quoted_content}\n")
        
        # 2. 注入群聊历史 (修改：从本地缓存读取)
        history_count = 0
        if self.config:
            history_count = self.config.get("history_inject_count", 0)
            
        group_id = event.message_obj.group_id
        if history_count > 0 and group_id and group_id in self.group_history:
            history_list = list(self.group_history[group_id])
            
            # 过滤掉当前正在处理的这条消息，避免重复
            current_msg_id = event.message_obj.message_id
            filtered_history = [
                f"[{item['time']}] {item['sender']}: {item['content']}" 
                for item in history_list 
                if item['msg_id'] != current_msg_id
            ]

            if filtered_history:
                # 截取配置的数量 (虽然 deque 已经限制了，但为了保险)
                recent_history = filtered_history[-history_count:]
                history_str = "\n".join(recent_history)
                prompts_to_inject.append(f"\n[当前群聊最近 {len(recent_history)} 条消息记录（仅供参考，不包含当前消息）]\n{history_str}\n")
        
        if prompts_to_inject:
             final_inject = "\n----------------\n".join(prompts_to_inject) + "\n----------------\n\n"
             # 修改为注入到 System Prompt 中，避免污染历史 Context
             req.system_prompt = (req.system_prompt or "") + "\n\n" + final_inject

        # 打印发送给 LLM 的完整内容
        logger.info("=" * 20 + " [lark_enhance] LLM Request Payload " + "=" * 20)
        logger.info(f"System Prompt: {req.system_prompt}")
        logger.info(f"Contexts (History): {req.contexts}")
        logger.info(f"Current Prompt: {req.prompt}")
        logger.info("=" * 60)

    @filter.llm_tool(name="lark_emoji_reply")
    async def lark_emoji_reply(self, event: AstrMessageEvent, emoji: str):
        """飞书表情回复工具。当你想对用户的消息表达态度（如点赞、开心、收到）时，可以使用此工具贴一个表情。
        
        Args:
            emoji(string): 表情代码。常用值：THUMBSUP(点赞), HEART(比心), OK(好的), LAUGH(大笑), CLAP(鼓掌), THANKS(感谢), WAH(哇), CRY(流泪), GLANCE(狗头), DULL(呆无辜)。
        """
        if not self._is_lark_event(event):
            return "不是飞书平台，无法使用表情回复。"
        
        try:
            # LarkMessageEvent.react 接收 emoji_type (如 "THUMBSUP")
            await event.react(emoji)
            logger.info(f"[lark_enhance] Reacted with {emoji} to message {event.message_obj.message_id}")
            # 返回 None，中断 Tool Loop，AstrBot 不会将工具执行结果发回给 LLM，从而避免 LLM 的额外回复。
            return None
        except Exception as e:
            logger.error(f"[lark_enhance] React failed: {e}")
            return f"添加 {emoji} 表情失败"
