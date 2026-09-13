# TUI(交互界面)

> 状态:设计草案(P7 实现)。本文只放交互形态:`/` 内部命令、布局、消息队列行为。与 [cli.md](cli.md) 区分:CLI = 一次性命令;TUI = 交互命令。
> 相关:[dispatcher.md](dispatcher.md)(分派卡片渲染)、[web.md](web.md)(v2 同款视图)。

## 1. 布局(草案,参考 pi)

```text
┌──────────────────────────────────────────────┐
│ header: qi · session名 · [AUTO|MANUAL] · agent │
├──────────────────────────────────────────────┤
│ 消息流(message_list)                           │
│   · 用户气泡                                   │
│   · dispatch 卡片:→ code-analyst 置信度 0.93    │
│     (理由 + source:router|rules|sticky|…)      │
│   · agent 消息 + 工具执行卡片(折叠)              │
├──────────────────────────────────────────────┤
│ status_bar: 当前 agent / 模型 / spinner        │
│ input_bar: 输入 + / 命令补全                    │
└──────────────────────────────────────────────┘
```

- 渲染器 = 纯函数 `event → lines`,消息 widget 增量 patch(流式友好)
- opening:`message` 进历史;suggestions 为输入框上方快捷 chips(点击提交,不进历史)

## 2. `/` 内部命令(草案)

### 会话与恢复(对齐 pi)

| 命令 | 说明 |
| --- | --- |
| `/resume` | 选历史会话恢复 |
| `/new` | 新会话 |
| `/name <name>` | 会话显示名 |
| `/session` | 会话信息(文件/ID/消息数) |
| `/fork` | 从某条历史消息 fork 新会话 |
| `/export [file]` | 导出 HTML/JSONL |
| `/import <file>` | 从 JSONL 导入并恢复 |
| `/trust` | 保存项目 `.qi` 信任决定 |
| `/quit` | 退出 |

### Agent 与分派(qi 特色)

| 命令 | 说明 |
| --- | --- |
| `/agents` | 列出 agent(可 `@name` 直派) |
| `/mode auto\|manual` | 切换分派模式 |
| `/agent <name>` | manual 下锁定执行 agent |
| `/todos` | 预留(二期 todo 工作流) |

### 检查与维护

| 命令 | 说明 |
| --- | --- |
| `/help` | 命令帮助(含快捷键) |
| `/tools` | 当前 agent 的工具清单 |
| `/skills` | 当前 agent 绑定的技能 |
| `/doctor` | 装载/配置诊断 |
| `/reload` | 重载 agents/plugins/配置(改 agent.md 即生效) |

### 对齐 pi 但砍掉/改造的

| pi 命令 | qi 处理 |
| --- | --- |
| `/login` | 选 provider,写入 auth store(`~/.qi/agent/auth.json`,0600) |
| `/logout` | 清除某 provider 凭证 |
| `/model /thinking /scoped-models /settings` | 配置化:改 `models.json`,不进 TUI |
| `/llama /share /tree /clone /changelog` | 无对应能力(v2 按需,如 `/share` 随 web v2) |
| `/hotkeys` | 并入 `/help` |
| `/compact` | 等会话摘要设计(v2) |

## 3. 消息队列行为(对齐 pi)

- **Enter**:排队 steering 消息,当前轮工具执行完后送达
- **Alt+Enter**:follow-up,agent 全部工作完成后送达
- **Esc**:中止当前轮,队列消息退回编辑器
- **Alt+Up**:取回排队消息

## 4. 扩展点(v2)

- 插件可注册自定义 TUI 命令(对齐 pi `registerCommand`)
- web v2 视图复用同一事件流与渲染结构(web.md)
