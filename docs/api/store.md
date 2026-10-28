# 商店（`/api/store/*`）

把 **hikqin** 当应用商店。这一组端点**只是转发**到 `qi-store` 扩展的
`qi_store.service`（进程内懒 import，与 `/api/mcp` 消费 `qi_mcp.config` 同一套模式）。

三条规矩，与其它写端点一致：

| 规矩 | 为什么 |
| --- | --- |
| `qi-store` 没装 → `available: false` + `error`，**200 而不是 500** | 少装一个扩展不该让整页报错 |
| 写操作必须带 `X-Qi-Confirm: yes` | 与 `POST /api/mcp`、`POST /api/a2a` 同一条闸 |
| **登录密码只过一次** | `POST /api/store/login` 把密码交给上游就丢；响应只有登录态，令牌不回浏览器 |

能力位 `GET /api/meta` → `capabilities.store_write` 由“真的 import 一次 `qi_store.service`”得出。
没有它 → 设置页把商店节当**只读**（`status` / `search` / `installed` 仍可读），不摆
“登录 / 安装 / 卸载”这些点了才 404 的键。

所有上游 HTTP 调用都在 `asyncio.to_thread` 里跑（`qi_store` 是同步 `httpx.Client`），
不阻塞事件循环。

失败映射：`StoreError.exit_code` → HTTP 状态码，按**语义**而不是数字对齐：

| `exit_code` | HTTP | 含义 |
| --- | --- | --- |
| 2 | 400 | 参数/配置不对、有条目被标高危但没允许 |
| 3 | 401 | 没登录 / 凭证过期 |
| 4 | 404 | 目录或账本里没有这个条目 |
| 5 | 409 | 目标已存在且不是本账本装的（要 `force`） |
| 其它 | 502 | 上游/磁盘问题 |

## GET /api/store/status

源的配置与登录状态。**不发请求**（断网也能画界面）。

```json
{
  "available": true,
  "sources": [{"name": "hikqin", "baseUrl": "http://10.1.43.6:6091",
               "default": true, "loggedIn": true, "login": "admin", "role": "admin",
               "expiresAt": 1792562546, "expiresIn": 9000}],
  "defaultScope": "global",
  "wanted": ["skill:4"],
  "warnings": [],
  "storeFile": "C:/Users/x/.qi/agent/store.json"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `available` | `bool` | `false` = qi-store 没装 / 配置坏了；看 `error` |
| `sources[]` | `object[]` | 每个源：`name` / `baseUrl` / `default` / `loggedIn` / `login` / `role` / `expiresAt` / `expiresIn`；**不含令牌** |
| `defaultScope` | `string` | 默认安装层（`global` \| `project`） |
| `wanted[]` | `string[]` | 项目声明要装但当前没装的条目 id（`check` 里会以 `wantedMissing` 展开） |
| `warnings[]` | `string[]` | 非致命问题（配置里的小毛病） |
| `storeFile` | `string` | 源配置 / 令牌文件路径（`~/.qi/agent/store.json`，0600） |
| `error` | `string` | `available=false` 时的人话原因 |

不带确认头（只读）。

## POST /api/store/login

需要 `X-Qi-Confirm: yes`(它会往磁盘写凭证)。

```json
{"login": "admin", "password": "…", "baseUrl": "http://10.1.43.6:6091", "store": "hikqin"}
```

| 字段 | 类型 | 必给 | 说明 |
| --- | --- | --- | --- |
| `login` | `string` | ✓ | 终端账号（≥1 字符） |
| `password` | `string` | ✓ | **只在这个请求里用一次**，不落盘、不进日志 |
| `baseUrl` | `string` | 第一次 | 还没有源时必须给；之后可省，沿用已配置地址 |
| `store` | `string` | ✗ | 多源时选一个；省略 = `defaultStore` |

- 第一次登录必须给 `baseUrl`(还没有源);之后可省,沿用已配置地址;
- 上游 401(密码错)→ **401**,且**一个文件都不写**;
- 没给地址 → 400;`baseUrl` 不像 http(s) → 400;
- 响应 = `StoreStatus`(登录态),**不包含 access / refresh 令牌**;
- 密码不落盘、不进日志:磁盘上只有上游返回的令牌(用户级 `store.json`,0600)。

## POST /api/store/logout

需要 `X-Qi-Confirm: yes`。`{"store": "", "all": false}` —— 删本地凭证(幂等)。
上游**没有吊销接口**,所以它只是删本地:要立即失效请改密码。

## GET /api/store/search

查询参数：

| 名 | 类型 | 默认 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `q` | `string` | 无 | — | 匹配名称或描述 |
| `kind` | `string` | 无 | `skill` / `mcp` / `a2a` / `agent` / `kb` | 只查这一类 |
| `limit` | `int` | `0` | 0..200 | `0` = 不限 |
| `offline` | `bool` | `false` | — | 读目录缓存，不发上游请求 |

`items[]` 里带**已装状态**，界面据此画"安装 / 已装 / 可升级"：

```json
{"id": "skill:4", "kind": "skill", "name": "ppt-generator", "version": "sig:e369b1467a52",
 "requiresConfig": false, "safety": {"scan_status": "safe", "reviewed": false},
 "installed": false, "installedVersion": null, "updateAvailable": false}
