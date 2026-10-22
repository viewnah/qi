# qi-agents

qi 的**角色系统**扩展:角色发现 + 角色选择 + `subagent` 委派(预算制分层,子角色还能再派)。

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
| `delegate` | | 能不能用 `subagent` **继续往下派**。缺省 `true`;`false` = 到此为止。见下文“分层委派”;`disallowed_tools: [subagent]` 仍一票否决,显式 `tools:` 名单里没写 `subagent` 也派不出去 |

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

## 远端角色:把 A2A 智能体当成一个角色

装了 [qi-a2a](../qi-a2a) 且它的 `a2a.json` 里有**非网关**条目(写了 `cardUrl`,或给了
`tenant`)时,那些条目会**自动出现在角色表**里,`source` 是 `a2a`:

```jsonc
// ~/.qi/agent/a2a.json
{
  "a2aServers": {
    "skill-debug": {                       // ← 会成为一个角色
      "cardUrl": "http://host:6091/a2a/agents/agent-xxxx/card.json",
      "description": "Hikqin 的技能调试专家"   // ← 模型挑角色的唯一依据,别省
    },
    "hikqin": { "baseUrl": "http://host:6091" }   // ← 网关,**不是**角色
  }
}
```

然后照常用 `subagent` 委派,与本地角色没有区别:

```jsonc
{"agent": "skill-debug", "task": "查一下这个技能为什么解析失败"}
```

几种边界:

| 情形 | 行为 |
| --- | --- |
| 只给 `baseUrl`(网关) | **不是**角色 —— 它后面挂着 N 个智能体,枚举要联网,而角色发现不能联网。继续用 `a2a` 工具带 `agent` 调 |
| 名字与本地角色重名 | **本地赢**(远端是别人家的,不该顶掉你自己写的角色) |
| `agentScope` | `user` → 全局层,`project` → 项目层(受信任门控),`both` → 两层 |
| 描述留空 | 会补一句 `远端 A2A 智能体 <名>`,但模型基本选不准 —— 填上 |
| Q：它能再往下派吗？ | 不能。远端是别人家的 agent,没有我们的 `subagent` 工具与预算/深度表,深度链在它这里终止 |
| 它返回 `input-required` | 算**成功**(把话带回来了),正文写清"没执行"与回传方式；接着用 `a2a(action="resume", …)` |
| 远端连不上 | 与本地子运行同一条规矩:**失败是结果**,不抛、不带倒同批其它任务 |
| `--ext agent=<远端角色>` | 会被拒并说明原因 —— 它没有本地提示词，当"会话人格"是静默无效 |

**为什么不是"多一个工具"**：`a2a` 代理工具与远端角色解决的是不同的事 ——
前者**接着当前对话说**(共享上下文、要自己传 `contextId`),后者是**一次独立委派**
(独立上下文、跑完就结束)。模型只要按"要不要把这块活交出去"来选就行。

实现落点：`qi-a2a/remote.py` 导出角色与"跑一次"的入口；`qi-agents` 只在
`discovery.py` 里合并它们(`Role.remote` 是不透明句柄)，`subagent._run_one` 见句柄
就分流到 `_run_remote` —— 协议细节一行都没进 qi-agents。

## 分层委派:子角色还能再派(预算制)

`subagent` 不限一层:拿到 `subagent` 的角色,在自己的运行里可以继续调 `subagent` 把活再往下派。
为了不让这件事变成失控递归,**五道闸**同时把着(任一不过,`subagent` 就不在那一层的工具
清单里,或这次调用被整笔拒绝):

| 闸 | 判据 | 不过会怎样 |
| --- | --- | --- |
| 身份闸 | `ctx.agent_name` 能在运行登记表里查到(主会话 = 根,深度 0) | fail-closed:拒绝这次调用,不起子运行 |
| 角色闸 | frontmatter `delegate`(缺省 `true`) | 这一层的清单里剥掉 `subagent` |
| 深度闸 | `MAX_DEPTH = 3`(主会话 = 0,最多 3 层子运行) | 同上,到顶就不再往下 |
| 名额闸 | 一次顶层调用的整棵委派树共享 **64 个 spawn 名额**(只减不增,链断/失败不退) | 整笔调用被拒绝(链模式则断在该步) |
| 否决闸 | `disallowed_tools` 里写了 `subagent` | 在**所有授权之后**应用,一票否决 |

**深度从哪来**:每个子运行起的时候用唯一名(`scout`、`scout#2`…)登记
`{深度, 最终工具清单, 剩余名额}`;调 `subagent` 时拿 `ctx.agent_name` 查表
(宿主给子运行的 `agent_name` 就是 `spec["name"]`)。查不到表、又不是主会话的调用者
一律 fail-closed 拒绝 —— 不用 contextvar / 进程级计数器:那类机制“坏了”时会静默读成
深度 0,恰好是放行无限递归的方向;查表 miss 是显式失败,坏在安全的那一边。

**继承父只继承该继承的**:`tools:` 省略的子角色继承的是**发起它那一层**的清单(不是主会话的
当前集合),所以“这一层被剥掉的工具”不会在下一层被悄悄加回来。

**进度与结果**:过程走**三条输出面**(格式契约在 `render.py`):

- **时间线**(对话流,append-only)——**前端零扩展输出,过程与节点全归卡**:
  卡标题已有 `▸ 名字 ← 父 · 深度N ✓/✗`,对话流再推完成块 `── 名字 ✓ 深度N ──`
  就是同一件事写两处 —— 前端时间线只剩 TUI 原生 `subagent` 工具块(宿主平铺卡)
  与主会话自己的内容;**黄标题/`·` 活动行/`✎` 思考/完成块都只进运行卡**。
  仅**无前端**(headless/notes,没有卡可显示)才把完成块(节点 + 结论摘要 3 行)
  与过程全量落时间线 —— 那时它是唯一日志;
