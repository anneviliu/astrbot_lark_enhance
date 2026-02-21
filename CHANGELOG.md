# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] - 2026-02-21

### Added
- **模块化目录结构**：新增 `lark_enhance/handlers`、`lark_enhance/mixins`、`lark_enhance/stores`、`lark_enhance/services`，将核心能力按职责拆分。
- **事件与工具处理器拆分**：新增 `lark_enhance/handlers/events.py` 与 `lark_enhance/handlers/tools.py`，承载主要业务逻辑。

### Changed
- **主入口瘦身**：`main.py` 从“大一统实现”调整为“装配 + 装饰器入口转调”模式，降低耦合，保留 AstrBot 插件加载兼容性。
- **工具参数文档补齐**：在 `lark_emoji_reply` / `lark_save_memory` / `lark_list_memory` / `lark_forget_memory` 中补全 `Args` 注释，确保框架可正确解析工具参数 schema。
- **版本号升级**：插件版本更新至 `0.3.1`。

### Fixed
- **插件导入路径兼容**：修复重载时 `No module named 'lark_enhance'` 的导入失败问题。
- **事件钩子绑定异常**：修复 `on_message_sent` / `on_llm_request` / `on_decorating_result` 出现 `missing positional argument` 的绑定错误。
- **mention 正则缓存报错**：修复 `NameError: name 'time' is not defined`。
- **工具调用参数丢失**：修复 `lark_emoji_reply` 在工具执行阶段出现 `emoji` 参数被忽略和参数不匹配的问题。

## [0.3.0] - 2026-02-06

### Added
- **引用图片多模态理解**：当用户引用的消息包含图片时，插件会下载引用图片并注入到 `req.image_urls`，交由 AstrBot 当前配置的多模态模型直接理解。
- **引用图片上下文增强**：在引用前缀中补充“引用消息包含图片”的提示，提升模型对多模态输入的对齐。

### Changed
- **流式卡片开关恢复**：恢复 `enable_streaming_card` 配置项，默认 `false`，仅在开启时注入 streaming patch。
- **版本号升级**：插件版本更新至 `0.3.0`。

### Removed
- **上下文清洗功能下线**：移除 `tool_calls` / `tool_calls_result` 清洗与转换逻辑。
- **无效配置移除**：删除 `enable_context_cleaner` 与 `force_tool_calls_cleanup` 配置项。

## [0.2.0] - 2025-01-26

### Added
- **用户记忆系统**：新增按群隔离的用户记忆功能
  - 支持三种记忆类型：`instruction`（持久指令）、`preference`（用户偏好）、`fact`（用户事实）
  - LLM 工具：`lark_save_memory`、`lark_list_memory`、`lark_forget_memory`
  - 记忆自动注入到 LLM prompt 中
  - 持久化存储到本地 JSON 文件
- **LLM 请求日志**：添加 info 级别日志，打印输入给 LLM 的完整内容（system prompt、contexts、current prompt）
- 新增配置项：
  - `enable_user_memory`：是否启用用户记忆功能（默认 true）
  - `memory_inject_limit`：每次注入的用户记忆数量上限（默认 10）
  - `memory_max_per_user`：每个用户的最大记忆条数（默认 20）

### Changed
- **记忆存储重构**：移除有问题的防抖机制，改为每次操作立即持久化，避免数据丢失风险
- **缓存优化**：`UserMemoryStore` 使用 LRU 缓存，限制最大缓存 100 个群，防止内存无限增长

### Fixed
- 修复 `@mention` 周围的 Markdown 格式问题（如 `**@名字**` 现在会正确转换）
- 修复 At 组件周围多余空白和换行的问题

## [0.1.0] - 2025-01-23

### Added
- 初始版本发布
- 真实姓名解析：自动将 OpenID 解析为飞书通讯录中的真实昵称
- 引用消息穿透：自动抓取用户引用的消息内容注入到 Prompt
- 群聊上下文感知：维护群聊消息记录并持久化存储
- 流式卡片输出：使用飞书消息卡片实现打字机效果
- @ 提及转换：将 LLM 回复中的 @名字 转换为飞书原生 @ 提及
- 原生表情回复工具：LLM 可使用飞书原生表情回复消息
- 上下文清洗：自动清洗历史上下文中的工具调用记录
