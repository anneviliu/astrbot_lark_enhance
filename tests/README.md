# 回归测试

在 Python 3.10+ 环境安装项目依赖后，从仓库根目录运行：

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

测试使用最小的 AstrBot API 替身和真实 `lark-oapi` 请求构造器，无需启动
AstrBot、配置飞书凭据或访问网络。55 项测试覆盖原生 CardKit 保留、事件级观察器、
旧版卡片与回退、工具分界、跨 token 提及、文件组件、完整日期与历史刷新、reset、
原子写入与失败回滚、记忆类型开关、姓名/引用解析、图片缓存、并发表情及日志开关。

## 真实框架集成验证

`framework_smoke.py` 必须在单独进程中运行，不使用单元测试中的 AstrBot 替身。
它使用真实 `PluginManager.load/reload`、`AstrBotConfig`、事件/工具装饰器、
`LarkMessageEvent`、`MessageChain`、`Plain/At/File`、`ProviderRequest`、真实
`RespondStage`、内置 `ConversationCommands.reset` 和原生
CardKit 发送方法。Lark 客户端和请求构造器也是真实 SDK，仅将飞书 API 网络调用替换为模拟响应。

已在 Python 3.12、lark-oapi 1.7.5 下验证：

| AstrBot | 验证范围 |
| --- | --- |
| v4.27.2 | 加载、配置、注册、事件调用、持久化、实际流式管线与提及/文件/回退、reset、热重载 |
| v4.29.4 | 同上 |

复现方式（以下以 v4.29.4 为例，两个版本的框架均要求 Python 3.12+）：

```sh
git clone --depth 1 --branch v4.29.4 https://github.com/AstrBotDevs/AstrBot.git /tmp/astrbot-compat
python3.12 -m venv /tmp/astrbot-compat-env
/tmp/astrbot-compat-env/bin/python -m pip install -r /tmp/astrbot-compat/requirements.txt
PYTHONPATH=/tmp/astrbot-compat /tmp/astrbot-compat-env/bin/python tests/framework_smoke.py
```

脚本会创建临时运行目录，禁用指标上传，不启动平台连接、LLM 或 WebUI。
真实框架测试与离线单元测试分别运行，避免替身模块掩盖接口兼容问题。热重载测试会故意留下待写数据，
由生产 terminate 路径落盘，并验证旧退出回调无法再覆盖新历史；不在测试中代替生产流程注销回调。

测试不能替代真实飞书环境的体验验证。上线前请在 AstrBot 中确认：

1. 使用原生 CardKit 的版本（如 4.27.2）开启插件流式卡片后，普通回复与工具调用后的流式回复均正常，
   `@名字` 能提及唯一成员，`group_history.json` 中包含最终机器人正文。
2. 收到跨日期群消息后，`group_history.json` 的新记录包含完整日期及可获取的姓名。
3. 机器人回复使用当前完整日期；关闭姓名转换后保留原昵称，关闭历史记录后不新增记录。
4. 执行 `/reset` 后旧历史不再注入；重载后待写历史保留；卡片权限不足仍能收到普通回复和提及。

