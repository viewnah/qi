# qi-agents

qi 的**角色系统**扩展:角色发现 + 角色选择 + `subagent` 委派。

它原本住在 core 里(P-E4c 移出,见 `design/extensions-design.md` 的 E14/E15)。现在 core 只认识运行单元
`{name, prompt, tools}`,「角色」这个概念由本扩展提供 —— 只用 qi 的公开扩展面,core 一行不用改。

## 装

```bash
qi install qi-agents            # 或 -e ./extensions/qi-agents(开发)
```

它会一并装上 **qi-mcp**(E25 定的硬依赖)—— 角色自带的 MCP server 由 qi-mcp 负责连与注册。

装完 `qi` 下次启动就会自动发现它(entry point `qi.extensions`)。

## 写一个角色

一个目录一份 `agent.md`,frontmatter 声明元信息,**正文就是那个角色的系统提示词**:

```markdown
---
name: reviewer
description: 只读审查员,专挑缺陷;不改代码
tools: read, grep, glob
model: anthropic/claude-sonnet-4
---

你是一名严格的代码审查员。只读代码,指出具体缺陷与风险,给最小修法。
不要重写整个文件,不要改动任何东西。
```

放哪儿:

| 位置 | 作用域 | 说明 |
| --- | --- | --- |
| `~/.qi/agent/agents/<名>/agent.md` | `user` | 跨项目,你自己的常用角色 |
| `<项目>/.qi/agents/<名>/agent.md` | `project` | 跟仓库走;**同名覆盖**用户级 |

`tools` 支持 `read, grep`(逗号)或 `[read, grep]`(列表);省略 = **继承父**(不是"全部工具"——
工具省略若解释成"全部",一个角色就能悄悄拿到比父更多的权限,那是一条提权路径)。
`description` 必填:模型靠它挑角色。

### frontmatter 字段(全部)

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | ✅ | 角色名。也用 `qi --ext agent=<name>` 与 `subagent` 的 `agent` 参数引用它 |
| `description` | ✅ | 供模型挑选角色;也会展示给人 |
| `tools` | | 工具名单,**省略 = 继承父**;另有 MCP 的两种写法(`mcp` / `mcp__server__*`)见下文 |
| `disallowed_tools` | | 从上面的结果里**减掉**的名单(denylist,支持 `fnmatch` 通配如 `mcp__github__*`)。`tools` 省略时先把父的当前集合取出来再减;两边都列到就移除 |
| `model` | | `provider/model`;省略 = 继承主会话当前模型 |

正文(Markdown)就是那个角色的系统提示词,会拼在**基座之后**。

**不支持的字段(写了不会生效,不会报错)**:`display_name` `keywords` `include` `opening`
`data_sources` —— 这几个是 v2/v3 设计里出现过的概念,现在都不在解析范围内。
尤其是 `include`(拼入 assets)与 `data_sources`,**已经不存在实现**;要拼参考内容就直接写进正文,
要外部数据就用自己的工具或 MCP。

## 用

```bash
qi --ext agent=reviewer "审一下 src/ 里的错误处理"     # 以此角色运行主会话
```

```bash
qi "看看这个模块"                                       # 用 subagent 工具把活派出去
> 你: 让 scout 查一下这个函数的调用方
```

`subagent` 工具三种模式:

```jsonc
{"agent": "scout", "task": "查调用方"}                       // single
{"tasks": [{"agent": "scout", "task": "A"}, …]}              // parallel(并发 4,最多 8 个)
{"chain": [{"agent": "scout", "task": "先查"},
           {"agent": "writer", "task": "基于 {previous} 写文档"}]}   // chain:上一棒输出代入 {previous}
```

`agentScope` 参数决定看哪层角色(`user` 默认 / `project` / `both`)。
**项目级角色是仓库控制的提示词**,所以走项目层时要显式指定,且项目未被信任时会先问一句。

**同一条闸门也管 `--ext agent=<项目角色>`**:那是"以这个角色起主会话",风险与
`agentScope="project"` 完全一样(仓库里的一个 `agent.md` 就能改系统提示词),所以
未信任时会**跳过角色层并在 notes 里说明**(无界面时 headless 也一样 fail-closed)。
用户级角色不受影响。

TUI 里 `/agents` 列角色(带 `both` 看项目级)。

## 角色怎么拿到 MCP 工具(`tools:` 的第二种语义)

