# qi:开发计划与未决清单

> 总设计见 [../README.md](../README.md)。本文只放**怎么干**(阶段/验收)与**开工前要拍板的事**。

## 1. 项目目录结构(可打包 wheel)与打包设计

仓库根 = 框架项目(`/workspace/qi`),标准 **src 布局**:源码在 `src/` 下,wheel 只打显式声明的包,`sdk/`、`docs/`、`examples/` 天然进不了包(无需黑名单)。

```text
/workspace/qi/                  ← 仓库根(uv build / pip install . 从这里打)
├── pyproject.toml              # name = "qi-agent",hatchling 构建
├── README.md                   # 总设计(项目根)
├── docs/                       # 设计文档 + 本 PLAN.md(不进 wheel)
├── examples/agents/            # 样例 agent(教学样例;不自动加载,不进 wheel)
├── src/qi_agent/               # 包代码
│   ├── __init__.py             # __version__
│   ├── py.typed                # PEP 561
│   ├── builtin/agents/general/ # ⭐ 包内置兜底 agent(进 wheel;可被用户/项目同名覆盖)
│   ├── cli.py                  # [project.scripts] qi → qi_agent.cli:main
│   ├── config.py               # TOML 分层装载 + pydantic 校验
│   ├── llm.py                  # LLMClient 协议 + 实现(N8 待定)
│   ├── models.py               # AgentConfig/Tool/Message/事件类型
│   ├── registry.py             # AgentRegistry / ToolCatalog / 插件能力注册表
│   ├── loader.py               # agents 发现(内置/用户/项目)+ agent.md/技能解析 + 校验 + SYSTEM.md 解析 + AGENTS.md 项目上下文
│   ├── tools/                  # 内置 7 工具(read/ls/find/grep/write/edit/bash)
│   ├── runner.py               # AgentRunner(tool-loop)
│   ├── system_prompt.py        # ⭐ 系统提示词构建:代码内默认基座 + 动态注入(工具清单/指南/AGENTS.md/技能/数据源/cwd)
│   ├── dispatcher.py           # Router-LLM 分派
│   ├── runtime.py              # AutoRuntime:manual/@点名/每轮重新路由/stream()
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
| --- | --- |
| `qi_agent/*` 代码、py.typed | docs/、examples/(仓库内容) |
| 内置 7 工具、CLI/TUI/MCP 代码 | sdk/(独立产品) |
| 无任何 agent/技能数据(零内置) | 运行时 ~/.qi 数据(永不打包) |

安装后:`pip install qi-agent` → `qi` 命令可用,`~/.qi/` 首次运行创建;web 前端(v2)按 web.md 走**独立 UI 插件包**,不进本 wheel。

## 2. v1(核心库 + CLI + TUI + MCP + 数据源)

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
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

| 内容 | 详见 | 状态 |
| --- | --- | --- |
| HTTP 宿主 `qi web` + 官方 UI 插件(SSE) | web.md | 宿主与**参考 UI 已可运行**(web.md §14);UI 拆包未做 |
| headless RPC(`--mode rpc`,给 IDE/外部客户端) | cli.md / web.md §5 | 未做(内置宿主让 web 不需要它) |
| bash 审批细化 / 路径防越界 / HITL | tools.md §4 | 未做(SSE 已预留 `action.required` 位) |
| 导入 adapter(Claude agent 卡 / SKILL.md) | agent-config.md §10 | 未做 |
| 打包导出 zip、registry/市场 | agent-config.md §10 | 未做 |

## 4. 未决清单与决策状态

### 已收口(A/B 全按推荐 ✅)

| # | 决策 | 结论 |
| --- | --- | --- |
| A1/N10 | 仓库与包名 | `qi-agent` / `qi_agent`,hatchling + uv,`/workspace/qi` 为根,sdk/ 留参考(见 §1) |
| A2/N2 | 应用配置 | `models.json`(格式对齐 pi)+ 分层(env→项目→用户)+ pydantic 校验 |
| A3/N1 | frontmatter 语法 | YAML(对齐 Agent Skills/Claude) |
| A4/N8 | LLM 接入 | litellm(统一多 provider) |
| A5 | 会话 JSONL entry 类型 | 消息/工具结果/分派/状态/自定义 五类(P3 定格式);**`tool` 类与 header `cwd` 已于 P0 契约补丁落地**,见 [web.md §13](web.md#13-p0-契约改动记录2026-09) |
| A6/N7 | bash 安全 | **与 pi 对齐:无命令级过滤**(旧的首词白名单已删除);限制靠 `tools`/`disallowed_tools` 收窄或容器/VM;文件工具路径限会话目录 |
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
| A8 | 全局目录层级 | `~/.qi/agent/` ↔ `<项目>/.qi` 配对(对齐 pi 的 `~/.pi/agent` ↔ `.pi`);旧扁平布局启动时自动迁移(不覆盖);`QI_AGENT_HOME` = agent 目录、`QI_CONFIG_DIR` = 名字空间根 |
| A9 | 设置文件 | `settings.json` 两级深合并、数组整体替换;默认模型只属于 settings(`models.json` 里已不读取);字段清单与“仅存储未生效”清单见 [settings.md](settings.md);`qi config` 读写 |
| A10 | 顶层技能 | 六级来源(低→高):`~/.agents/skills` → `~/.qi/agent/skills` → user `settings.skills` → 项目 `.agents/skills` 祖先链 → `<git根>/.qi/skills` → project `settings.skills`;agent 自带者最高;同层同名报错、跳层覆盖;排除项作用于整个发现集 |
| A11 | 系统提示词 | 默认基座**代码内**(`system_prompt.py`,按解析后的工具集生成「可用工具 / 指南」);`SYSTEM.md` **整体替换**默认基座(项目 > 全局);之后动态追加角色层 → `<project_context>`(AGENTS.override.md > AGENTS.md > AGENTS.MD > CLAUDE.md > CLAUDE.MD,全局 + 祖先链至 git 根)→ `<available_skills>` XML(无 `read`/`bash` 则不注入)→ 数据源 → cwd。取消包内置 `SYSTEM.md`;自身文档索引未做。详见 [system-prompt.md](system-prompt.md) |

### 仍待定(v2 + 实现期)

- C1:SSE vs WebSocket(先 SSE)
- C2:import adapter(Claude/SKILL.md)— v2
- C3:headless RPC 协议细节 — v2
- C4:db 插件工具名前缀/冲突策略 — v2 随首个插件定
- 会话 JSONL entry 类型表具体字段(P3 定稿)
- qi 自身文档索引注入:pi 在 prompt 尾部给 README/docs/examples 绝对路径 + 按主题指路(qi 版见 system-prompt.md §6);障碍是 `docs/` 不进 wheel,装入后路径不存在 —— 要么改打包(把 docs 打进 wheel),要么只在源码仓库里存在时注入
- Router prompt 模板细节(dispatcher.md 草案之上微调)

## 5. 收口节奏

- 开工前先收口 **N7(bash 安全)** 与 **会话 JSONL 格式**(P3/P4 前置)
- 每定一项,同步回写 README 决策表与本清单
- P1 动工前确认 **N10(仓库目录)**

## 6. v1 实现状态(代码完成 ✅,2026-09)

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
