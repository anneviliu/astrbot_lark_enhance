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
- 流式卡片输出（优先框架原生 CardKit，旧版兼容 Patch API）。
- 用户记忆（群维度 + 用户维度）存储与注入。
- 工具：`lark_emoji_reply`（单条消息限一次反应）。

## 2. 运行环境与安装

- 插件语法：Python 3.10+；已验证的 AstrBot v4.27.2 / v4.29.4 框架要求 Python 3.12+。
- 依赖：见 `requirements.txt`（主要为 `lark-oapi`）
- 插件路径：`AstrBot/data/plugins/astrbot_lark_enhance`

安装/更新：
1. 将插件目录放入 `data/plugins/`。
2. 执行：`pip install -r requirements.txt`。
3. 重启 AstrBot。
4. 更新时在插件目录执行 `git pull` 后重启。

## 3. 代码结构与关键文件

- `main.py`: 插件入口、钩子、工具注册及卸载流程；实现分布在 handlers / mixins / services / stores。
- `lark_enhance/plugin.py`: 旧导入路径兼容别名，统一指向入口 Main，不保留重复实现。
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

流式增强绑定到单次事件，不修改框架的全局类方法：
- 原生 CardKit 的创建、更新和关闭仍由 AstrBot 执行，插件仅转换跨 token 提及并记录最终正文。
- `enable_streaming_card=true` 且缺少原生 CardKit 能力时，使用兼容卡片；创建失败委托原发送器，更新/完成失败用缓冲正文回退。
- 保留工具调用分界；非文本组件通过普通发送接口交付，普通消息回退将 CardKit 标签还原为 At 组件。
- 重载后旧事件可完成原发送过程，已关闭实例不再写历史。

## 6. 持久化与缓存策略

### 6.1 群历史
- 存储文件：`data/group_history.json`
- 触发：接收消息与机器人发言时更新
- 保存：延迟刷新定时器 + 临时文件、fsync、原子替换；失败保留旧文件并允许重试。
- 卸载：terminate 刷新待写数据、取消定时器并注销 atexit，旧实例无法覆盖新历史。
- 清理：识别框架当前与旧版 reset 标记，按事件实际群 ID 清空当前群历史。
- 新记录的 `time` 使用 `YYYY-MM-DD HH:MM:SS`（AstrBot 所在系统时区）；收到的消息优先使用框架消息时间，机器人发言使用发送时刻。
- 开启 `enable_real_name` 时优先通讯录姓名，失败后尝试群成员名称，再保留已有昵称或用户标识；占位姓名缓存不能阻止后续群成员名称解析。
- 兼容旧的纯时刻记录，不推测补填未知日期或姓名。

### 6.2 缓存
- 用户昵称缓存：LRU + TTL
- 群成员缓存：TTL（用于 mention 转换）
- 群信息缓存：TTL（用于上下文注入）
- mention 正则缓存：TTL
- 同名成员从自动提及映射中排除，ID -> 姓名映射仍用于昵称解析。
- 引用图片按消息/图片 key 复用，闲置 TTL 一小时，新增下载总预算 128 MiB；到期清理。

## 7. 用户记忆功能

记忆开关按类型独立控制：`enable_user_memory` 管理 instruction/preference/fact，`enable_meme_memory` 管理 meme；查询、删除、注入使用相同筛选。
- 记录用户偏好/事实/指令等记忆。
- 按群和用户维度管理。
- 受 `memory_max_per_user`、`memory_max_per_group` 限制。
- 每次请求按 `memory_inject_limit` 注入到 LLM 请求上下文。
- 修改采用副本提交，仅在原子落盘成功后更新缓存；写入失败不返回成功，不覆盖已有有效文件。
- 新增记忆仅合并完全相同的内容，不用子串覆盖完整事实；拒绝空内容。

## 8. 配置项（与 `_conf_schema.json` 对齐）

| Key | Type | Default | 说明 |
|---|---|---:|---|
| `enable_real_name` | bool | `true` | 启用真实姓名转换 |
| `enable_quoted_content` | bool | `true` | 启用引用消息增强 |
| `enable_group_info` | bool | `true` | 注入群名称/描述到上下文 |
| `enable_streaming_card` | bool | `false` | 优先保留原生 CardKit，旧版启用插件流式卡片（需同时开启 AstrBot 流式输出） |
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
| `debug_log_payload` | bool | `false` | 显式开启后以 DEBUG 级别输出完整请求；默认仅摘要 |

## 9. 开发规范

- 遵循 PEP 8，优先完整类型注解。
- 使用 `async/await`，避免阻塞 I/O。
- 日志统一使用 `[lark_enhance]` 前缀。
- 对外部 API 调用必须 `try-except` 包裹，避免影响主进程。
- 新增配置必须同时更新 `_conf_schema.json` 与本文档。

## 10. 手工测试清单

自动化验证见 `tests/README.md`：离线单元测试与真实框架集成脚本 `tests/framework_smoke.py` 分进程运行。后者使用真实加载器和事件/工具接口，仅模拟飞书 API 网络响应。

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
- 流式增强使用事件级观察器，保留框架原生类方法和普通发送回退路径。

