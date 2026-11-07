# 回归测试

在 Python 3.10+ 环境安装项目依赖后，从仓库根目录运行：

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

测试使用最小的 AstrBot API 替身和真实 `lark-oapi` 请求构造器，无需启动
AstrBot、配置飞书凭据或访问网络。覆盖原生 CardKit 保留、旧版补丁与回退、
完整日期持久化、旧数据加载和提示词注入，以及通讯录/群成员姓名查询降级。

测试不能替代真实飞书环境的体验验证。上线前请在 AstrBot 中确认：

1. 使用原生 CardKit 的版本（如 4.27.2）开启插件流式卡片后，日志出现
   `Native CardKit streaming detected`，普通回复与工具调用后的流式回复均正常。
2. 收到跨日期群消息后，`group_history.json` 的新记录包含完整日期及可获取的姓名。
3. 机器人回复使用当前完整日期；关闭姓名转换后保留原昵称，关闭历史记录后不新增记录。
