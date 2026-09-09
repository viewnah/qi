# Agent 配置设计

> 状态:设计讨论中,随讨论演进。文末附「决策记录」与「待定决策」。
> 讨论结论:参考 hikqin_sdk 设计但独立实现;不基于它扩展。
> 相关文档:模型配置见 [model-config.md](model-config.md);内置工具见 [tools.md](tools.md)。

## 1. 设计原则

- **内容跟 agent 走**:人设、技能、资产是"内容",物理上属于某个 agent,可复制、可整体搬运。
- **代码工具全局共享**:工具是代码,只能注册一次,agent 按名引用。
- **基建集中配置**:模型、MCP server(含凭证引用)集中管理,agent 按名绑定可见性。
- **无内置内容**:运行时没有任何自动加载的 agent / 技能;示例放仓库 `examples/`,想要就拷走。

## 2. 一个 agent 是什么

**一个 agent = 一个目录 `agents/<name>/`**,固定入口文件 `agent.md`:

```
agents/code-analyst/
├── agent.md                  # ⭐ frontmatter(结构化配置)+ 正文(system prompt)
├── skills/                   # 私有技能:目录下所有 SKILL.md 自动绑定(可选,无内置)
│   └── code-review-checklist/
│       ├── SKILL.md          # 技能自带 frontmatter,description 用于渐进披露
│       └── assets/…          # 技能脚本/模板/参考
├── assets/                   # agent 私有参考/模板(可选)
│   └── review-rules.md       # 通过 frontmatter include 拼入 system prompt
├── mcp.json                  # (可选,v1)私有 MCP server 定义,只含 env 引用,无明文密钥
└── data_sources.json         # (可选)数据源实例(凭证 env);装载规则见 plugins.md
```

拷贝整个目录 = 连人设带技能整体带走。

## 3. 存放位置与覆盖规则

agent 只可能出现在 **2 个位置**,目录名都用 `agents/`:

| 位置 | 用途 | 优先级 |
|---|---|---|
| `~/.qi/agents/` | 全局(所有项目可用) | 低 |
| `<项目>/.qi/agents/` | 项目私有(跟项目走,可提交共享) | **高** |

同名规则:

- 两边都有 `<name>` → **项目版生效**(静默覆盖,不警告)。
- 同一层内出现重复 → 启动报错(配置错误,绝不静默取一)。
- 都不存在 → 该名字不存在,引用方报 `unknown agent: <name>`。

## 4. agent.md 格式

YAML frontmatter(`---` 包裹)+ Markdown 正文。正文即 system prompt。

### 4.1 字段定义(v1)

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | ✅ | 唯一标识,须与目录名一致(装载时校验,不一致报错);小写字母/数字/连字符 |
| `display_name` | | UI 显示名,默认取 `name` |
| `description` | ✅ | ⭐ 路由信号:适用场景 + **明确不适用**场景 |
| `keywords` | | L1 规则层路由命中词 |
| `tools` | | 工具引用:省略或 `["*"]` = 全部可用;显式名单 = 仅这些(allowlist,硬约束);含未知名 → 启动报错 |
| `include` | | `assets/` 下相对路径 md 文件列表,按序拼入 system prompt |
| `opening` | | 新会话开场(可选):`message` = agent 开场白(新会话首条,进历史);`suggestions` = UI 层快捷提问(不进历史,点选后作为 user 消息提交) |
- `description` 写作规范(路由准确率的上限由它决定):
  ```yaml
  description: |
    分析代码结构、定位 bug、评估改动影响、给出重构方案。
    适用:代码阅读、问题排查、设计评审。
    不适用:写文档/文案、执行修改类操作。
  ```

### 4.2 正文(system prompt)

```markdown
---
name: code-analyst
display_name: 代码分析师
description: |
  分析代码结构、定位 bug、评估改动影响、给出重构方案。
  适用:代码阅读、问题排查、设计评审。
  不适用:写文档/文案、执行修改类操作。
keywords: [bug, 重构, review, 分析]
tools: [read, ls, grep, find, bash]
include: [assets/review-rules.md]
opening:
  message: "我是代码分析师,可以帮你定位 bug、评估改动。把代码或问题发给我。"
  suggestions:
    - "分析一下这个仓库的结构"
    - "帮我找这段报错的原因"
---
你是「代码分析师」,职责:
1. 先读文件再下结论,引用具体行号……
2. 只输出分析与方案,不执行修改……
```

