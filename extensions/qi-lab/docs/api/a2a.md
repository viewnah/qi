# A2A 条目:两层 `a2a.json`

读端点(逐层原样 + 合并后的生效视图)+ 四个写端点(增删改 + 探测)。通用约定见
[`README.md`](README.md) —— 三个写操作**都要 `X-Qi-Confirm: yes`**,缺则 428。

> 声明文件与协议由 [qi-a2a](../..) 扩展定义;**本节只讲 qi-lab 怎么读它、怎么写它**。
> 没装 qi-a2a 时读端回 `unavailable=true`、写端 **400**(不是 500)。

## 只有两层

| layer | 位置 | 说明 |
| --- | --- | --- |
| `global` | `~/.qi/agent/a2a.json` | 用户级基建,跨项目 |
| `project` | `<cwd>/.qi/a2a.json` | 跟着仓库走,可提交共享 |

与 MCP **不同**:没有角色私有那层,而且合并规则是**同名深合并** —— 嵌套对象递归合并、
数组整体替换、`null` 删键。

所以读端回两份东西:

- `sources[]` —— 每层**原样**写了什么(界面显示“这条是全局写的”);
- `resolved[]` —— 深合并之后的**生效**条目,带 `layers` 说明哪些层参与过。

## `GET /api/a2a`

### 响应 `A2AList` (200)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sources` | `A2ASource[]` | 每层一项(global → project) |
| `resolved` | `A2AEntryInfo[]` | 合并后的生效视图;**含被 `disabled` 关掉的** |
| `unavailable` | `bool` | qi-a2a 没装 |

`A2ASource`:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `scope` | `string` | `global` \| `project` |
| `path` | `string` | 这一层的文件绝对路径 |
| `exists` | `bool` | 区分“没这个文件”与“文件在但没写条目” |
| `entries` | `A2AEntryInfo[]` | |

`A2AEntryInfo`:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | `string` | 条目名 |
| `card_url` | `string` | Agent Card 地址 |
| `base_url` | `string` | 网关基址 |
| `rpc_url` | `string` | 完整覆盖的 JSON-RPC 地址 |
| `tenant` | `string` | 固定到某个智能体(如 Hikqin 的 `agents.id`) |
| `description` | `string` | 说明 |
| `header_keys` | `string[]` | `headers` 的键名(**值不给**),已排序 |
| `timeout_ms` | `int` | 缺省 600000 |
| `streaming` | `bool` | 缺省 `true` |
| `discovery` | `string` | `auto` \| `single` \| `list` |
| `direct_tools` | `bool \| string[]` | `true` = 全直连;数组 = 只这几个 |
| `tool_name` | `string` | 直连工具名(`<prefix>__<条目>`);没开 `directTools` 时为空 |
| `disabled` | `bool` | 只认真正的 JSON `true` |
| `unknown_fields` | `string[]` | qi-a2a 不认识的字段(只提示,不拦) |
| `source` | `string` | 这条声明来自哪一层;`resolved` 里是**主导层** |
| `layers` | `string[]` | 参与的层(深合并的痕迹);只有 `resolved` 里有 |

**值一律不出宿主**:`headers` 只给键名。读端也**不展开 `${ENV}`** —— 展开等于把环境变量
的值吐进 HTTP 响应。

```bash
curl -s http://127.0.0.1:3456/api/a2a | jq '.resolved[] | {name, source, layers}'
```

## `POST /api/a2a`

在某一层新增一个条目(文件不存在就建)。**要 `X-Qi-Confirm: yes`**。

### 请求 `A2AWrite`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `scope` | `string` | `global` \| `project` |
| `entry` | `A2AEntryEdit` | 新条目 |

`A2AEntryEdit`(全部可选,除 `name`):

| 字段 | 类型 | 落盘键 |
| --- | --- | --- |
| `name` | `string` | 条目名(JSON 的 key) |
| `card_url` | `string` | `cardUrl` |
| `base_url` | `string` | `baseUrl` |
| `rpc_url` | `string` | `rpcUrl` |
| `tenant` | `string` | `tenant` |
| `description` | `string` | `description` |
| `headers` | `object` | `headers`(**只进不回**) |
| `timeout_ms` | `int \| null` | `timeoutMs` |
| `streaming` | `bool \| null` | `streaming` |
| `discovery` | `string` | `discovery` |
| `direct_tools` | `bool \| string[]` | `directTools` |
| `tool_prefix` | `string` | `toolPrefix` |
| `disabled` | `bool` | `disabled` |