```

`items[]` 字段：`id` / `kind` / `name` / `description` / `version` / `updatedAt` /
`requiresConfig` / `safety` / `badges` / `installed` / `installedVersion` / `updateAvailable`。

顶层字段：`available` / `store` / `items[]` / `total` / `missing[]` / `cachedAt`(目录缓存时刻) / `error`。
`missing[]` 每条 `{kind, reason}`。

`missing[]` 说明**哪一类没查成**（未登录 / 网络）——界面必须显示，否则"商店是空的"
是最坏的误解（`skill` / `mcp` / `agent` 需要登录，`a2a` 公开）。

五类 kind：`skill` / `mcp` / `a2a` / `agent` / `kb`。

`kb` 的 `installed` 状态以 qi-kb 的 `kb.json` 为准（不是商店账本）；安装时账号会复制到
qi-kb 自己的 `kb.json` / `kb-auth.json`，`store.json` 不变。

- `agent` 的目录是 **`GET /api/agents`**（你账号能看到的全部智能体，草稿与已发布都列）；
  安装时先取详情（`/api/agents/{id}`），再按需把它的 MCP（`/api/mcp/servers/{id}`）与
  技能（`/api/skills/{id}/download`）逐个取成原始形状，自己拼成 qi-agents 的角色目录。
  规则在 qi-store 的 `README.md` §4.5，这里只记接口形状。

## GET /api/store/installed

读本地账本（`~/.qi/agent/store/installed.json`），**离线可用**。

```json
{"available": true, "items": [{"id": "mcp:3", "kind": "mcp", "name": "ppt-tools",
  "version": "sig:…", "scope": "global", "target": "…/mcp.json",
  "needsConfig": ["PPT_TOOLS_TOKEN"], "installedAt": "2026-10-01T12:00:00",
  "upstreamUpdatedAt": "…", "present": true, "missingReason": ""}]}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` / `kind` / `name` / `version` | `string` | 账本条目 |
| `scope` / `target` | `string` | 装在哪一层、写到了哪个文件 |
| `needsConfig` | `string[]` | 还要 `export` 的环境变量 |
| `installedAt` / `upstreamUpdatedAt` | `string` | 安装时刻 / 上游版本时刻 |
| `present` | `bool` | **磁盘上还在不在**：账本只记“装过什么”；手改 json / `rm -rf` 都不经过商店 |
| `missingReason` | `string` | `present=false` 时的人话原因（界面直接显示） |

顶层字段：`available`（qi-store 是否可用）/ `items[]` / `error`。

## POST /api/store/plan

**只算不写**：装配前必须先看这份计划（写哪里 / 会不会撞 / 要补什么）。
**不需要**确认头（不落盘）。

```json
{"ids": ["skill:4"], "scope": "global", "force": false}
```

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `ids` | `string[]` | — | 条目 id 或名称；**空 → 400** |
| `scope` | `"global" \| "project" \| null` | `null` | 省略 = 用 `status.defaultScope` |
| `force` | `bool` | `false` | 允许覆盖冲突目标 |

返回 `items[].targets`（将写入的路径）、`needsConfig`、`conflict`、`unsafe`、`blocked`
（有冲突且没 `force` → 界面要先让用户选"覆盖"再重发）。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `available` / `scope` / `error` | `bool` / `string` / `string` | 服务是否可用 / 计划写到哪层 / 失败原因 |
| `items[]` | `object[]` | 每条：`id` / `name` / `note` / `targets[]` / `needsConfig[]` / `conflict` / `unsafe` |
| `skipped[]` | `object[]` | 不需要动的条目（已装且版本相同等） |
| `needsConfig[]` | `string[]` | 整批要补的环境变量 |
| `blocked` | `bool` | 有冲突且没 `force` → 界面必须先问“覆盖” |

## POST /api/store/install

需要 `X-Qi-Confirm: yes`。body 同 `plan`，另加 `allowUnsafe`（`critical` / `high`
的条目必须显式允许，否则 400）。

返回 `installed[]`（同上 `StoreInstalledItem`，含 `target` 与 `needsConfig`）、
`skipped[]`（已装且版本相同）、`needsConfig[]`、`scope`。
`needsConfig` 里的环境变量要在 shell 里 `export` —— qi-mcp 连接时读，缺了会明确报错。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `available` / `scope` / `error` | `bool` / `string` / `string` | 服务是否可用 / 实际写到哪层 / 失败原因 |
| `installed[]` | `StoreInstalledItem[]` | 真正落盘的条目（字段见 `installed` 端点） |
| `skipped[]` | `object[]` | 没动的条目及原因 |
| `needsConfig[]` | `string[]` | 装完还要补的环境变量 |

装了 **skill** 时，端点会顺带调 `WebState.refresh_skills()`：技能落盘只解决一半，
`/api/skills` 与提示词读的都是 `runtime.top_skills`（runtime 构造时的快照）。不重扫的症状是
**"商店里装好了、技能列表里没有、模型也用不上"** —— 与 `POST /api/skills` 同一条处置。

> MCP / A2A 没有等价物（也没有必要）：那两节的读端点是**每次请求重新读文件**。
> 但模型侧的 MCP server 清单在会话开始时取，装完要新会话才用得上（qi-mcp 的设计）。

## POST /api/store/uninstall

需要 `X-Qi-Confirm: yes`。

```json
{"id": "skill:4", "scope": "global"}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `string` | 条目 id；**空 → 400** |
| `scope` | `"global" \| "project" \| null` | 省略 = 账本里记的那一层 |