## 5. 技能(私有,自动绑定)

- 框架**不随包发任何技能**;技能只存在于 agent 目录的 `skills/` 下,放了才有。
- `skills/` 下每个 `SKILL.md` 自带 frontmatter(含 `description`);description 进入该 agent 的 system prompt(渐进披露),用时才加载 SKILL.md 全文。
- 同一 agent 的 `skills/` 内出现同名技能 → 启动报错。
- 技能不与 agent 跨层合并:目录整体覆盖(见 §3)即整体替换,无混合状态。

## 6. 装载与校验流程

```
扫 ~/.qi/agents/ 与 <项目>/.qi/agents/(项目优先,同名警告)
  → 每个 <name>/ 解析 agent.md:
     frontmatter → AgentConfig(pydantic)
     校验: name==目录名 / description 非空 / tools 存在于 ToolCatalog
           / include 文件存在
     正文 + include 拼合 → system_prompt
     skills/ 递归发现 SKILL.md,同名冲突报错
  → 得到 AgentUnit 注册进 AgentRegistry
```

## 7. 运行时形态

```
AgentUnit("code-analyst")
├── config        ← agent.md(frontmatter + 正文 + include)
├── tools[]       ← 全局 ToolCatalog 解析(read, ls, grep, find, bash)— 只注入被引用的
├── skills[]      ← skills/ 下全部 SKILL.md(自动绑定,渐进披露)
├── mcp_tools[]   ← 私有 mcp.json 或全局 [mcp.servers] → tools/list 注入
└── model         ← [models.default](执行统一用全局默认模型,见 model-config.md)
```

工具、技能、MCP 工具最终都以"工具"形态进入执行循环,对 AgentRunner 透明。

## 8. MCP(v1)

### 8.1 两个来源

| 来源 | 位置 | 可见性 |
|---|---|---|
| 全局 server | `qi_agent.toml [mcp.servers.<name>]`(共享基建,凭证仅 env 引用) | 按 agent 声明绑定 |
| 私有 server | agent 目录内 `mcp.json`(拷贝即走) | **仅该 agent**,自动绑定 |

### 8.2 绑定规则

frontmatter 增加 `mcp_servers`(v1):**默认无,必须显式声明**(凭证敏感;与 tools 的"省略=全部"不同):

```yaml
mcp_servers: [github]     # allowlist:只用这些全局 server
# 省略 = 不绑定任何全局 server;私有 mcp.json 的 server 总是仅本 agent 可用
```

### 8.3 mcp.json 内容(标准 MCP 格式,凭证只存 env 引用)

```json
{
  "mcpServers": {
    "github": { "type": "streamable-http", "url": "https://api.githubcopilot.com/mcp/" },
    "local-db": { "type": "stdio", "command": "npx", "args": ["-y", "db-mcp"], "env": { "DB_TOKEN": "{env:DB_TOKEN}" } }
  }
}
```

server 内工具子集过滤(`tool_names`)参考 hikqin `mcp_services`,v1 内定。

## 9. 数据源(实例在 agent)

```json
// agents/<name>/data_sources.json
{
  "dataSources": [
    { "id": "orders", "type": "mysql", "dsn": "{env:ORDERS_DSN}", "description": "订单库" }
  ]
}
```

- **实例**(id/type/dsn_env/描述,凭证仅 `{env:XXX}`)写在本 agent 目录 → 物理私有,只有该 agent 能用(权限不用查表)。
- **工具与 type 能力**(db_schema/db_query、mysql/… 解析校验)由 db 插件提供;**装载与否由插件存在性门控**,见 [plugins.md](plugins.md)。
- 运行时:绑定的清单注入 system prompt(列可用库 + 先 schema 后 query、只读);db 工具从 ctx 取当前 agent 清单校验 `data_source_id`,工具不持有配置。

## 10. 导入 / 导出(import / export)

agent = 自包含目录 → 导入 = **拷贝目录 + 装载前校验**,无清单/解析/安装步骤(目录本身就是最小离线单元)。