空串 / 空表 = **不写这个键**(默认值由 qi-a2a 补,这里不抄一份)。

### 响应 (201)

更新后的 `A2AList`。

### 错误

| 码 | 条件 |
| --- | --- |
| `400` | 名字为空 / 带首尾空白;声明不完整(合并后仍没有端点);URL 不是 http(s);`${ENV}` 变量未设;文件写坏 |
| `409` | **该层**已有同名条目(跨层同名是合法的,那正是深合并) |
| `428` | 缺 `X-Qi-Confirm: yes` |

```bash
curl -s -X POST http://127.0.0.1:3456/api/a2a \
  -H 'X-Qi-Confirm: yes' -H 'content-type: application/json' \
  -d '{"scope":"project","entry":{"name":"hikqin","base_url":"http://127.0.0.1:8080"}}'
```

## `PATCH /api/a2a`

改一个条目,**只动送来的键**。

### 请求 `A2APatch`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `scope` | `string` | `global` \| `project` |
| `name` | `string` | **改之前**的名字 |
| `patch` | `A2AEntryPatch` | 要改的键 |

`A2AEntryPatch` 的键与 `A2AEntryEdit` 同名,全部可空。两条语义:

- **没送的键 = 不改**;
- **送 `null` 也 = 不改** —— 读端不给 `headers` 的值,前端就不可能把整张表重发回来,
  把 `null` 当“删掉”会让人一保存就丢 token。

要清掉某张表,送**空对象**:`{"headers": {}}`。要改某个标量,送新值;送空串等于删掉那个键。

`patch.name` 给了就是**改名**,且保持这一层里的**原顺序**。

### 响应 (200)

更新后的 `A2AList`。

### 错误

与 `POST` 同;未知条目 `404`。

## `DELETE /api/a2a`

从某一层删掉一个条目。**只删这一层的键**:同名的下层声明会**露出来**(深合并关系没变)。

### 请求 `A2ADelete`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `scope` | `string` | `global` \| `project` |
| `name` | `string` | 条目名 |

### 响应 (200)

更新后的 `A2AList`。未知条目 `404`。

## `POST /api/a2a/probe`

去真实端点拉一次名片,回答“我配的对不对”。

**只探测已配置的条目**(不收任意 URL)—— 否则这个端点就成了内网探测器。
走的是 qi-a2a 工具的同一条代码路径,所以这里能看到什么,模型调用时就能看到什么。
**不需要** `X-Qi-Confirm`(只读)。

### 请求 `A2AProbeRequest`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `server` | `string` | 条目名;只配了一个时可省略 |
| `agent` | `string` | 条目下有多个智能体时,按 `tenant` 或名字选一个 |

### 响应 `A2AProbeResult` (200)

**失败也是 200**:`ok=false` + `error` —— 探测失败是**结果**,不是宿主异常,
界面要显示那句话而不是一个 500。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ok` | `bool` | 拿没拿到名片 |
| `server` | `string` | 探测的条目名 |
| `source` | `string` | 生效条目来自哪一层 |
| `discovery_url` | `string` | 实际 GET 的地址;空 = 没走发现(用配置合成卡) |
| `elapsed_ms` | `int` | 耗时 |
| `cards` | `A2AProbeCard[]` | 拿到的名片 |
| `error` | `string` | 失败原因(带条目名) |

`A2AProbeCard`:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | `string` | |
| `description` | `string` | |
| `tenant` | `string` | |
| `rpc_url` | `string` | JSON-RPC 端点 |
| `skills` | `A2ASkill[]` | 技能(只回展示要的四个字段) |

```bash
curl -s -X POST http://127.0.0.1:3456/api/a2a/probe \
  -H 'content-type: application/json' -d '{"server":"hikqin"}' | jq
```

### 探测会失败在哪些地方

| 情形 | `error` 里写什么 |
| --- | --- |
| 条目为空(或全被 `disabled`) | 提示该怎么加一条 |
| 配了多个条目却没指定 `server` | 列出可选名 |
| `a2a.json` 写坏 | 带文件路径与解析位置 |
| 端点不可达 / 非 JSON / HTTP 4xx-5xx | 带条目名与状态码 |
| 名片里没有 `supportedInterfaces[].url` | 说清缺什么、可以补 `rpcUrl` |

## 能力位

`GET /api/meta` 的 `capabilities.a2a_write`。老宿主没有它 → 界面把 A2A 那一节当只读
(读端点一直在),不摆一个点了才 404 的键。
