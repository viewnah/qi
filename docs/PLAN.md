# qi:开发计划与未决清单

> 总设计见 [../README.md](../README.md)。本文只放**怎么干**(阶段/验收)与**开工前要拍板的事**。

## 1. 项目目录结构(可打包 wheel)与打包设计

仓库根 = 框架项目(`/workspace/qi`),标准 **src 布局**:源码在 `src/` 下,wheel 只打显式声明的包,`sdk/`、`docs/`、`examples/` 天然进不了包(无需黑名单)。

```
/workspace/qi/                  ← 仓库根(uv build / pip install . 从这里打)
├── pyproject.toml              # name = "qi-agent",hatchling 构建
├── README.md                   # 总设计(项目根)
├── docs/                       # 设计文档 + 本 PLAN.md(不进 wheel)
├── examples/agents/            # 样例 agent,不自动加载(不进 wheel)
├── src/qi_agent/               # 包代码
│   ├── __init__.py             # __version__
│   ├── py.typed                # PEP 561
│   ├── cli.py                  # [project.scripts] qi → qi_agent.cli:main
│   ├── config.py               # TOML 分层装载 + pydantic 校验
│   ├── llm.py                  # LLMClient 协议 + 实现(N8 待定)
│   ├── models.py               # AgentConfig/Tool/Message/事件类型
│   ├── registry.py             # AgentRegistry / ToolCatalog / 插件能力注册表
│   ├── loader.py               # agents 发现 + agent.md/技能解析 + 装载校验器
│   ├── tools/                  # 内置 7 工具(read/ls/find/grep/write/edit/bash)
│   ├── runner.py               # AgentRunner(tool-loop)
│   ├── dispatcher.py           # Router-LLM 分派
│   ├── runtime.py              # AutoRuntime:sticky/@/manual/stream()
│   ├── session.py              # JSONL 会话读写
│   ├── events.py               # 事件总线
│   ├── mcp.py                  # MCP client 装载(v1)
│   └── tui/                    # textual 界面
├── tests/                      # pytest
├── .venv/  uv.lock             # uv 工作流(对齐 hikqin_sdk)
└── sdk/                        # 参考用的 hikqin_sdk:独立产品,不参与本包构建
```

### pyproject.toml 关键段

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "qi-agent"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [                    # 核心最小集
    "pydantic>=2.10",
    "typer>=0.12",
    "rich>=13.0",
    "textual>=0.80",
    "mcp>=1.26",                    # MCP v1
]                                   # LLM 接入待定(N8):litellm 或直连 SDK

[project.optional-dependencies]
web = ["fastapi>=0.115", "uvicorn[standard]>=0.32"]   # v2:qi web 宿主
dev = ["pytest>=8", "pytest-asyncio", "uv"]

[project.scripts]
qi = "qi_agent.cli:main"

[project.entry-points."qi.plugins"]  # 插件注册组(框架自身不装任何插件)

