# 用户记忆功能设计方案

## 1. 功能概述

让 LLM 能够"记住"群内用户的偏好、习惯和重要信息，实现个性化交互。

### 目标
- 记住用户告知的偏好（"叫我小王"、"我喜欢简洁的回复"）
- 记住用户的重要信息（"我是产品经理"、"我负责 X 项目"）
- 同一用户在不同群的记忆完全隔离
- 支持用户主动管理自己的记忆（查看、一键清除）

### 范围限定
- **仅群聊场景**，不考虑私聊
- **群级隔离**：用户 A 在群 1 的记忆与群 2 完全独立

---

## 2. 记忆模型设计

### 2.1 记忆类型

| 类型 | 说明 | 示例 |
|------|------|------|
| `preference` | 用户偏好 | 称呼、回复风格、语言 |
| `fact` | 用户事实 | 职业、负责项目、技能 |
| `instruction` | 持久指令 | "回复我时用英文"、"不要用表情" |

### 2.2 记忆结构

```python
@dataclass
class UserMemory:
    id: str                  # 唯一标识 (UUID)
    type: str                # preference / fact / instruction
    content: str             # 记忆内容（自然语言描述）
    created_at: float        # 创建时间戳
    updated_at: float        # 更新时间戳
```

### 2.3 存储结构

按群隔离存储，每个群一个文件：

```
data/
  user_memory/
    {group_id}.json          # 每个群一个文件
```

文件格式：
```json
{
  "group_id": "oc_xxx",
  "users": {
    "ou_user1": {
      "memories": [
        {
          "id": "uuid-1",
          "type": "preference",
          "content": "希望被称呼为「小王」",
          "created_at": 1700000000,
          "updated_at": 1700000000
        },
        {
          "id": "uuid-2",
          "type": "fact",
          "content": "是产品经理，负责用户增长项目",
          "created_at": 1700000001,
          "updated_at": 1700000001
        }
      ]
    },
    "ou_user2": {
      "memories": [...]
    }
  },
  "updated_at": 1700000001
}
```

---

## 3. 工具设计

### 3.1 记忆保存工具

```python
@filter.llm_tool(name="lark_save_memory")
async def lark_save_memory(
    self,
    event: AstrMessageEvent,
    memory_type: str,    # preference / fact / instruction
    content: str         # 记忆内容
):
    """保存当前群内用户的记忆。当用户明确要求记住某些信息时使用。

    注意：记忆仅在当前群生效，不会影响用户在其他群的交互。

    Args:
        memory_type: 记忆类型
            - preference: 用户偏好（称呼、回复风格等）
            - fact: 用户事实（职业、项目、技能等）
            - instruction: 持久指令（总是用英文回复、不要用表情等）
        content: 要记住的内容，用简洁的陈述句描述
    """
```

### 3.2 记忆查询工具

```python
@filter.llm_tool(name="lark_list_memory")
async def lark_list_memory(
    self,
    event: AstrMessageEvent
):
    """查询用户在当前群的所有记忆。当用户询问"你记得我什么"时使用。

    返回该用户在当前群的所有记忆列表。
    """
```

### 3.3 记忆删除工具

```python
@filter.llm_tool(name="lark_forget_memory")
async def lark_forget_memory(
    self,
    event: AstrMessageEvent,
    target: str = "all"
):
    """删除用户在当前群的记忆。

    Args:
        target: 删除目标
            - "all": 一键清除所有记忆
            - 具体描述: 删除匹配的记忆（如"关于称呼的"、"关于职业的"）
    """
```

---

## 4. Prompt 注入策略

### 4.1 注入位置和格式

在 `on_llm_request` 中，群聊场景下注入用户记忆：

```
[输出格式要求]
...

[关于当前用户的记忆]
- 希望被称呼为「小王」（偏好）
- 是产品经理，负责用户增长项目（事实）
- 偏好简洁直接的回复风格（偏好）

[当前群组信息]
...
```

### 4.2 注入规则

- 仅注入当前群内、当前用户的记忆
- 最多注入 10 条记忆
- 优先级：`instruction` > `preference` > `fact`
- 同优先级按 `updated_at` 降序

---

## 5. 配置项

在 `_conf_schema.json` 中新增：

```json
{
  "enable_user_memory": {
    "type": "bool",
    "default": true,
    "description": "是否启用用户记忆功能"
  },
  "memory_max_per_user": {
    "type": "int",
    "default": 20,
    "description": "每用户每群最大记忆条数"
  },
  "memory_inject_limit": {
    "type": "int",
    "default": 10,
    "description": "每次请求最多注入的记忆数"
  }
}
```

---

## 6. 使用示例

### 设置记忆
```
用户：记住，在这个群叫我老王
助手：好的老王，我记住了！在这个群会这样称呼你。

用户：我是这个项目的后端负责人
助手：收到，我记下了你是这个项目的后端负责人。
```

### 记忆生效
```
用户：帮我看看这个代码
助手：好的老王，我来看看...（自动使用记忆中的称呼）
```

### 查看记忆
```
用户：你记得我什么？
助手：在这个群里，我记得关于你的这些信息：
      - 希望被称呼为「老王」
      - 是这个项目的后端负责人
```

### 清除记忆
```
用户：忘掉你记得的关于我的所有东西
助手：好的，我已经清除了在这个群里关于你的所有记忆。
```

---

## 7. 实现清单

### Phase 1: 核心功能
- [ ] 记忆存储类 `UserMemoryStore`
  - [ ] 加载/保存群记忆文件
  - [ ] 添加记忆（带去重）
  - [ ] 查询用户记忆
  - [ ] 删除记忆（支持全部/匹配）
- [ ] 工具实现
  - [ ] `lark_save_memory`
  - [ ] `lark_list_memory`
  - [ ] `lark_forget_memory`
- [ ] Prompt 注入
  - [ ] 在 `on_llm_request` 中注入用户记忆
- [ ] 配置项
  - [ ] 更新 `_conf_schema.json`

---

## 8. 数据示例

用户 A 在群 1 说"叫我小王"，在群 2 说"叫我王总"：

**群 1 文件** (`oc_group1.json`):
```json
{
  "group_id": "oc_group1",
  "users": {
    "ou_userA": {
      "memories": [
        {"type": "preference", "content": "希望被称呼为「小王」", ...}
      ]
    }
  }
}
```

**群 2 文件** (`oc_group2.json`):
```json
{
  "group_id": "oc_group2",
  "users": {
    "ou_userA": {
      "memories": [
        {"type": "preference", "content": "希望被称呼为「王总」", ...}
      ]
    }
  }
}
```

→ 用户 A 在群 1 被叫"小王"，在群 2 被叫"王总"，互不干扰。

---

确认此方案后我开始实现。