角色可以带自己的 MCP server:`<角色目录>/mcp.json`(**qi-agents 自己读它** —— agent 目录是
它的自包含包,包主人点自己的成员;qi-mcp 只管连与注册)。角色的 server 集合 =
**qi 两层**(`~/.qi/agent/mcp.json` → `<git 根>/.qi/mcp.json`)**+ 角色私有那份**,
同名时**角色私有覆盖** qi 级。

能不能**用**它们由 `tools:` 决定 —— 而这里与内置工具的语义**不一样**,值得说清楚:

| `tools:` 里写到 | 拿到什么 |
| --- | --- |
| 什么都不写 | **没有任何 MCP 访问**(默认拒绝,而且连都不连) |
| `mcp` | 那个全局**代理**工具(`mcp({search:"…"})` 发现 → `mcp({tool,args})` 调用),覆盖 qi 两层;**角色私有的 server 额外直连注册**(全局代理看不见它们) |
| `mcp__github__*` / `mcp__github__create_issue` | **直连**匹配的工具(**不给代理**)—— 适合"这个角色就常用这几把" |

```markdown
---
name: reviewer
description: 只读审查员
tools: read, grep, mcp__github__*     # 内置工具 + 只授权 github 的 MCP 工具
---

你是一名严格的审查员……
```

**为什么不一致**:内置工具是"列出来的才有"(`tools:` 就是全集);MCP 的 **server 集合**由
`mcp.json` 决定,`tools:` 决定的是**接入方式**(走代理还是直连、授权哪些)。一句话读法:
**`tools:` = 这个角色要哪些内置工具 + MCP 走哪条路**。

好处是白名单仍然meaningful:角色的 MCP 面 = **它按需注册的 server** ∩ 白名单 ——
不写就没有,写了也不会因此多拿到别的 server。(这条不对称是 E25 明确记下的代价。)

## 角色私有技能

除顶层技能([skills.md](../../docs/skills.md) §1 的六层)之外,角色还可以带**自己的**技能目录:

```text
<角色目录>/skills/<技能名>/SKILL.md
```

- **单层扫描**:只认 `skills/<名>/SKILL.md`,不递归、不接受根级散落的 `*.md`;
- 同一角色内同名直接报错;
- 这些技能只在这个角色跑的时候可见,并且**优先于顶层同名技能**(越具体越优先);
- 注入形态与顶层技能一致(name/description/location 进提示词、正文按需 `read`)。

角色目录是一个**自包含包**:人设(`agent.md`)、私有技能、私有 MCP 声明都在里面,复制整个目录就能带走。

## 为什么子 agent 是**进程内**的

`api.runAgent` 在本进程里跑受管子运行,不是起子进程。Python 每进程首次调用 LLM 要付
litellm 的导入成本(实测 ~6.8s 冷热稳定),子进程等于 6.8s × N(8 个并行 ≈ 54s);
Node 起一次只要 0.04s,所以 pi 的子进程模型不能平移到这里。代价是子运行与主会话共享进程
(异常/内存不隔离),收益是并行委派真的能用。

**递归防护是结构性的**:子运行的工具清单里去掉 `subagent` 本身,所以子角色不可能再起子角色。
这条**对 `tools:` 省略(继承父)的角色同样成立** —— 继承那一路会先把父的当前集合取出来、
再过同一道过滤器,而不是把 `None` 交给宿主去解析(那样父集合里的 `subagent` 会漏下去)。

**中断信号**:`ctx` 有两个形状 —— 工具侧(`Tool.execute` 的第 2 个参数)是 `ToolContext`,
信号在 `abort` 字段上;handler 侧是 `ExtensionContext`,信号在 `signal` 上而 `abort` 是**方法**。
扩展里用 `_abort_signal()` 两边都认(只认其中一个的话,另一条路上会拿到 `None` —— 静默降级,
症状只是"Esc 杀不掉子 agent")。

## 已知限制

- 子运行的中间过程不回流(`on_event` 未接):界面只看到"子 agent 在跑"和最终结果,
  看不到它内部每次工具调用。要做得先定渲染契约。
- 角色的 `model` 走 `provider/model` 字符串;没有按角色的温度/最大轮数等更细的设置。
- `/agents` 是只读列表,没有交互式挑选(pi 有 `/agents` 面板)。
