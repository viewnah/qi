# qi:多 Agent 编码框架(总设计)

> 目标:用 Python 实现一个「类似 pi」的多 agent 编码框架——专职角色 agent + **auto 模式**(Dispatcher 自动分派)。参考 hikqin_sdk 的设计(不依赖它),独立实现。
> **开发计划见 [docs/PLAN.md](docs/PLAN.md)**;本文是设计总入口:文档索引、架构总览、决策索引、技术选型与目录布局。

## 快速开始

```bash
# 1. 引导配置默认模型(交互:上下键选择 provider/模型,写 models.json + auth.json)
qi init

# 非交互(CI / 脚本)
qi init -y --provider deepseek --model deepseek-chat \
    --base-url https://api.deepseek.com/v1 --api openai-completions --api-key sk-xxx

# 2. 校验配置与凭证
qi doctor

# 3. 运行(无头一次执行,auto 分派)
qi -p "分析这个仓库"
```

`qi init` 交互流程与样式复刻 QwenPaw `init`:**Provider Configuration**(选已有/新建 → Base URL → API 类型 → API Key)→ **Add Models**(`Add a model?` 循环,含 reasoning/contextWindow/maxTokens)→ **Activate LLM Model**(选 provider → 选 model)。完整用法、选项与示例见 [docs/model-config.md §7](docs/model-config.md#7-qi-init-用法)。

## 1. 文档索引

| 文档 | 内容 |
|---|---|
| [PLAN.md](docs/PLAN.md) | 开发计划(v1/v2 阶段)与未决清单 |
| [docs/agent-config.md](docs/agent-config.md) | agent = 自包含目录(agent.md / skills / assets / mcp.json / data_sources.json)、装载校验、MCP、数据源、导入导出 |
| [docs/tools.md](docs/tools.md) | ToolCatalog、内置 7 工具、tools 三态、bash 安全(v1 只读 allowlist) |
| [dispatcher.md](docs/dispatcher.md) | auto 模式:信号分层、分派管线、Router 契约、优先级 |
| [docs/model-config.md](docs/model-config.md) | `models.json`(对齐 pi:`providers` / `baseUrl` / `api` / `models`);默认模型 default / 分派 router;凭证 auth store + 约定 env + apiKey 引用 |
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
│   Runtime.stream() ── sticky/@点名 ── Dispatcher(路由)      │
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
|---|---|---|
| agent | **内容**:自包含目录 `agents/<name>/`(agent.md 定义,frontmatter + 正文即 system prompt) | agent-config.md |
| agent 位置 | 仅 2 处:`~/.qi/agents/` + `<项目>/.qi/agents/`;项目静默覆盖全局;同层重复报错;无内置 | agent-config.md §3 |
| agent.md 字段(v1) | name / display_name / description(路由信号)/ keywords / tools / include / opening | agent-config.md §4 |
| 技能 | 私有自动绑定、渐进披露、无内置、无共享库;同一 agent 内同名报错 | agent-config.md §5 |
| MCP(v1) | 私有 mcp.json(仅本 agent,凭证 env)+ 全局 `[mcp.servers]` 按 `mcp_servers` 绑定 | agent-config.md §8 |
| 数据源 | 实例在 agent `data_sources.json`(私有,凭证 env);**插件消费型配置**:插件缺席不装载 | agent-config.md §9 / plugins.md |
| 导入导出 | import = 拷贝 + 复用装载校验器 + 明文凭证扫描;export 产物可直接 import | agent-config.md §10 |
| 工具 | 代码全局注册 ToolCatalog;**tools 三态**:省略或 `*` = 全部,名单 = allowlist;未知名报错 | tools.md |
| 内置工具(v1) | 7 个:read / ls / find / grep / write / edit(diff 精确)/ bash(对齐 pi,去 powershell) | tools.md §2 |
| 模型 | 全局 `models.json`:`defaultProvider/defaultModel`(执行)/ `routerProvider/routerModel`(分派);agent 不声明模型 | model-config.md |
| 插件 | pip 包(entry point `qi.plugins`)+ 本地目录双通道;register():add_tool / provides_config / provides_types | plugins.md |
| 运行 | auto 默认(Dispatcher 分派);`--agent` manual;sticky 会话亲和 + `@` 点名 | cli.md |
| 命令 | 参数尽量对齐 pi:`qi [-p\|-c\|-r\|…] [--] [@files…] [msg…]` + 子命令 | cli.md |
| Web(v2) | HTTP 宿主在框架(`qi web`),UI 插件化;不做外置 RPC 桥 | web.md |
| 配置形态 | agent 定义 = Markdown + frontmatter;模型配置 = JSON `models.json`(对齐 pi,分层:env → 项目 → 用户) | model-config.md |
| 会话 | JSONL 每会话文件(pi 风格,entry 带 type/agent_id);位置:**全局 `~/.qi/sessions/`** |
| Dispatcher | auto:信号分层(L1 规则 / L2 embedding 默认关 / L3 Router 读 description / L4 兜底)+ sticky + @ 点名 |
| bash 安全 | 默认只读 allowlist;破坏性命令需配置放开或审批(v2);路径限会话目录 |
| clarify / denylist | v1 内置 clarify 通用工具;`disallowed_tools` v1 |

## 4. 技术选型

| 项 | 选择 | 状态 |
|---|---|---|
| 语言 | Python ≥3.12 + asyncio | ✅ |
| 数据模型/校验 | **pydantic v2**(AgentConfig/Tool 参数 schema/配置校验) | ✅ |
| LLM 接入 | litellm 统一 或 直连 openai/deepseek SDK | ⏳ 待定(倾向 litellm) |
| 会话存储 | JSONL 每会话文件(不选 SQLite:追加写/可读/零迁移) | ✅ |
| CLI | typer + rich(薄;**无独立 REPL**,交互 = TUI) | ✅ |
| TUI | textual(事件驱动增量渲染,渲染器 = event → lines) | ✅ |
| 配置 | JSON `models.json`(格式对齐 pi)+ 分层深合并 + pydantic 校验;凭证不入配置文件(auth store / env) | ✅ |

## 5. 目录布局(运行时)

```
~/.qi/                      # 全局(与项目 .qi 同构)
├── models.json             # providers / 模型 / default(格式对齐 pi)
├── agents/<name>/          # 全局 agent
├── plugins/<name>/         # 本地目录插件通道
└── sessions/*.jsonl        # 会话(全局,对齐 pi)

<项目>/.qi/                 # 项目级,需 -a 信任后加载
├── models.json
├── agents/  plugins/       # 项目私有;项目版覆盖全局版(静默)
```

## 6. 安全总原则

- 内容(技能/第三方 agent)是可执行指令:**先审后装/导入时提示**
- 插件代码 = 全权限:仅可信源;项目级 `.qi`(plugins/agents)需 `-a` 信任
- 凭证三源:auth store(`~/.qi/auth.json`,0600,按 provider)→ 约定环境变量 → `models.json` 的 `apiKey` 引用;配置文件与导入包**零明文**(导入时扫描)
- Web/远程暴露需显式开启 + 鉴权(默认回环)
