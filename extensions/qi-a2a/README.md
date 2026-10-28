# qi-a2a

> **qi-lab 内置扩展**：安装 `qi-lab` 时已随包提供，无需单独安装。下面的安装 / CLI 用法是它作为独立扩展时的路径；提取发布后仍按这些步骤使用。

qi 的 A2A(Agent2Agent)扩展:用标准协议调用远端智能体,内置对
[Hikqin](../../data/hikqin) A2A 网关的支持。

- **一个条目 = 一个智能体**(一张 Agent Card);`baseUrl` 不带 `tenant` 时它是网关。
- **两层配置**:`~/.qi/agent/a2a.json` → `<cwd>/.qi/a2a.json`,同名**深合并**。
- **一个代理工具** `a2a`(约 200 token),而不是把每个远端技能塞进工具清单。
- **协议交给官方 [`a2a-sdk`](https://pypi.org/project/a2a-sdk/)**(与 Hikqin 同版本):
  方法名、`A2A-Version` 头、proto oneof、`tenant` 装饰器都由 SDK 负责,本扩展不做协议适配。
- **也能当角色被委派**(装了 [qi-agents](../qi-agents) 时):非网关条目会自动出现在
  `subagent` 的角色表里(`source=a2a`)—— 见下面「两种用法」。

## 两种用法:同一个智能体,两套上下文

| | `a2a` 代理工具 | 远端角色(委派) |
| --- | --- | --- |
| 谁发起 | 主会话自己 | 模型通过 `subagent` 派活 |
| 上下文 | **接着当前对话说**(要自己传 `contextId`) | **一次独立对话**,跑完就结束 |
| 多轮 | 由调用方维护 `contextId` | 一次委派的输入输出就是全部 |
| 适合 | "跟我一起聊这个" | "把这块活交给它去做" |
| 要求 | 任何条目(网关也行) | **非网关**条目(有 `cardUrl` 或 `tenant`) |

网关条目(只有 `baseUrl`)不能当角色:枚举它后面的智能体要联网拉名片,而角色发现
跑在每次 `subagent` 调用上,那里不能联网 —— 网关继续用 `a2a` 工具带 `agent` 调。

```jsonc
{ "a2aServers": {
    "skill-debug": { "cardUrl": "…/card.json", "description": "技能调试专家" },  // 角色 + 工具都能用
    "hikqin":      { "baseUrl": "http://host:6091" }                            // 只能当工具的目标
} }
```

远端角色**不能再往下派**:它是别人家的 agent,没有我们的 `subagent` 工具与预算/深度表。
回传 `input-required` 时算委派成功(把话带回来了),接着用 `a2a(action="resume", …)`。

## 正文是流式的

A2A 的 `artifactUpdate` 本来就是增量的,所以 `run_remote` 把它**逐片转发**出去
(`on_text(delta, *, replace)`),而不是等整轮跑完再给一整段:

| 参数 | 含义 |
| --- | --- |
| `replace=True` | 之前收到的作废,从这段重新开始(A2A 的 `append` 缺省为 false,Hikqin 首片就是这样) |
| `replace=False` | 追加 |

聚合仍由 `client` 的 `_Aggregate` 负责(`run.text` 是权威全文)—— 两边各算一遍必然会在
某处不一致。非流式远端(`message/send`)一次给全,`on_text` 不会被调用,调用方拿
`run.text` 兜底。qi-lab 的对话页面据此边收边画。


## 装

```bash
qi install ./extensions/qi-a2a     # 从本地目录装
# 或开发用:pip install -e extensions/qi-a2a
```

装完下次启动自动发现(entry point `qi.extensions` → `qi_a2a:register`)。

## 测试

用例在 `extensions/qi-a2a/tests/`(与 `qi-agents` / `qi-lab` 同一种布局),已登记在根
`pyproject.toml` 的 `testpaths` 里:

```bash
pytest extensions/qi-a2a/tests -q
```

全部离线:名片与流式响应都走 `httpx.MockTransport` 注入 SDK 的 `ClientConfig.httpx_client`,
不连真网关。

## 配置

| 层 | 路径 | 说明 |
| --- | --- | --- |
| `global` | `~/.qi/agent/a2a.json` | 用户级基建,跨项目 |
| `project` | `<cwd>/.qi/a2a.json` | 跟仓库走,可提交共享 → 同名**深合并** |

```json
{
  "a2aServers": {
    "hikqin-analysis": {
      "cardUrl": "http://127.0.0.1:8080/a2a/agents/agent-3fa85c64/card.json",
      "description": "Hikqin 数据分析智能体"
    },
    "hikqin": {
      "baseUrl": "http://127.0.0.1:8080",
      "description": "Hikqin 全平台网关:列出全部已发布智能体"
    }
  }
}
```

### 三种写法(功能等价,成本不同)

| 写法 | RPC 端点 | tenant | 每次 send 的额外请求 | 适用 |
| --- | --- | --- | --- | --- |
| `cardUrl` | 卡片里的 `supportedInterfaces[0].url` | 卡片里的 `tenant` | **1 次 GET 名片** | 只拿到卡片地址 |
| `baseUrl` + `tenant` | `{baseUrl}/a2a` | 配置 | **1 次 GET 名片** | 已知 `agents.id` |
| `rpcUrl` + `tenant` | 配置 | 配置 | **0** | 端点已知、想省掉发现 |
| `baseUrl` | `{baseUrl}/a2a` | 调用时用 `agent` 选 | **1 次 GET 列表** | 网关 |

只填 `cardUrl` 时,地址与 tenant 全部来自名片 —— Hikqin 的名片按**调用方实际访问的 host**
生成,手抄 `baseUrl` 容易出现 `127.0.0.1` / `localhost` 不一致。

不想多那次 GET、也不怕手抄地址,就写 `rpcUrl` + `tenant`:这条路径用**配置合成卡**,
零网络发现。(合成卡会自己带上 `protocolBinding: JSONRPC` 与 `capabilities.streaming`,
否则 SDK 会报找不到传输或静默改成非流式。)

### 名片给什么、配置给什么

填了 `cardUrl` **不等于别的都不用写** —— 名片里只包含下列四项:

| 来自名片 | 只能靠配置 |
| --- | --- |
| RPC 端点(`url`)、`tenant`、名字/描述、`skills` | `headers`(认证/网关要求) |
| | `timeoutMs`、`streaming` |
| | `discovery`(对象还是数组) |
| | `directTools`、`toolPrefix`、`disabled` |
| | `description`(仅本地面板,/a2a 与 list) |

其中 `description` 是唯一容易误会的一项:**名片条目用自己的远端描述**,配置里写的只用于
`/a2a` 面板与 `list`;只有 `rpcUrl` 合成卡才把它当作名字与描述。

### 字段

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `cardUrl` | string | — | Agent Card 地址;数组响应会展开成多个智能体 |
| `baseUrl` | string | — | 网关基址;`/a2a` 与 `/a2a/agents` 由它推导 |
| `rpcUrl` | string | `{baseUrl}/a2a` | 完整覆盖 JSON-RPC 地址;与 `tenant` 一起用可免去名片发现 |
| `tenant` | string | — | 固定到某个智能体(Hikqin 的 `agents.id`) |
| `description` | string | — | 给人看的说明,**不进系统提示词**;名片条目下不覆盖远端描述 |
| `headers` | object | `{}` | 附加请求头;值支持 `${ENV}` |
| `timeoutMs` | int | `600000` | 单次 RPC 总超时(含流式读取) |
| `streaming` | bool | `true` | 优先 `message/stream`;名片未声明能力时以它为准 |
| `discovery` | `auto`/`single`/`list` | `auto` | 期望名片是对象还是数组 |
| `directTools` | bool \| string[] | `false` | 为条目注册独立工具 |
| `toolPrefix` | string | `a2a` | 直连工具名前缀 |
| `disabled` | bool | `false` | 只有字面 `true` 生效 |

### 合并规则

- 嵌套对象递归合并,数组**整体替换**,`null` **删除键**;
- 项目层只写差异即可:

```json
{ "a2aServers": { "hikqin-analysis": { "headers": { "Authorization": "Bearer ${TOKEN}" } } } }
```

- 关掉全局的同名条目:

```json
{ "a2aServers": { "hikqin-analysis": { "disabled": true } } }
```

### 环境变量

`${VAR}` 会在**读取时**展开(URL / headers / description 都可)。缺变量会直接报错并指出
条目与变量名 —— 静默留一个 `${TOKEN}` 发出去只会得到 401,更难查。

## 用

默认只注册一个代理工具:

| action | 参数 | 作用 |
| --- | --- | --- |
| `list` | `discover?` | 列条目(默认**不联网**;`discover=true` 才刷新名片) |
| `card` | `server`, `agent?` | 看名片:名字 / tenant / 技能 / RPC 端点 |
| `send` | `server?`, `message`, `agent?`, `context_id?` | 发消息,聚合到终态 |
| `resume` | `server?`, `task_id`, `decisions` | 回传审批决策 |
| `get` / `cancel` | `server?`, `task_id` | 查询 / 取消任务 |

只配了一个条目时 `server` 可省略;条目下有多个智能体时用 `agent` 指定(tenant 或名字)。

多轮对话把 `context_id` 传回来;不传时本扩展会**复用本会话最近一次**(进程内缓存,
重启后请从对话历史里取)。

### 直连工具(opt-in)

```json
{ "a2aServers": { "daily-report": {
    "cardUrl": "http://gw/a2a/agents/agent-1/card.json",
    "directTools": true
} } }
```

会多出工具 `a2a__daily_report`。适合高频固定调用;**只在 `session_start` 注册,不联网**,
名片在真正调用时才取。

### 面板

```
/a2a            # 条目、来源层、请求头键名、未识别字段、配置文件路径
/a2a hikqin     # 只看某个条目
```

面板**不联网**:一个挂掉的网关不该让面板卡住。

## 人在环(HITL)

远端返回 `input-required` 时,工具结果会带上 `action_requests` / `review_configs`,
并把 `task_id` 一起回给你:

```
[input-required] server=hikqin task_id=t7 context_id=c7

需要决策(input-required):
  1. delete_file id=a1 args={"path": "t.txt"}
允许的 decisions:approve, reject
回传:a2a(action="resume", task_id="t7", decisions=[{"type": "approve"}])
```

## 安全

- **项目层受信任门控**:项目未受信任时整层 `a2a.json` 跳过(`-a` 信任本次运行)。
  项目配置能把对话内容指向任意端点,风险与项目级扩展同级。
- **密钥只走环境变量**:`/a2a` 面板与日志只显示请求头的**键名**,不显示值。
- **名片是数据,不是指令**:远端的 `description` / `skills` 只进工具结果,**绝不拼进系统提示词**。
- **不跟随跨域重定向**:避免 `Authorization` 被带到别的 host。
- 结果有 40k 字符上限;`list` / `card` 最多列 50 个智能体。

## 协议细节

协议由官方 `a2a-sdk` 处理,这里只记几条**实测**事实(便于排查):

- SDK 发出的是 PascalCase 方法名(`SendStreamingMessage` / `SendMessage` / `GetTask`),
  并自动带 `A2A-Version: 1.0`;`tenant` 由卡片 `supportedInterfaces[].tenant` 注入
  `params.tenant`。这些**不需要**在配置里写。
- 流里的事件是 proto oneof 包装:`{"task": …}` / `{"statusUpdate": …}` /
  `{"artifactUpdate": …}`。首个 `artifactUpdate` **不带** `append`(= 替换),
  后续带 `append: true`(= 追加) —— 聚合按这个语义走。
- 状态名统一归一成小写连字符(`TASK_STATE_INPUT_REQUIRED` → `input-required`)。
- Hikqin 的**列表**端点(`/a2a/agents`)返回数组,SDK 的 `A2ACardResolver` 只认单卡;
  所以名片用 httpx 另取,再交给 SDK 的 proto 类型。

## 不做什么

- 不支持 push notifications(名片里的 `pushNotifications` 被忽略);
- 不自动上传文件:`FilePart` 的 URL 会作为文本出现在结果里;
- 不缓存名片(Hikqin 的发布状态是请求级现查,缓存只会立刻过期);
- 不做自动审批:审批决策始终交回给模型/用户。
