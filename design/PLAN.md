# qi:开发计划与未决清单

> 总设计见 [overview.md](overview.md)。本文只放**怎么干**(阶段/验收)与**开工前要拍板的事**。
> 与上游 pi 的逐节对照(原 `docs/` 各页的「与 pi 的对应」小节)汇总在 [pi-alignment.md](pi-alignment.md)。
> **v3 扩展化重构(进行中)的完整设计在 [extensions.md](../docs/extensions.md)**,阶段表见本文 §3。

## 1. 项目目录结构(可打包 wheel)与打包设计

仓库根 = 框架项目(`/workspace/qi`),标准 **src 布局**:源码在 `src/` 下,wheel 只打显式声明的包,`sdk/`、`docs/`、`examples/` 天然进不了包(无需黑名单)。

```text
/workspace/qi/                  ← 仓库根(uv build / pip install . 从这里打)
├── pyproject.toml              # name = "qi-coding-agent",hatchling 构建
├── README.md                   # 入口页(装 + 快速开始 + 索引)
├── docs/                       # 手册(force-include → qi_agent/docs,随 wheel 发布;索引进提示词)
├── design/                     # 设计记录 + 本 PLAN.md(不进 wheel、不注入)
├── examples/agents/            # 样例角色(不自动加载)
├── examples/extensions/        # 样例扩展(不自动加载)
├── extensions/                 # 官方三件套:qi-mcp / qi-agents / qi-web(各自独立 pip 包)
├── src/qi_agent/               # core 包代码
│   ├── __init__.py             # __version__
│   ├── py.typed                # PEP 561
│   ├── cli.py                  # [project.scripts] qi → qi_agent.cli:main
│   ├── tui.py                  # textual 界面(event → lines)
│   ├── runtime.py              # QiRuntime:stream() / 会话绑定 / 事件派发
│   ├── runner.py               # AgentRunner(tool-loop)
│   ├── extensions.py           # 扩展宿主:ExtensionBus / ExtensionApi / Tool / register_tool
│   ├── system_prompt.py        # ⭐ 提示词构建:代码内默认基座 + 动态注入
│   ├── prompt.py               # 交互式提示控件(选择 / 输入)
│   ├── registry.py             # ToolCatalog / 扩展能力注册表
│   ├── session.py              # JSONL 会话读写 + 分支树
│   ├── compaction.py           # 上下文压缩
│   ├── llm.py                  # LLMClient(litellm)+ model registry
│   ├── models.py               # 核心数据模型:Message / 事件 / 工具结果
│   ├── settings.py             # settings.json(分层深合并)
│   ├── auth.py                 # auth store(~/.qi/agent/auth.json)
│   ├── packages.py             # settings.packages ↔ 实装 比对(qi list / doctor)
│   ├── pep723.py               # 目录通道扩展的 PEP 723 声明
│   ├── loader.py               # markdown + frontmatter 解析(技能/内容共用)
│   ├── paths.py                # 运行时目录约定(QI_CONFIG_DIR / QI_AGENT_HOME …)
│   ├── theme.py / themes/      # 主题(dark / light,移植自 pi)
│   ├── titling.py              # 会话自动命名(用模型起短标题)
│   ├── abort.py                # 协作式中断信号
│   ├── workspaces.py           # 工作区显示名覆盖
│   ├── config.py               # models.json 装载 + 解析
│   └── tools/                  # 内置 8 工具 + clarify
│       ├── __init__.py         # read/ls/find/grep/write/edit/bash/powershell/clarify
│       └── shell.py            # bash 解析(shellPath → Git Bash → PATH → /bin/bash → sh)
├── tests/                      # pytest(54 个文件)
├── .venv/  uv.lock             # uv 工作流
└── sdk/                        # 参考用的 hikqin_sdk:独立产品,不参与本包构建
```

