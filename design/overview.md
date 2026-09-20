# qi 总设计:架构总览、决策索引与运行时布局

> **这是设计记录,不是手册。** 现行行为以 [docs/](../docs/index.md) 手册为准;阶段计划与验收在
> [PLAN.md](PLAN.md);扩展机制的设计与决策在 [extensions-design.md](extensions-design.md)。
>
> 本文由根 README 拆出(2026-09):README 只留安装与快速开始,这里保留**架构总览、既定决策索引、
> 技术选型、运行时目录布局、安全总原则**。表中标 **v1** 的行是重构前的形状(保留用于对照,**勿据此
> 配置**);标 **v3** 的是现行形状。

## 1. 架构总览

```text
┌──────────────────────────────────────────────────────────────┐
│ 前端(同为 stream() 的 consumer,同进程)                          │
│   TUI(textual)· CLI(typer 一次性)· HTTP(qi-web 扩展,SSE/AG-UI) │
├──────────────────────────────────────────────────────────────┤
│ core 编排                                                      │
│   Runtime.stream() → AgentRunner(tool-loop)                   │
│   RunSpec = {name, prompt, tools} —— core 不认识"角色"          │
│   ExtensionBus(事件派发)· 压缩 / 标题 / 认证                    │
├──────────────────────────────────────────────────────────────┤
│ 内核                                                           │
│   ToolCatalog(内置 8 工具 + clarify)· Session(JSONL + 分支树)   │
│   LLMClient(models.json)· 技能 / 提示词基座 / 主题资源           │
├──────────────────────────────────────────────────────────────┤
│ 官方扩展(独立 pip 包,进程内)                                    │
│   qi-agents(角色 + subagent)· qi-mcp(MCP)· qi-web(HTTP + UI)     │
└──────────────────────────────────────────────────────────────┘
```

**一句话运行流**:用户输入 → `Runtime.stream()` → `AgentRunner` 的 tool-loop → 消息/事件写入
会话 JSONL → 前端消费同一事件流。

**与 v1 的差别(v3,P-E4c)**:v1 的图里还有一层「编排层 = Dispatcher + `AgentUnit`」。两者都已从
core 移除:core 只认 `RunSpec`,而且 `AgentRunner` **不再自己拼 prompt**(否则 `before_agent_start`
改过的那份会被重建覆盖)。「多 agent」由 qi-agents 以 **agent-as-tool** 提供。

## 2. 核心概念与既定决策

