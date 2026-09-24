# qi-mcp

qi 的 MCP 支持。形状**照搬 [`pi-mcp-adapter`](https://pi.dev/packages/pi-mcp-adapter)**
(见 `design/extensions-design.md` 的 E25)。

## 为什么默认只给一个代理工具

一个 MCP server 的工具定义轻松 **10k+ token**,连几个 server,对话还没开始就烧掉半个上下文
窗口 —— 而多数工具这次对话根本用不到。所以默认注册**一个约 200 token 的 `mcp` 代理工具**:
模型先 `search` 发现有什么,再 `call` 调。server 默认 **lazy**,真调用才启动。

## 装

```bash
qi install qi-mcp        # 或 -e ./extensions/qi-mcp(开发)
```

装完 `qi` 下次启动自动发现它(entry point `qi.extensions`)。

## 写声明表(qi 的两层)

格式与 v1 一致,只有一份真相(**Agent Plugins 1.0 的写法也照收**):

```json
{
  "mcpServers": {
    "chrome-devtools": {
      "command": "npx",
      "args": ["-y", "chrome-devtools-mcp"]
    },
    "docs": { "url": "https://mcp.example.com/mcp" }
  }
}
```

Agent Plugins 1.0 写法(顶层 `$schema` + 每 server 的 `type`):

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "docs": { "type": "streamable-http", "url": "https://mcp.example.com/mcp" }
  }
}
```

顶层只读 `mcpServers` —— `$schema` 这样靠邻的键自然被忽略。server 里的 `type` 是**传输的
首选判定**;没写 `type` 的老声明仍按 `command` / `url` / `socket` 推断(两种写法都在用,
不至于把旧的判死)。

| `type` | 结果 |
| --- | --- |
| `stdio` | stdio 传输 |
| `streamable-http` | Streamable HTTP(`http` 这种生态简写也认) |
| `sse` | **明确报「传输尚未实现」** —— 旧 HTTP+SSE 不做(§7.5) |
| 其它 | 原样当传输名 → 连接时报「传输 `x` 尚未实现」,不被猜成能跑的传输 |

| 层 | 路径 | 说明 |
| --- | --- | --- |
| `global` | `~/.qi/agent/mcp.json` | 用户级基建,跨项目 |
| `project` | `<git 根>/.qi/mcp.json` | 跟仓库走,可提交共享 → **同名覆盖全局** |

**第三层(角色私有 `<角色目录>/mcp.json`)由 qi-agents 读** —— 它把 server 定义按值交给
qi-mcp(E25:agent 目录是 qi-agents 的自包含包,"包的主人认识包成员")。所以这里只认两层。

TUI 里 **`/mcp`** 是那面板,两个子命令分工是"快/慢":

| 命令 | 做什么 | 会连 server 吗 |
| --- | --- | --- |
| `/mcp` | 列声明表(带来源层与 disabled/directTools 标记) | **不会** —— 纯读 JSON,随时按随时回 |
| `/mcp tools [server]` | 列**工具清单**(名字 + 描述,直连的标 `[直连]`),带连接失败原因 | 会(按需) |
| `/mcp help` | 用法 | 不会 |

`tools` 不并进默认输出是故意的:元数据缓存是**进程内**的,所以列工具必须先连 server ——
并进去的话,一个起不来的 server 会让 `/mcp` 卡到连接超时。

**命令与代理工具同名(都叫 `mcp`)** —— 这没问题:两者不在同一个命名空间(命令进
`CommandRegistry`,工具进 `ToolCatalog`),pi 就是这个形状。

## 状态

五个切片全部落地:

| 切片 | 内容 | 状态 |
| --- | --- | --- |
| 1 | 两层声明表 + `/mcp` 面板（`/mcp tools` 列工具） | ✅ |
| 2 | `mcp` 代理工具(search / describe / call)+ lazy 连接 | ✅ |
| 2b/2c | 真客户端:stdio + Streamable HTTP(端到端跑真 server) | ✅ |
| 3 | `directTools` 直连 + `includeTools`/`excludeTools` 通配 + `toolPrefix` | ✅ |
| 4 | 给 qi-agents 的按值注入 API | ✅ |

**尚未实现,别按它配置**:`lifecycle` / `idleTimeout` / `debug` / `oauth` / `caFile` / 状态快照事件。

它们会出现在 `/mcp` 的“未识别字段”提示里 —— 这是**故意的**,口径就一条:

> `KNOWN_FIELDS` = **代码真的会读的**字段,不是“生态里存在这个词”。

所以两份名单不能混为一谈（两个方向都真错过一次，属静默失效）：

| 情况 | 后果 | 处理 |
| --- | --- | --- |
| `lifecycle` / `idleTimeout` / `debug`：**声明了但没读** | 写下去“没效果 + 没提示” | ⇒ 留在**表外**，让它显形 |
| `auth` / `bearerToken` / `bearerTokenEnv` / `inheritEnv` / `requestTimeoutMs`：**读了却没进表** | 面板把**正在生效**的字段报成“我不认识” | ⇒ 进表 |

这条不变量由 `tests/test_qi_mcp_config.py` 里那条源码扫描测试盯着（双向相等）。

## 连接什么时候关

`session_shutdown`（退出 / 换会话 / 重载前，宿主会发）→ 关掉**两层**缓存的全部连接：
本模块里每 cwd 一个的 manager，以及 `role.py` 里每个角色目录一个的 manager。

这样 stdio 子进程（`npx` / `node`）与 HTTP 连接池在退出时会真的收掉；不接这个事件的话，
`aclose()` 写了也等于没写（它曾经就是“有实现、有测试、**没有调用方**”）。

## 连接生命周期（并发 / 超时 / 重连）

这三件事同在 `servers.py` + `client.py` 的一条线上，一起说：

| 事 | 行为 | 为什么 |
| --- | --- | --- |
| **并发连** | `tools()` 用 `asyncio.gather` 同时连全部 active server；连接锁是**每 server 一把** | 以前是一把全局锁、而且跨 `await connect()` 持有 —— 一个慢 server 会把其余全部挡在外面，最坏等满它的连接超时（60s）。同一 server 仍只连一次（第二个等锁的人拿到同一份） |
| **有界列工具** | `requestTimeoutMs` 定在 `ClientSession(read_timeout_seconds=…)` 上，**覆盖所有请求** | `ClientSession.list_tools()` **不收**这个参数（签名只有 `params`），所以以前只有 `call_tool` 有超时 —— 列工具卡住会让 `mcp({search})` 与 `/mcp tools` **永久挂起**。定在会话上之后 `initialize` 也一起有界。该值只用于 `send_request`（另有一处是订阅流，本扩展不用），**不会**给空闲读循环上铊 |
| **断线重连** | `client()` 先 `client.alive()`；死的丢掉重连，并丢掉工具缓存 | 以前只看 `name in self._clients`、**不查存活** —— server 中途崩掉后，那个死 client 会被一直返回，整条会话都得到 `连接已关闭` |

`tools_of(name)` **只连那一个** server：直连注册是一个一个问的，以前它走 `tools()` 会把
全部 server 都拉起来（给 3 个 server 开 `directTools` = 启动时连全部）。

## 搜索是**分页**的

`mcp({search})` 默认只给 **12** 个匹配（`limit` 可调，硬上限 50），并在输出里说明
「共 N 个匹配 / 这是第几段 / 下一页怎么写」。

不是为了省字节 —— 是因为**不分页就自相矛盾**：这个代理工具存在的理由正是"不把每个 server
的全部工具定义塞进上下文"，而一次宽泛的查询会把匹配到的全部工具连同 schema 倒出来，正好把它
倒满。上限也不是装饰：`limit` 是**模型给的**，没有硬上限时一句 `limit: 100000` 就绕过去了。
`limit` / `offset` 一律容错（字符串、`None`、负数都夹到合法区间），一个写歪的分页参数不该让
整次 search 失败。

## 输出护栏（单个结果不能打爆上下文）

MCP 结果是**第三方 server 给的**，而 `runtime.py` 那道上限只管**落盘**（避免单条工具结果
撑破会话文件）—— 模型上下文这边以前没有任何限制，一个返回几 MB 文本的 server 能一次把窗口打爆。

现在所有结果都过 `_render_call_result`（代理工具与直连工具的**唯一**收口）里的护栏：

| 上限 | 值 | 为什么是这个数 |
| --- | --- | --- |
| 字符 | 50 000 | 与 qi 内置工具的 `MAX_FILE_CHARS` 及 pi 的 output guard 同一量级 —— 字符数才是撑窗口的那条 |
| 行 | 2 000 | pi 的数。**不是** qi 内置工具的 200：那个数是给 `ls` / `grep` 这类自产输出调的，拿它卡第三方 API 的正常 JSON 会丢真数据 |

超限 → 留 head，再接一句 `…(MCP 结果过长已截断:共 N 行 / M 字,上面只给了前 …)`。
**带上总量是有意的**：不说的话模型会把截断当成“就这么多”，然后基于残缺数据下结论。

口径与 qi 自己的工具一致（**截断 + 说明**，不做落盘 spill —— `tools/__init__.py` 的
`_truncate` / `read` 都是这个形状）。

急用时的逃生口：`MCP_OUTPUT_GUARD=0`（与 pi 同名同义）。

## 角色怎么拿到 MCP(与 v1 的差别)

v1 的模型更严:全局/项目两层只是**声明表**,哪个角色真能看到由 `agent.md` 的 `mcp_servers`
显式点名(凭证敏感 → 默认无)。E25 的模型是:角色的 server 集合 = 它自己的 `mcp.json` +
qi 两层(**角色私有同名覆盖 qi 级**),能不能调由 `tools:` 里写什么决定:

| 角色 `tools:` 里写到 | 拿到什么 |
| --- | --- |
| 什么都不写 | **没有任何 MCP 访问**(默认拒绝;连都不连) |
| `mcp` | 那个全局代理工具(覆盖 qi 两层);**角色私有 server 的工具额外直连** —— 全局代理看不见它们 |
| `mcp__github__create_*` | 直连匹配的工具(**不给代理**) |

所以**不再要求逐 server 点名**:闸门在 `tools:` 这一行,粒度可以细到工具名(`fnmatch` 通配)。
实现见 `qi_mcp/role.py`。
