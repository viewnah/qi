# 配置:qi 目录与上下文文件

qi 的配置分**用户级**与**项目级**两层。

- 用户级 = **agent 目录**,默认 `~/.qi/agent`(`QI_AGENT_HOME` 可改),所有用户级状态都挂在这里。
- 项目级 = 工作目录下的 `.qi/`,跟仓库走、可提交共享。

项目级**扩展**要过[项目信任](security.md)才加载;`settings.json` 的普通字段(`sessionDir`、`theme`、
`defaultTools`…)与文本指令(`AGENTS.md`、技能)**不受门控**。

改完手动编辑的文件(设置、指令、资源)后,在 TUI 里 `/reload` 一次。

## agent 目录(用户级)

下表里写成 `<agent-dir>`:

| 路径 | 职责 |
| --- | --- |
| `<agent-dir>/settings.json` | 用户级[设置](settings.md):偏好、默认值、资源路径、扩展声明(`packages`) |
| `<agent-dir>/models.json` | provider 与模型定义、`apiKey` 引用 → [models.md](models.md) |
| `<agent-dir>/auth.json` | 凭证(0600,按 provider)→ [providers.md](providers.md) |
| `<agent-dir>/trust.json` | 按目录记住的信任决定(`/trust` 写它) |
| `<agent-dir>/sessions/` | 会话 JSONL(`settings.sessionDir` 可改)→ [sessions.md](sessions.md) |
| `<agent-dir>/skills/` | 用户级[技能](skills.md) |
| `<agent-dir>/extensions/` | 用户级[扩展](extensions.md),一目录一个扩展 |
| `<agent-dir>/SYSTEM.md` | **整体替换**默认基座(见下) |
| `<agent-dir>/AGENTS.md`(或 `AGENTS.override.md` / `CLAUDE.md`) | 跨工作目录的用户指令 |
| `<agent-dir>/agents/`、`<agent-dir>/mcp.json` | **官方扩展自己的目录**:角色(qi-agents)与 MCP server(qi-mcp)—— 没装对应扩展就是普通目录 |

## 项目 `.qi` 目录

| 路径 | 职责 |
| --- | --- |
| `.qi/settings.json` | 项目级设置(键级深合并覆盖用户级) |
| `.qi/models.json` | 项目级 provider / 模型定义(优先于用户级) |
| `.qi/SYSTEM.md` | **整体替换**本项目里的基座 |
| `.qi/extensions/` | 项目级扩展(**信任后才加载**) |
| `.qi/skills/` | 项目级技能 |
| `.qi/agents/`、`.qi/mcp.json` | 同上:官方扩展自己的目录与声明 |

`SYSTEM.md` 有项目级就用项目级(同名文件**不合并**)。

## 上下文文件

上下文文件与 `.qi/` 配置是两回事:qi 从 **agent 目录**、**工作目录**以及**它的祖先目录**里找,
在某个目录或其子目录里跑都生效。每级目录按候选顺序取**第一个**命中:

```text
AGENTS.override.md > AGENTS.md > AGENTS.MD > CLAUDE.md > CLAUDE.MD
```

顺序 = 用户级 → 项目祖先链**由远到近**(含 cwd),按路径去重;祖先链**止于 git 根**。
`AGENTS.override.md` 只在**同一目录**里顶替 `AGENTS.md` / `CLAUDE.md`,不会压掉其它目录的。
空文件视为未配置。上下文文件的发现**不需要项目信任**。

注入位置在 `<project_context>` 块里,是系统提示词的一部分(见 [how-qi-works.md](how-qi-works.md))。

## 换掉或追加系统提示词

| 入口 | 语义 | 落盘 |
| --- | --- | --- |
| `.qi/SYSTEM.md` / `<agent-dir>/SYSTEM.md` | **整体替换**基座(项目 > 用户) | 是(仓库 / 用户文件) |
| `qi --system-prompt "<文本\|文件>"` | **整体替换**基座(只本次运行) | 否 |
| `qi --append-system-prompt "<文本\|文件>"` | **追加**到每回合提示词末尾(可重复) | 否 |
| 扩展的 `before_agent_start` | 改 `system_prompt`(链式) | 否 |

**副作用**:默认基座里的「可用工具」「指南」两块跟基座绑在一起,写了 `SYSTEM.md` 就等于接管基座、
那两块不再出现。写自定义基座时请自己交代工具约定,例如:

```markdown
你是本仓库(订单服务)的开发助手。

可用工具:`read` / `ls` / `find` / `grep` / `edit` / `write` / `bash` / `clarify`。
- `bash` 不做命令级过滤,行为由本仓库约定约束。
- 需求不明确时先用 `clarify` 提问。

## 本仓库环境
- 跑测试:`uv run pytest -q`
- 数据库迁移在 `migrations/`,只允许新增文件。
```

**动态块不受影响**:项目上下文、技能清单、手册索引、工作目录照旧追加 —— 不是"整条提示词只剩你的文件"。
(实现细节见 [design/internals.md](../design/internals.md)。)

## qi 没有的配置项

- **prompt 模板**:qi 不做可复用提示词模板;要固定前缀就写进 `SYSTEM.md`,或做成技能。
- **键位配置文件**:qi 没有 `keybindings.json`;键位固定,扩展可用 `register_shortcut` 加自己的。见 [keybindings.md](keybindings.md)。
- **用户主题目录**:只有内置 `dark` / `light`,`theme` 只认这三个值(`auto` 是探测)。见 [themes.md](themes.md)。