| 概念 | 结论 | 详见 |
| --- | --- | --- |
| agent | **内容**:自包含目录 `agents/<name>/`(`agent.md` + `skills/` + `mcp.json`);frontmatter + 正文即 system prompt。**v3 归 qi-agents** | [qi-agents README](../extensions/qi-agents/README.md) |
| agent 位置 | **2 处**:`~/.qi/agent/agents/` → `<项目>/.qi/agents/`(v1 还有"包内置 `general`"那层,P-E4c 删除);项目覆盖全局;同层重复报错 | [agent-config-design.md §3](agent-config-design.md) |
| agent.md 字段 | **v3 只有 `name` / `description` / `tools` / `model`**;`tools: None` = **继承父**(不是"全部":那会提权)。v1 还有 `display_name` / `keywords` / `include` / `opening` | [agent-config-design.md §4](agent-config-design.md) |
| 技能 | 私有自动绑定、渐进披露、无内置共享库;同一 agent 内同名报错 | [skills.md](../docs/skills.md) |
| MCP | 全局 `~/.qi/agent/mcp.json` + 项目 `.qi/mcp.json`(项目覆盖全局)+ 角色私有 `mcp.json`;角色能不能用由它的 `tools:` 里写什么决定(**默认拒绝**) | [qi-mcp README](../extensions/qi-mcp/README.md) |
| 数据源 | 设计上住在 agent `data_sources.json`(私有,凭证 env);**v3 无实现**(没有消费它的扩展) | [agent-config-design.md §9](agent-config-design.md) |
| 导入导出 | import = 拷贝 + 复用装载校验器 + 明文凭证扫描;export 产物可直接 import。**v3 未实现** | [agent-config-design.md §10](agent-config-design.md) |
| 工具 | 代码全局注册 ToolCatalog;**`tools` 三态**:省略或 `*` = 全部,名单 = allowlist;未知名报错 | [tools.md](../docs/tools.md) |
| 内置工具 | **8 个** + `clarify`:read / ls / find / grep / write / edit(diff 精确)/ bash(真 bash)/ powershell(仅 Windows) | [tools.md §2](../docs/tools.md) |
| 模型 | 全局 `models.json`:`defaultProvider` / `defaultModel`;v1 的 `routerProvider`(分派用)随 Dispatcher 一起取消;角色可以用 `model:` 覆盖 | [model-config.md](../docs/model-config.md) |
| 插件 → 扩展 | pip 包(entry point `qi.extensions`)+ 本地目录双通道;`register(api)`:工具 / 事件 / 命令 / 旗标 / 能力交接整套面(P-E1 改名) | [extensions.md](../docs/extensions.md) · [plugins.md](plugins.md) |
| 运行 | **单 agent**(core 不分派);多 agent 走 **agent-as-tool**(qi-agents);P-E4c 取消 auto 与 `--agent` | [cli.md](../docs/cli.md) · [extensions.md §7](../docs/extensions.md) |
| 命令 | 参数尽量对齐 pi:`qi [-p\|-c\|…] [--] [@files…] [msg…]` + 子命令 | [cli.md](../docs/cli.md) |
| Web | HTTP 宿主 + UI 都在 **qi-web** 扩展里(core 不内置);`qi web` 由扩展注册;不做外置 RPC 桥 | [web.md](web.md) · [qi-web README](../extensions/qi-web/README.md) |
| 配置形态 | 模型配置 = JSON `models.json`(分层:env → 项目 → 用户);应用设置与声明 = `settings.json` | [settings.md](../docs/settings.md) |
| 会话 | JSONL 每会话文件(entry 带 `type`);位置:**全局 `~/.qi/agent/sessions/`** | [sessions.md](../docs/sessions.md) · [session-format.md](../docs/session-format.md) |
| Dispatcher | v1:auto 分派(信号分层 L1 规则 / L2 embedding / L3 Router / L4 兜底 + `@` 点名,每轮重路由)。**v3 整体取消** | [dispatcher.md](dispatcher.md) |
| bash 策略 | **无命令级过滤**(对齐 pi);限制靠角色 `tools:` 白名单、CLI 的 `-t`/`-xt`/`-nt`/`-nbt`,或容器/VM;文件工具路径限会话目录 | [tools.md §4](../docs/tools.md) · [bash-allowlist.md](bash-allowlist.md) |
| clarify / denylist | `clarify` 是通用小工具(有前端时**真问人**);`disallowed_tools` 是角色级 denylist(支持通配) | [tools.md](../docs/tools.md) |

## 3. 技术选型

| 项 | 选择 | 状态 |
| --- | --- | --- |
| 语言 | Python ≥3.12 + asyncio | ✅ |
| 数据模型/校验 | **pydantic v2**(工具参数 schema / 配置校验) | ✅ |
| LLM 接入 | **litellm**(v1 的"或直连 openai/deepseek SDK"未采用) | ✅ |
| 会话存储 | JSONL 每会话文件(不选 SQLite:追加写 / 可读 / 零迁移) | ✅ |
| CLI | typer + rich(薄;**无独立 REPL**,交互 = TUI) | ✅ |
| TUI | textual(事件驱动增量渲染,渲染器 = event → lines) | ✅ |
| 配置 | `models.json`(格式对齐 pi)+ `settings.json`(分层深合并)+ pydantic 校验;凭证不入配置文件(auth store / env) | ✅ |
| Web | FastAPI + uvicorn —— **在 qi-web 扩展里**,core 不带这两个依赖 | ✅ |

> **litellm 的实测代价**:`import litellm` ≈ **6.8s**(2026-09,热缓存 3 次稳定)。这既是
> 子 agent 选**进程内**的量化依据,也是"别让每次 `qi -p` 都重付一遍"的理由 ——
> 见 [PLAN.md](PLAN.md) 与 [extensions-design.md §11.6](extensions-design.md)。