### pyproject.toml 关键段

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "qi-coding-agent"
version = "0.1.0"
description = "多 agent 编码框架:专职角色 agent + auto 分派(Dispatcher)"
readme = "README.md"
requires-python = ">=3.12"
license = { text = "MIT" }
dependencies = [
    "pydantic>=2.10",
    "typer>=0.12",
    "rich>=13.0",
    "textual>=0.80",
    "PyYAML>=6.0",
    "litellm>=1.40",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.24"]

[project.scripts]
qi = "qi_agent.cli:main"

[project.entry-points."qi.extensions"]

[tool.hatch.build.targets.wheel]
packages = ["src/qi_agent"]

[tool.pytest.ini_options]
markers = [
    "real_extensions: 本测试要装载**本机真装的扩展**(退出默认的 entry-point stub)",
]
asyncio_mode = "auto"
testpaths = ["tests"]
```

### wheel 内容边界

| 进 wheel | 不进 wheel |
| --- | --- |
| `qi_agent/*` 代码、py.typed | `design/`、`examples/`、`extensions/`(仓库内容) |
| 内置 8 工具 + `clarify`、CLI/TUI、扩展宿主 | `sdk/`(独立产品) |
| 无任何角色/技能数据(**零内置**;角色归 qi-agents) | 运行时 `~/.qi` 数据(永不打包) |
| `docs/` 手册(**已落地**:force-include → `qi_agent/docs/`,提示词里注入索引) | MCP / web 代码(core 不含,归 qi-mcp / qi-web) |

安装后:`pip install qi-coding-agent` → `qi` 命令可用,`~/.qi/` 首次运行创建;MCP / 角色 / web 都在**独立扩展包**里(qi-mcp / qi-agents / qi-web),core 一个都不带。

## 2. v1(核心库 + CLI + TUI + MCP + 数据源)

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| P1 骨架 | pyproject、包结构、配置装载(TOML 分层 + pydantic)、models 解析 | `qi doctor` 能读配置/模型并报错 |
| P2 内容装载 | agents 目录发现(2 层)、agent.md/技能解析、**装载校验器**(import 复用同款) | `qi agents list/show` 输出正确;坏包报错 |
| P3 会话 | JSONL 格式、sessions 读写与命令 | `-c/--session` 续聊;`sessions list/show/rm` 可用 |
| P4 执行内核 | LLMClient、AgentRunner(tool-loop)、内置 8 工具、ToolContext、结果截断 | manual(`--agent`)单 agent 完整跑通含 write/edit |
| P5 Dispatcher | registry → Router-LLM 结构化分派 + general 兜底 + `@` 点名 | auto 模式多 agent 端到端正确分派 |
| P6 CLI | cli.md 全命令面落地 | 命令与文档一致 |
| P7 TUI | textual 消息流 + dispatch 卡片 + 状态栏 + `/` 命令 | 交互会话流畅 |
| P8 MCP + 数据源 | mcp.json 装载、db 插件(首个参考插件)+ data_sources 动态装载门控 | 装插件前/后行为符合 plugins.md |
| P9 测试与样例 | 单测 + 路由回归集 + demo agents 齐全 | 覆盖率与回归集基线 |

## 3. v3:扩展化重构(进行中)

> 目标:core 收窄到 **pi 同款**(单 agent + 工具 + 会话 + TUI + 扩展宿主),MCP / 多 agent / web 拆成**独立官方 pip 扩展**(qi-mcp / qi-agents / qi-web)。
> 完整设计(边界、hook 面、中间件链语义、前置件、三件套)见 **[extensions.md](../docs/extensions.md)**;本节只放阶段与验收。
> 连带:定位从「多 Agent 编码框架」→「**单 agent 框架 + 可选多 agent 扩展**」,`auto` 分派取消(改 **agent-as-tool**)。

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| P-E1a/b/c 宿主骨架 ✅ | plugin→extension 改名(entry point `qi.extensions` / `.qi/extensions/` / `extension.py`)、激活 `settings.extensions`、**信任门控**(`-a`/`-na` + `defaultProjectTrust` + headless fail-safe)、legacy 目录迁移 | 未信任项目的 `.qi/extensions/` 不加载;迁移后能装载(19 项新测试,见 `tests/test_extensions.py`) |
| P-E1d 事件面骨架 ✅ | 总线 `ExtensionBus`(`emit`/`emit_until`/快照遍历/patch 链/失败隔离/fail-safe)+ `api.on()` + `ctx` 最小集 + 首个真实事件 `session_start` | 链语义逐条有测试;真实 QiRuntime 端到端收到 `session_start`(19 项新测试,`tests/test_extension_bus.py`) |
| P-E2 工具面 + 输入面 ✅ | `registerTool` 完整化(动态注册 / `source_info` / `prompt_snippet` / `prompt_guidelines`)、`setActiveTools`/`getAllTools`、`exec`、公开 import 白名单 + 禁钉宿主版本、`input`/`before_agent_start`/`tool_call`/`tool_result`/`context`/`turn_start` | 内置 9 工具改走 `registerTool`(dogfood);`tool_call` 能拦住工具且**真的没执行**;39 项新测试(`test_extensions.py` / `test_extension_tools.py` / `test_extension_events.py` / `test_extension_runner_events.py` / `test_extension_deps.py`) |
| P-E3 命令与 UI + 会话面 | `registerCommand`/`Shortcut`/`Flag`、**`ctx.ui` 通道(TUI)**、renderer 三件套、`appendEntry`、`sendMessage`/`sendUserMessage`、`sessionManager`、`setModel`/thinking、会话类事件 | 扩展能注册 `/cmd`、弹 confirm、落盘自定义 entry 并在 TUI 回放 |
| P-E4 core 收窄 | runner 入参 `AgentUnit` → `{system_prompt, tools, model}`;**新增 `ctx.runAgent(spec, task)`(E12:子 agent 走进程内)**;`AgentRegistry`/`load_all_agents`/dispatcher 移出 core;`events` 总线;`registerProvider` | `qi` 裸启动单 agent 跑通;core 不再 import dispatcher;core 依赖去掉 `mcp`;`ctx.runAgent` 能在测试里跑完一个子任务 |
| P-E5 三件套迁移 | qi-mcp → qi-agents → qi-web(依赖从少到多);`ctx.ui` 的 web 侧 + `add_route`/`add_static` | 三个包各自可装可卸;不装时启动提示与 `qi doctor` 正确;`qi web` 由扩展提供;`qi --agent <name>` 由 qi-agents 提供 |
| P-E6 收尾 | 参考扩展样例 + 目录通道的 PEP 723 声明解析 + 依赖冲突报告 + 文档重写(agent-config / web / dispatcher 归档)+ 迁移提示打磨 | 新用户按 extensions.md 能写出并装上第一个扩展;声明与实装不一致时 `qi doctor` 报得出来 |

**依赖关系**:P-E1a/b/c ✅ → **P-E1d ✅** → P-E2 → P-E3 顺序做;P-E4 可与 P-E3 并行;**P-E5 依赖 P-E2 + P-E3 + P-E4(`ctx.runAgent`)**;P-E6 最后。

**被 v3 取代的既有条目**:P5(Dispatcher)与 P8 的「db 插件」形态、B7'(每轮重路由)、C4(db 插件工具名冲突)——多 agent 形态一变,这些全部重定。

## 4. v2

| 内容 | 详见 | 状态 |
| --- | --- | --- |
| HTTP 宿主 `qi web` + 官方 UI 插件(SSE) | web.md | 宿主与**参考 UI 已可运行**(web.md §14);UI 拆包未做 |
| headless RPC(`--mode rpc`,给 IDE/外部客户端) | web.md §5 / 未决 C3 | 未做(内置宿主让 web 不需要它) |
| TUI 里改 `tuiMode` 立即换渲染模式(不重启) | — | 未做(启动时读 `settings.tuiMode`;`--tui-mode` 只覆盖当次) |
| TUI 斜杠命令面 | [slash-commands.md](../docs/slash-commands.md) | **无待补**:pi 的 23 条内置命令已全部对齐,`PLANNED_COMMANDS` 是空集合;后续命令走扩展 `registerCommand` |
| bash 审批细化 / 路径防越界 / HITL | 未决 C5([how-qi-works.md §4](../docs/how-qi-works.md) 的边界) | 未做(SSE 已预留 `action.required` 位) |
| 导入 adapter(Claude agent 卡 / SKILL.md) | agent-config-design.md §10 | 未做 |
| 打包导出 zip、registry/市场 | agent-config-design.md §10 | 未做 |

## 5. 未决清单与决策状态

### 已收口(A/B 全按推荐 ✅)

| # | 决策 | 结论 |
| --- | --- | --- |
| A1/N10 | 仓库与包名 | **发行名 `qi-coding-agent`**(2026-09 改:`qi-agent` / `qi` 在 PyPI 上都已被别的项目占用),import 包名仍 `qi_agent`、命令仍 `qi`;hatchling + uv,`/workspace/qi` 为根,sdk/ 留参考(见 §1) |
| A2/N2 | 应用配置 | `models.json`(格式对齐 pi)+ 分层(env→项目→用户)+ pydantic 校验 |
| A3/N1 | frontmatter 语法 | YAML(对齐 Agent Skills/Claude) |
| A4/N8 | LLM 接入 | litellm(统一多 provider) |
| A5 | 会话 JSONL entry 类型 | 消息/工具结果/分派/状态/自定义 五类(P3 定格式);**`tool` 类与 header `cwd` 已于 P0 契约补丁落地**,见 [web.md §13](web.md#13-p0-契约改动记录2026-09) |
| A6/N7 | bash 安全 | **与 pi 对齐:无命令级过滤**(旧的首词白名单已删除);限制靠 `tools`/`disallowed_tools` 收窄或容器/VM;文件工具路径限会话目录。执行器为**解析出的真 bash**(`shellPath` → Git Bash → PATH → `/bin/bash` → `sh`),Windows 原生另备 `powershell` 工具 |
| B1/N3 | sessions 位置 | 全局 `~/.qi/agent/sessions/`(对齐 pi) |
| B2/N4 | mcp_servers 省略默认 | **默认无、必须显式声明**(凭证敏感);tools 仍"省略=全部" |
| B3/N5 | disallowed_tools | v1 做(denylist,Claude 同款) |
| B4/N6 | clarify 工具 | v1 加(全局通用工具) |
| B5 | Router 输出约束 | tool-call 强约束 |
| B6 | confidence_min | 0.6(可配 `[runtime]`) |
| B7 | sticky 档位 | ~~v1 关键词启发~~ → **已废弃(B7')**:改为每轮重新路由,不做会话亲和;Router 带着 `active_agent` 上下文自行判定是否沿用。理由见 [dispatcher.md §3](dispatcher.md) |
| B8 | L2 embedding | 可插拔模块,默认关(离线场景再开) |
| B9 | agent.md icon | 不进 v1 |
| B10 | 样例 | examples/ 加 writer + general(演示 auto 多角色) |
| A7 | 凭证存储 | auth store `~/.qi/agent/auth.json`(0600,git 不跟踪);解析顺序 auth store → 约定 env → models.json `apiKey` 引用;`qi auth login/logout/list` + 只读三件套 `print-api-key` / `print-bearer-token` / `check`(退出码对齐 pi:0/1/2)、`qi init` 引导 |
| A8 | 全局目录层级 | `~/.qi/agent/` ↔ `<cwd>/.qi` 配对(对齐 pi 的 `~/.pi/agent` ↔ `.pi`);旧扁平布局启动时自动迁移(不覆盖);`QI_AGENT_HOME` = agent 目录、`QI_CONFIG_DIR` = 名字空间根 |
| A9 | 设置文件 | `settings.json` 两级深合并、数组整体替换;默认模型只属于 settings(`models.json` 里已不读取);字段清单与“仅存储未生效”清单见 [settings.md](../docs/settings.md);`qi config` 读写 |
| A10 | 顶层技能 | 六级来源(低→高):`~/.agents/skills` → `~/.qi/agent/skills` → user `settings.skills` → 项目 `.agents/skills` 祖先链 → `<cwd>/.qi/skills` → project `settings.skills`;agent 自带者最高;同层同名报错、跳层覆盖;排除项作用于整个发现集 |
| A11 | 系统提示词 | 默认基座**代码内**(`system_prompt.py`,按解析后的工具集生成「可用工具 / 指南」+ **手册索引**);`SYSTEM.md` **整体替换**默认基座(项目 > 全局);之后动态追加 `<project_context>`(AGENTS.override.md > AGENTS.md > AGENTS.MD > CLAUDE.md > CLAUDE.MD,全局 + 祖先链至文件系统根)→ `<available_skills>` XML(无 `read`/`bash` 则不注入)→ cwd → `--append-system-prompt`。角色层与数据源**不在 core**(归 qi-agents / 提供方扩展)。详见 [configuration.md](../docs/configuration.md) |
| A12 | 轮次 / 超时 / 中断 | **对齐 pi:runner 里既不设轮次上限,也不套回合级请求超时** —— 循环是 `while True`,退出靠模型不再调工具 / 中止 / 嵌入方谓词 `stop_after`(pi 的 `shouldStopAfterTurn` 同形,默认 `None` = 不限)。**qi 自己从不传谓词**(pi-coding-agent 也从不实现那个钩子):交互式靠 `escape` / 客户端断开,无头靠 `SIGTERM`/`SIGHUP` → `kill_live_children()` + `exit(143/129)`(对齐 pi 的 `killTrackedDetachedChildren`)。请求级超时与重试归 **provider 层**(`settings.json` 的 `retry.provider.timeoutMs` / `maxRetries` → litellm `timeout` / `num_retries`;对齐 pi 的 `getProviderRetrySettings`)。中断是**协作式**(`abort.py` 的 `AbortSignal`):未执行的 tool_call 补“已中断”结果、半截回答照常落盘、照常发 `agent_end`(data 带 `aborted`);TUI `escape` 宽限 3s 后 `cancel_all()` 兜底;硬取消(`CancelledError`,含 SIGINT/ASGI 取消)也先落盘再抛 |

### 仍待定(v2 + 实现期)

- C1:SSE vs WebSocket(先 SSE)
- C2:import adapter(Claude/SKILL.md)— v2
- C3:headless RPC 协议细节 — v2
- C5:工具审批 / 确认交互形态(破坏性操作的**交互前钩子**,即 HITL;与上表「bash 审批细化」、SSE 的 `action.required` 同位)— v2
- C4:扩展工具名前缀/冲突策略 — **v3 并入 qi-mcp / qi-agents 的工具命名约定**(见 design/extensions-design.md 的 E22);v1 的「db 插件」形态取消
- **litellm 的 ~6.8s import**(2026-09 实测,热缓存 3 次稳定)— 拖累每一次 `qi -p` 的首次响应,也是 v3 选「子 agent 进程内」的量化依据(子进程 = 6.8s × N);待查瘦身开关或 provider 直连。见 [extensions-design.md §11.6](extensions-design.md)
- 会话 JSONL entry 类型表具体字段(P3 定稿)
- ~~qi 自身文档索引注入~~ → **已落地**:`docs/` 经 force-include 进 wheel(`qi_agent/docs/`),默认基座末尾注入由 `docs.json` 生成的索引;`--append-system-prompt` 也一并接上(见 [configuration.md §6](../docs/configuration.md))
- 回合级重试(pi 的 `retry.enabled` / `maxRetries` / `baseDelayMs`:失败回合退避重试)未接;已接的是 provider 层的 `retry.provider.timeoutMs` / `maxRetries`(见 settings.md)。pi 的 `retry.provider.maxRetryDelayMs` 无对应 litellm 参数,也未映射
- Router prompt 模板细节(dispatcher.md 草案之上微调)

## 6. 收口节奏

- 开工前先收口 **N7(bash 安全)** 与 **会话 JSONL 格式**(P3/P4 前置)
- 每定一项,同步回写 README 决策表与本清单
- P1 动工前确认 **N10(仓库目录)**

## 7. v1 实现状态(代码完成 ✅,2026-09)

P1-P9 代码已落地并推送(master),tests **180** 通过、wheel 构建通过。

**P0 契约补丁(2026-09,为 web 铺路)**:工具结果结构化(`ToolOutcome` → `tool_end.data` 带
`status/duration_ms/exit_code`)、`usage` 透出到 `agent_end`、会话 header 加 `cwd`(旧会话首次使用时回填)、
补齐第五类 `tool` entry 落盘、user 消息改为**发起时**落盘、**逐字流式**(`text_delta`,新增
`assistant_message` 并把"工具前的叙述"落成 custom entry 以保证回放顺序)。逐项契约与理由见
[web.md §13](web.md#13-p0-契约改动记录2026-09),不变量由 `tests/test_contract_p0.py`
与 `tests/test_streaming.py` 锁定。

> CLI/TUI 输出**不变**:它们只读回合末尾的 `text` 事件,逐字增量只服务于 Web。

**待真环境验证**(需 key/终端/外部服务):

- 真实 LLM 对话端到端(litellm + defaultProvider/defaultModel;回归集跑分)
- MCP server 实连(stdio/http;当前完成解析与门控)
- TUI 真终端交互(当前为冒烟级基础版)
