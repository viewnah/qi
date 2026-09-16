# qi:多 Agent 编码框架(总设计)

> 目标:用 Python 实现一个「类似 pi」的多 agent 编码框架——专职角色 agent + **auto 模式**(Dispatcher 自动分派)。参考 hikqin_sdk 的设计(不依赖它),独立实现。
> **开发计划见 [docs/PLAN.md](docs/PLAN.md)**;本文是设计总入口:文档索引、架构总览、决策索引、技术选型与目录布局。

## 快速开始

```bash
# 1. 引导配置默认模型(交互:上下键选 provider/模型 → 写 models.json + settings.json + auth.json)
qi init

# 非交互(CI / 脚本)
qi init -y --provider deepseek --model deepseek-chat \
    --base-url https://api.deepseek.com/v1 --api openai-completions --api-key sk-xxx

# 2. 校验配置与凭证
qi doctor

# 3. 运行:裸 `qi` 直接进 TUI(交互,auto 分派);`-p` 是无头一次执行
qi
qi -p "分析这个仓库"

# TUI 视觉/配色与 pi 同源(theme/dark.json、light.json),主题默认跟随终端背景
# (`qi config --set theme=dark|light|auto`,或 `QI_THEME=light qi`)
qi "分析这个仓库"          # 进 TUI 并把这句话作为首条发出(对齐 pi 的 `pi "问题"`)
```

零配置即可跑:框架内置了一个 `general`(兜底角色)与一份**基座系统提示词**,不需要先装任何 agent。
想定制角色就装自己的 agent;想定制基座提示词就写 `.qi/SYSTEM.md`(见第 4 步)。

```bash
# 4.(可选)装专职 agent 与自定义基座提示词
qi agents import examples/agents/code-analyst   # 装专职角色(默认落在 ~/.qi/agent)
vim .qi/SYSTEM.md                              # 项目级基座提示词(可提交共享)
```

优先级:**项目 `.qi/agents/` > 用户 `~/.qi/agent/agents/` > 包内置**;**项目 `.qi/SYSTEM.md` > `~/.qi/agent/SYSTEM.md` > 包内置**。
详见 [docs/system-prompt.md](docs/system-prompt.md) 与 [docs/agent-config.md](docs/agent-config.md)。