## 4. 目录布局(运行时)

全局侧比项目侧深一层,与 pi 同构:`~/.pi/agent/` ↔ `<项目>/.pi/`。全部用户级状态都挂在 `agent/` 下,
`~/.qi` 本身只是名字空间外壳(`QI_CONFIG_DIR` 可改)。

```text
~/.qi/                      # 名字空间根(空壳)
└── agent/                  # 全局(QI_AGENT_HOME 可覆盖此路径,对齐 PI_CODING_AGENT_DIR)
    ├── settings.json       # 默认模型 / skills 追加路径 / theme / packages 声明 …
    ├── models.json         # providers / 模型(格式对齐 pi)
    ├── auth.json           # 凭证(0600,按 provider)
    ├── SYSTEM.md           # 可选:整体替换默认基座(见 system-prompt.md §3 副作用)
    ├── AGENTS.md           # 可选:全局项目上下文(注入 <project_context>)
    ├── skills/<name>/      # 全局技能(qi 私有)
    ├── agents/<name>/      # 全局角色        —— 读它的扩展:qi-agents
    ├── extensions/<name>/  # 本地目录扩展通道(入口 extension.py;P-E1 从 plugins/ 改名)
    ├── mcp.json            # 全局 MCP 声明    —— 读它的扩展:qi-mcp
    └── sessions/*.jsonl    # 会话

<项目>/.qi/                 # 项目级(扁平;与 ~/.qi/agent 配对)
├── settings.json           # 覆盖全局(深合并;数组整体替换)
├── models.json
├── skills/  agents/  extensions/
├── mcp.json
└── SYSTEM.md

~/.agents/skills/           # Agent Skills 标准目录(.qi 的同级,跨工具共享)
```

> 旧版扁平布局(`~/.qi/models.json` 等)与 v0.1 的 `plugins/` 目录名在启动时**自动迁移**到
> `agent/` 与 `extensions/`,只在目标不存在时搬,绝不覆盖。
>
> 提示词的两个入口不要混:`SYSTEM.md` **整体替换**默认基座(项目 > 全局 > 代码内默认);
> `AGENTS.md` / `CLAUDE.md`(全局 `~/.qi/agent/` + 项目根及各级祖先,止于 git 根)则作为
> `<project_context>` **追加**。两者与角色层、技能、工作目录的推出顺序见
> [system-prompt.md](../docs/system-prompt.md)。

## 5. 安全总原则

- **bash 不筛命令**(对齐 pi):内置 bash 以 qi 进程权限执行任意命令。要收紧只有三条路 ——
  角色 `tools:` 里**只列需要的工具**(白名单)、本次运行用 CLI 收窄(`-nt` 全禁 / `-nbt` 只留
  扩展工具 / `-t` 白名单 / `-xt` 排除,见 [cli.md §1](../docs/cli.md)),或把进程放进容器/VM。
  进程内的半吊子过滤容易被误当成安全边界,这是**故意不做**的(要摘整类工具就用
  `disallowed_tools` / `-xt`,而不是去筛工具内部的子命令)
- 内容(技能 / 第三方角色)是**可执行指令**:先审后装,导入时提示
- **扩展代码 = 全权限**:仅可信源。项目级 `.qi/extensions/` 的**信任门控已实现**
  (`settings.defaultProjectTrust` = `ask`/`always`/`never`,`-a` / `-na` 单次表态);
  `ask` 的**交互式询问未实现**,所以它现在保守判**不信任**(fail-safe = 不加载)。项目级**角色**
  与 `.agents/skills` 的门控**尚未实现** —— 见 [extensions.md §5.3](../docs/extensions.md)
- 凭证三源:auth store(`~/.qi/agent/auth.json`,0600,按 provider)→ 约定环境变量 →
  `models.json` 的 `apiKey` 引用;配置文件与导入包**零明文**(导入时扫描)
- Web / 远程暴露需显式开启 + 鉴权(默认只绑回环)