```
qi agents export <name> [-o <path>]       # 导出:目录副本(可选 zip/tar.gz)
qi agents import <source> [-l] [--force]  # 导入:拷贝 + 校验
```

**导出产物 = 可直接 import 的最小单元**(agent.md + skills/ + assets/ + mcp.json + data_sources.json 全带走)。

### 来源与落点

| source | 行为 |
|---|---|
| `./path/to/agent` 本地目录 | 校验后复制 |
| `agent.tar.gz / .zip` | 解压后校验复制 |
| `git:https://…/agents` | 拉取;多 agent 时列出供选或按 `--name` |
| 单个 `agent.md` | 自动包成目录 |
| registry / 市场 | 远期 |

- 落点:`-l` → 项目 `.qi/agents/`;默认全局 `~/.qi/agents/`
- 冲突:目标已有同名 → **默认拒绝并提示**,`--force` 覆盖或 `--rename <新名>`(显式操作,不静默覆盖)

### 校验与安全

- 复用启动装载的**同一套 validator** 预检(name==目录名 / description / tools / include / skills 冲突):坏包在落盘前拦截
- **明文凭证扫描**:mcp.json / data_sources.json 只应有 `{env:XXX}`;发现 api_key/password 类明文 → 警告,`--allow-plaintext` 才放行
- 第三方 agent 的技能是可执行指令 → import 输出「内容未经审查」提示(对齐 pi 对 skills 的安全警告)

### 生态导入(二期,adapter 化)

Claude Code agent 卡(单 md)/ SKILL.md 技能目录 → 格式转换器注册机制导入:

```
qi agents import claude:…/foo.md       # 角色卡转换
qi agents import skill:path/to/skill   # 包装成私有技能进目标 agent
```

一期只导自家格式;adapter 走 plugins.md 的注册机制,不写死进核心。

## 11. 预留(二期)

- `handoffs`(允许主动转交的 agent 白名单)
- `guards`(输入/输出安全检查)

## 12. 决策记录

| 决策 | 结论 |
|---|---|
| 配置形态 | agent 定义 = **Markdown + frontmatter**(非 TOML);应用设置 = TOML |
| 技能归属 | **私有自包含**,随 agent 目录走;无共享技能库 |
| agent 形态 | 目录形式 `agents/<name>/`,入口固定 `agent.md` |
| include 机制 | frontmatter `include: [...]` 拼入 system prompt(显式) |
| 通用小能力 | 做成**全局代码工具**,不做成技能 |
| 存放位置 | 仅 2 处:`~/.qi/agents/` + `<项目>/.qi/agents/`;无内置、无 env 层 |
| 覆盖规则 | 项目版**静默覆盖**用户版(不警告);同层重复报错 |
| 目录命名 | 隐藏目录 `.qi`(全局 `~/.qi`,项目 `.qi`),对齐 pi 的 `.pi` |
| 内置内容 | 无内置 agent/技能;示例放 `examples/agents/`,不自动加载 |
| 无内置技能 | 撤销"内置技能覆盖"问题(H4 moot) |
| opening 字段 | v1 补充:message(agent 开场白,进会话历史)+ suggestions(UI 层快捷提问,不进历史) |
| 执行参数 | model / temperature / max_turns 不进 agent.md;统一归全局配置(模型/温度在 [models.*],轮次/超时在 [runtime]) |
| MCP | **v1**:私有 mcp.json 仅本 agent 自动绑定;全局 [mcp.servers] 按 `mcp_servers` 声明绑定,**默认无、显式声明**(凭证敏感);凭证仅 env 引用 |
| 数据源 | 实例在 agent 目录 data_sources.json(私有自动绑定,凭证 env);工具/type 能力由 db 插件提供,装载门控见 plugins.md |
| 导入/导出 | agent 自包含目录;import = 拷贝 + 复用装载校验器预检 + 明文凭证扫描;export 产物可直接 import |

## 13. 待定决策

- frontmatter 语法最终确认(YAML,推荐,对齐 Claude/Agent Skills 生态)
- 应用配置文件名与格式(`qi_agent.toml` 待定)
- sessions 存放位置(全局 `~/.qi/sessions` 还是项目内)
- `mcp_servers` 省略默认 = 全部全局 server(与 tools 三态一致,推荐)还是默认无、必须显式声明