`qi init` 交互流程与样式复刻 QwenPaw `init`:**Provider Configuration**(选已有/新建 → Base URL → API 类型 → API Key)→ **Add Models**(`Add a model?` 循环,含 reasoning/contextWindow/maxTokens)→ **Activate LLM Model**(选 provider → 选 model)。完整用法、选项与示例见 [docs/model-config.md §7](docs/model-config.md#7-qi-init-用法)。

## 1. 文档索引

| 文档 | 内容 |
| --- | --- |
| [PLAN.md](docs/PLAN.md) | 开发计划(v1/v2 阶段)与未决清单 |
| [docs/agent-config.md](docs/agent-config.md) | agent = 自包含目录(agent.md / skills / assets / mcp.json / data_sources.json)、装载校验、MCP、数据源、导入导出 |
| [docs/system-prompt.md](docs/system-prompt.md) | 系统提示词:基座层(内置 `SYSTEM.md` + 可选覆盖)+ 角色层(agent.md)分层与优先级 |
| [docs/tools.md](docs/tools.md) | ToolCatalog、内置 7 工具、tools 三态、bash 策略(与 pi 对齐:无命令级过滤) |
| [docs/bash-allowlist.md](docs/bash-allowlist.md) | bash 策略**变更记录**:为何删掉首词白名单、与 pi(v0.85.1)的对照、可绕过的四种写法 |
| [dispatcher.md](docs/dispatcher.md) | auto 模式:信号分层、分派管线、Router 契约、优先级 |
| [docs/model-config.md](docs/model-config.md) | `models.json`(对齐 pi:`providers` / `baseUrl` / `api` / `models`);默认模型 default / 分派 router;凭证 auth store + 约定 env + apiKey 引用 |
| [docs/settings.md](docs/settings.md) | `settings.json`(对齐 pi):两级分层深合并、字段清单(含“仅存储未生效”清单)、资源路径与排除项、`qi config` |
| [docs/plugins.md](docs/plugins.md) | 插件机制:pip(entry point)+ 本地目录双通道、消费型配置动态装载 |
| [docs/cli.md](docs/cli.md) | 命令面(参数尽量对齐 pi) |
| [docs/tui.md](docs/tui.md) | TUI 交互:`/` 命令草案、布局、消息队列(与 cli.md 区分) |
| [docs/web.md](docs/web.md) | v2:HTTP 宿主在框架 + UI 插件化 |
| [examples/agents/](examples/agents/) | code-analyst 完整样例(agent.md + assets + skills + mcp.json) |

## 2. 架构总览

```
┌──────────────────────────────────────────────────────────┐
│ 前端(同为 stream() 的 consumer,同进程)                      │
│   TUI(textual)· CLI(typer 一次性)· HTTP(v2,SSE)           │
├──────────────────────────────────────────────────────────┤
│ 编排层                                                     │
│   Runtime.stream() ── @点名 ── Dispatcher(每轮重新路由)      │
│        │                                      │           │
│        ▼                                      ▼           │
│   AgentUnit(装载自 .qi/agents/)       Router-LLM(L1/L3/L4)│
├──────────────────────────────────────────────────────────┤
│ 内核                                                       │
│   AgentRunner(tool-loop)· ToolCatalog · Session(JSONL)     │
│   LLMClient(models.json)· 事件总线                          │
└──────────────────────────────────────────────────────────┘
```

**一句话运行流**:用户输入 → (auto)Dispatcher 选 agent → 该 agent 的 tool-loop 执行 → 消息/事件写入会话 JSONL → 前端消费同一事件流。

## 3. 核心概念与既定决策

| 概念 | 结论 | 详见 |
| --- | --- | --- |
| agent | **内容**:自包含目录 `agents/<name>/`(agent.md 定义,frontmatter + 正文即 system prompt) | agent-config.md |
| agent 位置 | 仅 2 处:`~/.qi/agent/agents/` + `<项目>/.qi/agents/`;项目静默覆盖全局;同层重复报错;无内置 | agent-config.md §3 |
| agent.md 字段(v1) | name / display_name / description(路由信号)/ keywords / tools / include / opening | agent-config.md §4 |
| 技能 | 私有自动绑定、渐进披露、无内置、无共享库;同一 agent 内同名报错 | agent-config.md §5 |
| MCP(v1) | 私有 mcp.json(仅本 agent,凭证 env)+ 全局 `[mcp.servers]` 按 `mcp_servers` 绑定 | agent-config.md §8 |
| 数据源 | 实例在 agent `data_sources.json`(私有,凭证 env);**插件消费型配置**:插件缺席不装载 | agent-config.md §9 / plugins.md |
| 导入导出 | import = 拷贝 + 复用装载校验器 + 明文凭证扫描;export 产物可直接 import | agent-config.md §10 |
| 工具 | 代码全局注册 ToolCatalog;**tools 三态**:省略或 `*` = 全部,名单 = allowlist;未知名报错 | tools.md |
| 内置工具(v1) | 7 个:read / ls / find / grep / write / edit(diff 精确)/ bash(对齐 pi,去 powershell) | tools.md §2 |
| 模型 | 全局 `models.json`:`defaultProvider/defaultModel`(执行)/ `routerProvider/routerModel`(分派);agent 不声明模型 | model-config.md |
| 插件 | pip 包(entry point `qi.plugins`)+ 本地目录双通道;register():add_tool / provides_config / provides_types | plugins.md |
| 运行 | auto 默认(Dispatcher **每轮**分派,不做会话亲和);`--agent` manual;`@` 点名 | cli.md |
| 命令 | 参数尽量对齐 pi:`qi [-p\|-c\|-r\|…] [--] [@files…] [msg…]` + 子命令 | cli.md |
| Web(v2) | HTTP 宿主在框架(`qi web`),UI 插件化;不做外置 RPC 桥 | web.md |
| 配置形态 | agent 定义 = Markdown + frontmatter;模型配置 = JSON `models.json`(对齐 pi,分层:env → 项目 → 用户);应用设置/默认模型 = `settings.json` | settings.md |
| 会话 | JSONL 每会话文件(pi 风格,entry 带 type/agent_id);位置:**全局 `~/.qi/agent/sessions/`** |
| Dispatcher | auto:信号分层(L1 规则 / L2 embedding 默认关 / L3 Router 读 description / L4 兜底)+ @ 点名;**每轮都重新路由,无 sticky 沿用** |
| bash 策略 | **无命令级过滤**(对齐 pi);限制靠 `tools`/`disallowed_tools` 收窄,或容器/VM;文件工具路径限会话目录 | tools.md §4 / bash-allowlist.md |
| clarify / denylist | v1 内置 clarify 通用工具;`disallowed_tools` v1 |

## 4. 技术选型

| 项 | 选择 | 状态 |
| --- | --- | --- |
| 语言 | Python ≥3.12 + asyncio | ✅ |
| 数据模型/校验 | **pydantic v2**(AgentConfig/Tool 参数 schema/配置校验) | ✅ |
| LLM 接入 | litellm 统一 或 直连 openai/deepseek SDK | ⏳ 待定(倾向 litellm) |
| 会话存储 | JSONL 每会话文件(不选 SQLite:追加写/可读/零迁移) | ✅ |
| CLI | typer + rich(薄;**无独立 REPL**,交互 = TUI) | ✅ |
| TUI | textual(事件驱动增量渲染,渲染器 = event → lines) | ✅ |
| 配置 | `models.json`(格式对齐 pi)+ `settings.json`(默认模型/字符串资源,分层深合并)+ pydantic 校验;凭证不入配置文件(auth store / env) | ✅ |

## 5. 目录布局(运行时)

全局侧比项目侧深一层,与 pi 同构:`~/.pi/agent/` ↔ `<项目>/.pi/`。全部用户级状态都挂在 `agent/` 下,
`~/.qi` 本身只是名字空间外壳(`QI_CONFIG_DIR` 可改)。

```text
~/.qi/                      # 名字空间根(空壳)
└── agent/                  # 全局(QI_AGENT_HOME 可覆盖此路径,对齐 PI_CODING_AGENT_DIR)
    ├── settings.json       # 默认模型 / skills 追加路径 / theme …
    ├── models.json         # providers / 模型(格式对齐 pi)
    ├── auth.json           # 凭证(0600,按 provider)
    ├── SYSTEM.md           # 可选:覆盖基座提示词
    ├── skills/<name>/      # 全局技能(qi 私有)
    ├── agents/<name>/      # 全局 agent
    ├── plugins/<name>/     # 本地目录插件通道
    └── sessions/*.jsonl    # 会话

<项目>/.qi/                 # 项目级(扁平;与 ~/.qi/agent 配对)
├── settings.json           # 覆盖全局(深合并;数组整体替换)
├── models.json
├── skills/  agents/  plugins/
└── SYSTEM.md

~/.agents/skills/           # Agent Skills 标准目录(.qi 的同级,跨工具共享)
```

> 旧版扁平布局(`~/.qi/models.json` 等)在启动时**自动迁移**到 `~/.qi/agent/`,
> 只在目标不存在时搬,绝不覆盖。

## 6. 安全总原则

- **bash 不筛命令**(对齐 pi):内置 bash 以 qi 进程权限执行任意命令;要收紧就在 agent 级摘工具(`disallowed_tools: [bash]`),要真边界就把进程放进容器/VM——进程内的半吊子过滤容易被误当成安全边界
- 内容(技能/第三方 agent)是可执行指令:**先审后装/导入时提示**
- 插件代码 = 全权限:仅可信源;项目级 `.qi`(plugins/agents)需 `-a` 信任
- 凭证三源:auth store(`~/.qi/agent/auth.json`,0600,按 provider)→ 约定环境变量 → `models.json` 的 `apiKey` 引用;配置文件与导入包**零明文**(导入时扫描)
- Web/远程暴露需显式开启 + 鉴权(默认回环)
