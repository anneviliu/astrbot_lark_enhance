# 回归测试

在 Python 3.10+ 环境安装项目依赖后，从仓库根目录运行：

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

测试使用最小的 AstrBot API 替身和真实 `lark-oapi` 请求构造器，无需启动
AstrBot、配置飞书凭据或访问网络。覆盖原生 CardKit 保留、旧版补丁与回退、
完整日期持久化、旧数据加载和提示词注入，以及通讯录/群成员姓名查询降级。

## 真实框架集成验证

`framework_smoke.py` 必须在单独进程中运行，不使用单元测试中的 AstrBot 替身。
它使用真实 `PluginManager.load/reload`、`AstrBotConfig`、事件/工具装饰器、
`LarkMessageEvent`、`MessageChain`、`Plain/At`、`ProviderRequest` 和原生
CardKit 发送方法。Lark 客户端和请求构造器也是真实 SDK，仅将飞书 API 网络调用替换为模拟响应。

已在 Python 3.12、lark-oapi 1.7.5 下验证：

| AstrBot | 验证范围 |
| --- | --- |
| v4.27.2 | 加载、配置、注册、事件调用、历史持久化、原生流式与回退、热重载 |
| v4.29.4 | 同上 |

复现方式（以下以 v4.29.4 为例，两个版本的框架均要求 Python 3.12+）：

```sh
git clone --depth 1 --branch v4.29.4 https://github.com/AstrBotDevs/AstrBot.git /tmp/astrbot-compat
python3.12 -m venv /tmp/astrbot-compat-env
/tmp/astrbot-compat-env/bin/python -m pip install -r /tmp/astrbot-compat/requirements.txt
PYTHONPATH=/tmp/astrbot-compat /tmp/astrbot-compat-env/bin/python tests/framework_smoke.py
```

脚本会创建临时运行目录，禁用指标上传，不启动平台连接、LLM 或 WebUI。
真实框架测试与上述 22 项离线单元测试分别运行，避免替身模块掩盖接口兼容问题。

测试不能替代真实飞书环境的体验验证。上线前请在 AstrBot 中确认：

1. 使用原生 CardKit 的版本（如 4.27.2）开启插件流式卡片后，日志出现
   `Native CardKit streaming detected`，普通回复与工具调用后的流式回复均正常。
2. 收到跨日期群消息后，`group_history.json` 的新记录包含完整日期及可获取的姓名。
3. 机器人回复使用当前完整日期；关闭姓名转换后保留原昵称，关闭历史记录后不新增记录。
