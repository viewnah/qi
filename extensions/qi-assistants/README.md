# qi-assistants

> **qi-lab 内置扩展**：安装 `qi-lab` 时已随包提供，无需单独安装。下面的安装 / CLI 用法是它作为独立扩展时的路径；提取发布后仍按这些步骤使用。

qi 的**应用助手目录**：一份项目级的 `assistants.json`，加上给模型用的两个只读工具。

它只做三件事：

1. 读 / 写 / 校验 `<cwd>/.qi/assistants.json`（唯一事实源）；
2. 给 qi-lab 的 CRUD 端点提供同一份读写实现；
3. 给模型提供 `assistant_search` / `assistant_recommend` 两个工具。

**它不连接助手页面。** `url` 就是一个外链，用户点了新标签打开，qi 不跟目标系统交换任何数据。

## 装

```bash
# 开发：本次运行临时加载（改完即生效）
qi -e ./extensions/qi-assistants

# 或让每次启动都加载：软链/复制到用户扩展目录
ln -s "$PWD/extensions/qi-assistants" ~/.qi/agent/extensions/qi-assistants

# 或 pip 可编辑安装，再在 settings.packages 里声明 pip:qi-assistants
uv pip install -e ./extensions/qi-assistants

# 发布后
qi install qi-assistants
```

目录通道不需要 entry point；pip 通道由 `qi.extensions` 自动发现。
**没装时 qi-lab 的能力位 `assistant_write` 为 false，设置页那一节只读。**

## 文件

```json
{
  "version": 1,
  "assistants": [
    {
      "id": "as_7f3c1a2b",
      "name": "评估方案生成助手",
      "description": "意图分析、评估要素人在环确认，分部分流式生成评估方案。",
      "tasks": ["意图分析", "评估要素人在环确认", "分部分流式生成"],
      "outputs": ["评估方案文档"],
      "url": "https://eval.example.com/assistants/plan",
      "keywords": ["方案", "指标"],
      "enabled": true
    }
  ]
}
```

| 字段 | 必填 | 约束 |
| --- | --- | --- |
| `id` | ✅ | `as_` + 8 位 hex，服务端生成，不可改 |
| `name` | ✅ | 1–40 字，全目录唯一 |
| `description` | ✅ | 1–400 字 |
| `tasks` | ✅ | 1–8 条，每条 1–60 字 |
| `outputs` | ✅ | 1–8 条，每条 1–60 字 |
| `url` | ✅ | 只接受 `http(s)://` 绝对地址 |
| `keywords` |  | 0–12 条，只用于召回，不展示 |
| `enabled` |  | `true` / `false`，默认 `true` |

**只有 `enabled: true` 会被推荐。** 模型拿不到 `url`：链接由 `assistant_recommend`
从目录回填，模型只负责选 `id`。

## 工具

| 工具 | 作用 |
| --- | --- |
| `assistant_search` | 按任务描述召回候选（不含 URL）；空 query = 列出全部 |
| `assistant_recommend` | 校验选中的 `id`，输出卡片数据（含 URL）与 `details["ui"]` |

推荐策略（一次、不追问）写在角色里，见 `examples/agents/training-evaluator/agent.md`。

## 与 qi-lab 的关系

- qi-lab 的 `GET/POST/PATCH/DELETE /api/assistants` 与 `POST /api/assistants/import`
  调的就是本包的 `catalog.py`；
- 没装本包时 qi-lab 的能力位 `assistant_write` 为 `false`，设置页那一节只读；
- 两边读的是同一个文件，改完下一轮工具调用立刻生效（不缓存）。
