# lark_enhance 开发协作指南（AGENTS）

## 1. 项目定位

`lark_enhance` 是 AstrBot 的 Lark（飞书）增强插件，目标是让 LLM 在群聊场景中获得更完整的上下文，并提升输出可用性。

核心增强能力：
- OpenID/mention 解析为真实昵称。
- 引用消息内容回填到当前对话。
- 群聊历史持久化与注入。
- 群信息（名称/描述）注入。
- 群聊氛围感知（根据近期聊天自动调节语气）。
- 群梗记忆（自动捕获 + 工具读写 + prompt 注入）。
- 拟人节奏控制（短句、口语化、节奏变化）。
- `@名字` 自动转换为飞书 `At` 组件。
- 流式卡片打字机输出（Patch API）。
- 用户记忆（群维度 + 用户维度）存储与注入。
- 工具：`lark_emoji_reply`（单条消息限一次反应）。

## 2. 运行环境与安装

- Python: 3.10+
- 依赖：见 `requirements.txt`（主要为 `lark-oapi`）
- 插件路径：`AstrBot/data/plugins/astrbot_lark_enhance`

安装/更新：
1. 将插件目录放入 `data/plugins/`。
2. 执行：`pip install -r requirements.txt`。
3. 重启 AstrBot。
4. 更新时在插件目录执行 `git pull` 后重启。

## 3. 代码结构与关键文件

- `main.py`: 插件主体（事件钩子、缓存、历史、记忆、流式卡片、工具实现）。
- `_conf_schema.json`: 配置定义与默认值。
- `metadata.yaml`: 插件元信息。
- `data/group_history.json`: 群聊历史持久化。

## 4. 事件流（按执行顺序）

### 4.1 `on_message`

主要职责：
- 校验平台为 Lark。
- 真实昵称解析（发送者/mentions）。
- 清理消息内容中的序列化噪声。
- 处理引用消息，拉取被引用内容与发送者。
- 若引用消息含图片，下载后作为多模态输入素材。
- 自动捕获“记住这个梗”类消息，沉淀群梗记忆。
- 记录群聊历史（滑动窗口 + 持久化）。
- 拉取并缓存群信息。

### 4.2 `on_llm_request`

主要职责：
- 注入群信息、引用消息、历史消息。
- 将引用图片注入 `req.image_urls` 供当前多模态模型理解。
- 注入用户记忆（开启时）。
- 注入群梗记忆、群聊氛围和拟人节奏提示。

### 4.3 `on_decorating_result`

主要职责：
- 清理模型输出中的结构化噪声。
- 将 `@名字` 转换为飞书可识别 mention 组件。

## 5. 流式卡片实现

开启 `enable_streaming_card` 后：
- 通过 monkey patch 替换 `LarkMessageEvent.send_streaming`。
- 使用 `LarkStreamingCard` 创建、增量更新、最终完成卡片。
- 走 `im.v1.message.patch`（代码使用 `apatch`）更新内容。
- 内置节流策略，平衡实时性与 API 压力。

## 6. 持久化与缓存策略

### 6.1 群历史
- 存储文件：`data/group_history.json`
- 触发：接收消息与机器人发言时更新
- 保存：防抖写盘
- 清理：`/reset` 会清空当前群历史

### 6.2 缓存
- 用户昵称缓存：LRU + TTL
- 群成员缓存：TTL（用于 mention 转换）
- 群信息缓存：TTL（用于上下文注入）
- mention 正则缓存：TTL

## 7. 用户记忆功能

当 `enable_user_memory=true`：
- 记录用户偏好/事实/指令等记忆。
- 按群和用户维度管理。
- 受 `memory_max_per_user`、`memory_max_per_group` 限制。
- 每次请求按 `memory_inject_limit` 注入到 LLM 请求上下文。

## 8. 配置项（与 `_conf_schema.json` 对齐）

| Key | Type | Default | 说明 |
|---|---|---:|---|
| `enable_real_name` | bool | `true` | 启用真实姓名转换 |
| `enable_quoted_content` | bool | `true` | 启用引用消息增强 |
| `enable_group_info` | bool | `true` | 注入群名称/描述到上下文 |
| `enable_streaming_card` | bool | `false` | 启用流式卡片输出（需同时开启 AstrBot 流式输出） |
| `enable_vibe_sense` | bool | `true` | 启用群聊氛围感知 |
| `enable_meme_memory` | bool | `true` | 启用群梗记忆 |
| `enable_human_rhythm` | bool | `true` | 启用拟人节奏控制 |
| `history_inject_count` | int | `20` | 历史记录与注入条数，`0` 表示禁用 |
| `bot_name` | string | `"助手"` | 机器人在历史中的显示名 |
| `enable_mention_convert` | bool | `true` | 启用 `@名字` 转飞书 mention |
| `enable_user_memory` | bool | `true` | 启用用户记忆 |
| `memory_max_per_user` | int | `20` | 每用户每群最大记忆条数 |
| `memory_max_per_group` | int | `30` | 每群最大群维度记忆条数 |
| `memory_inject_limit` | int | `10` | 每次请求最大注入记忆条数 |

## 9. 开发规范

- 遵循 PEP 8，优先完整类型注解。
- 使用 `async/await`，避免阻塞 I/O。
- 日志统一使用 `[lark_enhance]` 前缀。
- 对外部 API 调用必须 `try-except` 包裹，避免影响主进程。
- 新增配置必须同时更新 `_conf_schema.json` 与本文档。

## 10. 手工测试清单

最小回归建议：
1. 提及解析：消息含 `@` 时，LLM 获取到真实昵称。
2. 提及转换：模型输出 `@某人` 后实际变成飞书 mention。
3. 引用增强：回复他人消息时，模型能理解被引用内容。
4. 历史注入：连续对话后可引用近期上下文。
5. 记忆注入：多轮后能记住用户偏好并复用。
6. 反应工具：`lark_emoji_reply` 生效且不会重复刷反应。
7. 流式卡片：开启后可稳定增量刷新并最终收敛。
8. `/reset`：仅清理当前群历史。

## 11. 安全与边界

- 仅在 Lark 平台执行 Lark 特定逻辑。
- 对 `41050` 等权限错误要降级处理，不中断主流程。
- 持久化文件只存最小必要信息，避免扩散敏感数据。

## 12. 常见变更建议

- 涉及 prompt 注入逻辑时，优先保证“可解释、可回滚、可观测”（日志可追踪）。
- 涉及缓存/持久化结构变更时，考虑旧数据兼容与异常恢复。
- 涉及 monkey patch 时，必须保持回退路径（fallback 到原始流式发送）。