响应：`{available, id, kind, removed[], error}` —— `kind` 让宿主知道要不要重扫技能缓存。
只删账本登记过的文件；`mcp.json` / `a2a.json` 只删**那个键**（别人配的 server 不受影响）。

## GET /api/store/check

账本 ↔ 上游对比。`offline` 查询参数（默认 `false`）：`true` 时读目录缓存（TTL 5 分钟）。

`status` ∈ `up-to-date` / `update` / `gone`（上游没了）/ `missing`（本地没了，`detail` 说哪一步）。
`wantedMissing[]` 是"项目声明了但没装"的条目（`store.json` 的 `wanted[]`）。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `available` / `error` | `bool` / `string` | 服务是否可用 / 失败原因 |
| `items[]` | `object[]` | 每条：`id` / `name` / `scope` / `version` / `upstreamVersion` / `status` / `detail` |
| `wantedMissing[]` | `object[]` | 声明了但没装的条目 |

> 装完 MCP / A2A 条目后,**设置面对应那一节立刻能看到**(进入该节会重读 `mcp.json` /
> `a2a.json`;这两节的读端点本来就没有缓存)。但**模型侧**要新会话:
> qi-mcp 的 server 清单在会话开始时取,qi-a2a 的远端角色也是 `session_start` 时注册的。