- **平铺卡**(实时,每 agent 一张):`render_call` 返回**平铺架子**入队;**首层**
  在 `run_subagent` 按 FIFO 认领出队(入队在同协程链上先于工具执行,事件时还有
  认领兜底),**嵌套子层经 `_RunInfo.holder` 继承同一个架子** —— 子调用没有自己
  的工具块,在架子上追加自己的一张卡 = **全树拍平**;卡标题 = `▸ 名字 ← 由谁指派
  · 深度N`(运行中 cyan、完成后 ✓/✗)。`thinking_delta`/`text_delta` 累计进当前轮
  缓冲、**节流 0.3s** 刷卡,轮末强制 flush —— 思考/输出在卡里**逐字滚**,正文区内滚
  **每次刷新自动 scroll_end 跟底**(不用手动滚找进度)。默认展开(流式就是给人看的),
  **点击可折叠**;run 收束 `set_done` 定格并收起为**摘要**(输出前 3 行 + 提示),
  **点击展开看该 agent 全文**(活动/各轮思考/输出)。**架子头部**是这次调用的
  摘要(`委派 → 目标角色`,**不带 `▸`** —— `▸` 专属可点折叠的 agent 卡,避免
  "看起来能点却点不动");**高度两态(`_live`)**:实时态(没人碰过)固定 12 行
  内滚 + 自动跟底(看最新);**首次点击 / `set_done` / 全局开合 → 浏览态**,
  高度 `auto` **完全撑开、无内部滚动条,点开即整段看全**(不强制拉底,自己滚)。
- 给**模型 / 导出 / 回放**的 `result` = **横幅分段**
  (`── 名字 (深度N, 状态) ──`)——**不画树地图**:委派关系在活动行
  (`· subagent …` 派单)与横幅深度里已经自明,地图是重复的第二份信息。
  **每段三区:`·` 该层活动(上限 30 行)→ `✎` 该层各轮思考(6 轮×600 字)→ 输出**
  —— 一个 agent 干了什么、想了什么、说了什么,物理相邻;归属写在横幅里,不靠
  空格缩进(纯文本场景会吃掉它)。收尾轮不吐文本的模型(如 mimo-flash)会指向本段
  思考区,再不行给占位符 —— 段绝不留空横幅;
- **ctrl+o 全局开合**:`render_result` 返回 `None`(树卡退役 —— 各卡 `set_done`
  已各自定格);前端拿到 None 把内置 `tool_body` 转发进容器的 `set_output`,被吞成
  **对整架子所有卡的同步折叠/展开**(不会把纯文本覆盖进平铺内容);
  `·` 活动行 dim、`✎` 思考行 dim 斜体。不碰 core;两个钩子必须**一起挂**,
  只挂 result 时卡片还是内置 `ToolBlock`,自绘路径不会走;
- `details.ui` 仍进事件与会话存档(`depth` 标明层级)—— TUI 不渲染 `details`,
  它不是界面通道。

## 角色怎么拿到 MCP 工具(`tools:` 的第二种语义)

角色可以带自己的 MCP server:`<角色目录>/mcp.json`(**qi-agents 自己读它** —— agent 目录是
它的自包含包,包主人点自己的成员;qi-mcp 只管连与注册)。角色的 server 集合 =
**qi 两层**(`~/.qi/agent/mcp.json` → `<git 根>/.qi/mcp.json`)**+ 角色私有那份**,
同名时**角色私有覆盖** qi 级。

能不能**用**它们由 `tools:` 决定 —— 而这里与内置工具的语义**不一样**,值得说清楚:

| `tools:` 里写到 | 拿到什么 |
| --- | --- |
| **什么都不写** | 角色**私有**的 server 全部直连接上（全部工具，或 `includeTools` 限定的那些）；qi 两层**不接** —— 那是父会话的事（父有 `mcp` 代理就继续继承） |
| 只写内置工具（`read, grep`） | 没有任何 MCP 访问（默认拒绝；连都不连） |
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

**递归防护是预算制的**(E12/E15 说的“深度上限由扩展自己管”):子角色**可以**继续往下派,
由上一节的五道闸把着(身份 fail-closed、`delegate`、`MAX_DEPTH=3`、64 名额、denylist 否决)。
`tools:` 省略(继承父)的角色同样只走这**同一道**出口 —— 继承那一路会先把发起者那一层的
清单取出来再过闸,而不是把 `None` 交给宿主去解析(那样上一层被剥掉的 `subagent` 会漏回来)。

**中断信号**:`ctx` 有两个形状 —— 工具侧(`Tool.execute` 的第 2 个参数)是 `ToolContext`,
信号在 `abort` 字段上;handler 侧是 `ExtensionContext`,信号在 `signal` 上而 `abort` 是**方法**。
扩展里用 `_abort_signal()` 两边都认(只认其中一个的话,另一条路上会拿到 `None` —— 静默降级,
症状只是"Esc 杀不掉子 agent")。

## 已知限制

- 时间线是 append-only 的:运行中按**时间线**分组(agent 切换才重新出标题,嵌套
  返回时标题会再现),“同一 agent 的内容物理相邻”只在结束后的卡片 / result 里成立;
  流式刷卡节流 0.3s;一次工具调用一个平铺架子,其中每个 agent 各有一张卡(流进
  各自的卡,不串);完整事件流的总账要看 result 分段与会话存档。
- 角色的 `model` 走 `provider/model` 字符串;没有按角色的温度/最大轮数等更细的设置。
- `/agents` 是只读列表,没有交互式挑选(pi 有 `/agents` 面板)。