[tool.hatch.build.targets.wheel]
packages = ["src/qi_agent"]
```

### wheel 内容边界

| 进 wheel | 不进 wheel |
|---|---|
| `qi_agent/*` 代码、py.typed | docs/、examples/(仓库内容) |
| 内置 7 工具、CLI/TUI/MCP 代码 | sdk/(独立产品) |
| 无任何 agent/技能数据(零内置) | 运行时 ~/.qi 数据(永不打包) |

安装后:`pip install qi-agent` → `qi` 命令可用,`~/.qi/` 首次运行创建;web 前端(v2)按 web.md 走**独立 UI 插件包**,不进本 wheel。

## 2. v1(核心库 + CLI + TUI + MCP + 数据源)

| 阶段 | 内容 | 验收 |
|---|---|---|
| P1 骨架 | pyproject、包结构、配置装载(TOML 分层 + pydantic)、models 解析 | `qi doctor` 能读配置/模型并报错 |
| P2 内容装载 | agents 目录发现(2 层)、agent.md/技能解析、**装载校验器**(import 复用同款) | `qi agents list/show` 输出正确;坏包报错 |
| P3 会话 | JSONL 格式、sessions 读写与命令 | `-c/--session` 续聊;`sessions list/show/rm` 可用 |
| P4 执行内核 | LLMClient、AgentRunner(tool-loop)、内置 7 工具、ToolContext、结果截断 | manual(`--agent`)单 agent 完整跑通含 write/edit |
| P5 Dispatcher | registry → Router-LLM 结构化分派 + general 兜底 + `@` 点名 | auto 模式多 agent 端到端正确分派 |
| P6 CLI | cli.md 全命令面落地 | 命令与文档一致 |
| P7 TUI | textual 消息流 + dispatch 卡片 + 状态栏 + `/` 命令 | 交互会话流畅 |
| P8 MCP + 数据源 | mcp.json 装载、db 插件(首个参考插件)+ data_sources 动态装载门控 | 装插件前/后行为符合 plugins.md |
| P9 测试与样例 | 单测 + 路由回归集 + demo agents 齐全 | 覆盖率与回归集基线 |

## 3. v2

| 内容 | 详见 |
|---|---|
| HTTP 宿主 `qi web` + 官方 UI 插件(SSE) | web.md |
| headless RPC(`--mode rpc`,给 IDE/外部客户端) | cli.md / web.md §5 |
| bash 审批细化 / 路径防越界 / HITL | tools.md §4 |
| 导入 adapter(Claude agent 卡 / SKILL.md) | agent-config.md §10 |
| 打包导出 zip、registry/市场 | agent-config.md §10 |

## 4. 未决清单与决策状态

### 已收口(A/B 全按推荐 ✅)

| # | 决策 | 结论 |
|---|---|---|
| A1/N10 | 仓库与包名 | `qi-agent` / `qi_agent`,hatchling + uv,`/workspace/qi` 为根,sdk/ 留参考(见 §1) |
| A2/N2 | 应用配置 | `qi_agent.toml`,TOML + 分层(env→项目→用户→内置) |
| A3/N1 | frontmatter 语法 | YAML(对齐 Agent Skills/Claude) |
| A4/N8 | LLM 接入 | litellm(统一多 provider) |
| A5 | 会话 JSONL entry 类型 | 消息/工具结果/分派/状态/自定义 五类(P3 定格式) |
| A6/N7 | bash 安全 | 默认只读 allowlist;破坏性命令需配置放开或审批;路径限会话目录 |
| B1/N3 | sessions 位置 | 全局 `~/.qi/sessions/`(对齐 pi) |
| B2/N4 | mcp_servers 省略默认 | **默认无、必须显式声明**(凭证敏感);tools 仍"省略=全部" |
| B3/N5 | disallowed_tools | v1 做(denylist,Claude 同款) |
| B4/N6 | clarify 工具 | v1 加(全局通用工具) |
| B5 | Router 输出约束 | tool-call 强约束 |
| B6 | confidence_min | 0.6(可配 `[runtime]`) |
| B7 | sticky 档位 | v1 关键词启发;预留语义判定升级位 |
| B8 | L2 embedding | 可插拔模块,默认关(离线场景再开) |
| B9 | agent.md icon | 不进 v1 |
| B10 | 样例 | examples/ 加 writer + general(演示 auto 多角色) |
| A7 | 凭证存储 | auth store `~/.qi/auth.json`(0600,git 不跟踪);解析顺序 api_key_env → store → 约定 env;`qi auth login/logout/list`、`qi init` 引导 |

### 仍待定(v2 + 实现期)

- C1:SSE vs WebSocket(先 SSE)
- C2:import adapter(Claude/SKILL.md)— v2
- C3:headless RPC 协议细节 — v2
- C4:db 插件工具名前缀/冲突策略 — v2 随首个插件定
- 会话 JSONL entry 类型表具体字段(P3 定稿)
- TUI 布局与 `/` 命令清单(P7 定稿,草案见 cli.md 或独立 tui 文档)
- Router prompt 模板细节(dispatcher.md 草案之上微调)

## 5. 收口节奏

- 开工前先收口 **N7(bash 安全)** 与 **会话 JSONL 格式**(P3/P4 前置)
- 每定一项,同步回写 README 决策表与本清单
- P1 动工前确认 **N10(仓库目录)**

## 6. v1 实现状态(代码完成 ✅,2026-09)

P1-P9 代码已落地并推送(master),tests 26 通过、wheel 构建通过。

**待真环境验证**(需 key/终端/外部服务):
- 真实 LLM 对话端到端(litellm + [models.default];回归集跑分)
- MCP server 实连(stdio/http;当前完成解析与门控)
- TUI 真终端交互(当前为冒烟级基础版)
