# 知识库（`/api/kb/*`）

把 **hikqin 知识库**接进 qi-lab。宿主进程内懒 import `qi_kb.service`；qi-kb 没装时
返回 `available: false` + `error`，不报 500。

三条规矩与商店一致：

| 规矩 | 为什么 |
| --- | --- |
| 网络调用走 `asyncio.to_thread` | `qi_kb` 是同步 `httpx.Client`，不阻塞事件循环 |
| 写操作要 `X-Qi-Confirm: yes` | provider / KB / 默认参数都会落盘 |
| 令牌不回显 | 响应只有登录态；密码只在一次请求里用 |

能力位 `GET /api/meta` → `capabilities.kb_write` 由“真的 import 一次 `qi_kb.service`”得出。
没有它 → 设置页把知识库节当**只读**（检索 / 问答 / 文档列表仍可用），不摆
provider / KB / 默认参数的写操作。

## GET /api/kb/status

配置与 provider 状态（**不发请求**，断网也能画界面）。

```json
{
  "available": true,
  "providers": [
    {"name": "alice", "baseUrl": "http://10.1.43.6:7091", "apiPrefix": "/api",
     "instance": "http://10.1.43.6:7091/api", "login": "alice", "role": "user",
     "loggedIn": true, "expiresAt": 1792658971, "expiresIn": 9000,
     "kbCount": 1, "scope": "global"}
  ],
  "knowledgeBases": [{"id": "54aa…", "provider": "alice", "name": "…", "bound": true}],
  "defaults": {"topK": 5, "retrievalStrategy": "hybrid"},
  "defaultProvider": "alice",
  "defaultKnowledgeBase": "",
  "files": ["…/kb.json"],
  "warnings": [],
  "error": ""
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `available` | `bool` | `false` = qi-kb 没装 / 配置坏了；看 `error` |
| `providers[]` | `KbProvider[]` | 见下；**不含 access / refresh 令牌** |
| `knowledgeBases[]` | `object[]` | 已绑定 KB 的摘要（流式面板首屏用） |
| `defaults` | `object` | 默认检索参数（`topK` / `retrievalStrategy` / …） |
| `defaultProvider` / `defaultKnowledgeBase` | `string` | 全局默认；空 = 没设 |
| `files[]` | `string[]` | 实际读了哪些配置文件（诊断） |
| `warnings[]` / `error` | `string[]` / `string` | 非致命警告 / `available=false` 的原因 |

`KbProvider`：`name` / `baseUrl` / `apiPrefix` / `instance` / `login` / `role` /
`loggedIn` / `expiresAt` / `expiresIn` / `kbCount` / `scope`。

不带确认头（只读）。

## GET /api/kb/providers

只读 provider 列表（不含 access / refresh）。

```json
{"providers": [{"name": "alice", "loggedIn": true, "kbCount": 1}], "warnings": [], "error": ""}
```

`providers[]` 字段同 `KbProvider`。`error` 非空 = 读配置失败（不是 500）。

## POST /api/kb/providers

需要 `X-Qi-Confirm: yes`。新建/更新 provider（不登录）。

```json
{"name": "alice", "baseUrl": "http://10.1.43.6:7091",
 "apiPrefix": "/api", "login": "alice", "role": "user",
 "instance": "hikqin-7091", "scope": "global"}
```

| 字段 | 类型 | 必给 | 说明 |
| --- | --- | --- | --- |
| `name` | `string` | ✓ | provider 名（≥1 字符） |
| `baseUrl` | `string` | ✓ | 上游地址 |
| `apiPrefix` | `string` | ✗ | 默认 `/api` |
| `login` / `role` | `string` | ✗ | 登录名 / 角色（只声明，不写凭证） |
| `instance` | `string` | ✗ | 同一上游多实例时区分；空 = 按地址推断 |
| `scope` | `"global" \| "project"` | ✗ | 默认 `global` |

响应 = `KbStatus`（刷新后的全量状态）。

## POST /api/kb/providers/login

需要 `X-Qi-Confirm: yes`。密码只在这次调用里用，响应不回显。

```json
{"name": "alice", "login": "alice", "password": "…",
 "baseUrl": "", "apiPrefix": "/api", "instance": "", "scope": "global"}
```

| 字段 | 类型 | 必给 | 说明 |
| --- | --- | --- | --- |
| `name` | `string` | ✓ | 已声明的 provider 名 |
| `password` | `string` | ✓ | 只过一次：交给上游后丢弃 |
| `login` | `string` | ✗ | 省略 = 用 `name` |
| `baseUrl` / `apiPrefix` / `instance` | `string` | ✗ | 首次登录（还没声明）时可一并给出 |
| `scope` | `"global" \| "project"` | ✗ | 默认 `global` |

响应 = `KbStatus`；磁盘上只保存上游返回的令牌（`kb-auth.json`，0600）。

## DELETE /api/kb/providers

需要 `X-Qi-Confirm: yes`。provider 下还有 KB 时 **409**；`cascade=true` 才连 KB 一起删。

```json
{"name": "alice", "cascade": false}
```

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `name` | `string` | — | provider 名（≥1 字符）；不存在 → 404 |
| `cascade` | `bool` | `false` | `true` = 连同它名下的 KB 绑定一起删 |

## POST /api/kb/plan

**只算不写**。安装前先看：`install` / `update` / `rebind`，以及是否 `blocked`。
**不需要**确认头（不落盘）。

```json
{"kbId": "54aa…", "provider": "bob", "scope": "global", "rebind": false}
```

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `kbId` | `string` | — | KB id（≥1 字符；空 → 422） |
| `provider` | `string` | — | 要绑到哪个 provider（≥1 字符） |
| `scope` | `"global" \| "project"` | `global` | 写到哪一层 |
| `rebind` | `bool` | `false` | KB 已绑在别的 provider 上时是否抢过来 |

```json
{"available": true, "action": "rebind", "kbId": "54aa…",
 "provider": "bob", "existingProvider": "alice",
 "targetProvider": "bob", "blocked": true}
```

`action` ∈ `install` / `update` / `rebind`；`blocked=true` = 有冲突且 `rebind=false`，
界面要先让用户选“保留 / 覆盖”。

## GET /api/kb/available

查询参数：`provider`、`q`、`boundOnly`。省略 `provider` 时聚合所有 provider；
`boundOnly=true` 只看已绑定的；`q` 按名称 / 描述过滤。

```json
{"available": true, "items": [
  {"id": "54aa…", "provider": "alice", "name": "智能体平台相关案例材料",
   "description": "…", "embeddingModel": "bge-m3", "visibility": "private",
   "docCount": 2, "chunkCount": 6, "updatedAt": "2026-10-01T12:00:00",
   "icon": "", "bound": true, "boundProvider": "alice", "scope": "global"}
], "total": 1, "warnings": [], "error": ""}
```

`KbItem`：`id` / `provider` / `name` / `description` / `embeddingModel` / `visibility` /
`docCount` / `chunkCount` / `updatedAt` / `icon` / `bound` / `boundProvider` / `scope`。
`bound=false` 时 `boundProvider=null`。

## GET /api/kb/docs

查询参数：

| 名 | 类型 | 必给 | 说明 |
| --- | --- | --- | --- |
| `kbId` | `string` | ✓ | KB id（空 → 422） |
| `provider` | `string` | ✗ | 省略 = 按 `kbId` 找 / 用默认 provider |

```json
{"available": true, "provider": "alice", "kbId": "54aa…", "name": "…",
 "documents": [{"filename": "a.pdf", "chunk_count": 5, "status": "completed"}],
 "total": 1, "warnings": [], "error": ""}
```

## POST /api/kb/search

只检索，不调用上游 LLM。不需要确认头（只读）。

```json
{"query": "情报分析", "kbIds": ["54aa…"], "provider": "alice",
 "topK": 3, "strategy": "hybrid", "useRerank": false, "rerankTopK": 3}
```

`KbSearchRequest`（`/search` 与 `/ask` 共用同一套检索参数）：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `query` / `question` | `string` | — | ≥1 字符（空 → 422） |
| `kbIds` | `string[]` | `[]` | 空 = 用全局 / 会话绑定 |
| `provider` | `string` | `""` | 空 = 按 KB 自动选；多 provider 命中时会报错 |
| `topK` | `int \| null` | `null` | 省略 = 默认参数 |
| `strategy` | `"dense" \| "sparse" \| "hybrid" \| null` | `null` | 省略 = 默认参数 |
| `useRerank` / `rerankTopK` | `bool \| null` / `int \| null` | `null` | 省略 = 默认参数 |

```json
{"available": true, "provider": "alice", "query": "情报分析",
 "results": [{"content": "…", "score": 0.31, "provider": "alice",
              "metadata": {"filename": "a.pdf", "chunk_index": 3}}],
 "total": 1, "strategy": "hybrid", "rerankMethod": null,
 "kbIds": ["54aa…"], "warnings": []}
```

## POST /api/kb/ask

上游 RAG LLM 作答；会在上游写一轮 QA 历史。不需要确认头。

```json
{"question": "这份材料讲什么", "kbIds": ["54aa…"], "provider": "alice",
 "topK": 3, "strategy": "hybrid", "useRerank": true}
```

请求字段同 `/api/kb/search`（只把 `query` 换成 `question`）。

```json
{"available": true, "provider": "alice", "question": "这份材料讲什么",
 "answer": "…", "sources": [{"doc_name": "a.pdf", "score": 0.0}],
 "kbIdsUsed": ["54aa…"], "evidenceSufficient": true, "warnings": []}
```

`evidenceSufficient=false` 是**结果**不是错误：上游认为检索到的证据不足，界面应显示提示。

## POST /api/kb/ask/stream

流式问答（SSE）：逐 token 推，最后 `sources` / `done`。请求体同 `/api/kb/ask`。

响应头：`Content-Type: text/event-stream`、`Cache-Control: no-cache, no-transform`、
`X-Accel-Buffering: no`。每帧只有 `data:` 行（与 `POST /api/ag-ui` 同一套组帧）。

```text
data: {"type":"token","content":"这份","provider":"alice"}
data: {"type":"token","content":"材料","provider":"alice"}
data: {"type":"sources","content":[{"doc_name":"a.pdf"}],"provider":"alice"}
data: {"type":"done","provider":"alice","kbIds":["54aa…"]}
```

| `type` | 含义 |
| --- | --- |
| `token` | 一段正文增量（`content`） |
| `sources` | 引用的文档（`content[]`） |
| `done` | 结束；带上本轮用了哪些 `kbIds` |
| `error` | 出错（`message`）—— **不断连**，帧内报错后结束 |

qi-kb 没装时不 404：流里先发一帧 `{"type":"error","message":"…"}` 再结束。
一次只允许一个 provider；多个 provider 命中时返回 `error` 事件（400 级语义），
用 `provider` 或 `名称@provider` 收窄。

## GET /api/kb/files/{provider}/{kb_id}/{file_id}

**稳定的源文件下载/预览链接**。检索与问答的 `sources[].url` 就是它。
浏览器点击即可访问，不需要 `Authorization`；链接不带签名、不过期，因此可以直接落进历史消息。

```text
/api/kb/files/alice/54aa…/e4e2…
```

- Host 头不被允许 → **400**
- provider / qi-kb 不可用 → **503**

> 这是明确的取舍：链接不签名、不过期，能访问 qi-lab 该路径的人就能下载。
> 建议 qi-lab 只绑回环/内网；需要更强边界时用容器/反代鉴权。
- 返回源文件的 `Content-Type`，`Content-Disposition: inline`（PDF 等浏览器直接预览）

检索 / 问答的 source 会带：

```json
{
  "index": 1,
  "doc_name": "a.pdf",
  "filename": "a.pdf",
  "provider": "alice",
  "kb_id": "54aa…",
  "doc_id": "…",
  "file_id": "e4e2…",
  "file_ref": "kb://alice/54aa…/e4e2…",
  "url": "/api/kb/files/alice/54aa…/e4e2…"
}
```

## POST /api/kb/install

需要 `X-Qi-Confirm: yes`。同一个 `(instance, kbId)` 已由别的 provider 安装且
`rebind=false` → **409**；前端据此弹“保留 / 覆盖为当前 provider”。

```json
{"kbId": "54aa…", "provider": "bob", "scope": "global", "rebind": true}
```

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `kbId` / `provider` | `string` | — | 都必给（≥1 字符） |
| `scope` | `"global" \| "project"` | `global` | 绑定写到哪一层 |
| `rebind` | `bool` | `false` | 已绑在别的 provider 上时是否抢过来 |

```json
{"available": true, "action": "rebind", "kbId": "54aa…",
 "provider": "bob", "scope": "global", "name": "…"}
```

## POST /api/kb/uninstall

需要 `X-Qi-Confirm: yes`。只删本地绑定，不动上游数据；provider 保留。

```json
{"kbId": "54aa…", "provider": ""}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `kbId` | `string` | KB id（≥1 字符） |
| `provider` | `string` | 可选；不给 = 按绑定记录找 |

响应：`{available, kbId, provider, name, removed, error}` —— `removed=false` 表示本来就没绑。

## PATCH /api/kb/defaults

需要 `X-Qi-Confirm: yes`。只允许白名单键（`topK` / `retrievalStrategy` /
`hybridAlpha` / `useRerank` / `rerankTopK` / `rerankScoreThreshold` / `maxChars` /
`evidenceScoreThreshold` / `reuseSession`）。

```json
{"defaults": {"topK": 5, "retrievalStrategy": "hybrid"}, "scope": "global"}
```

`defaults` 只写白名单键（其它键忽略 / 由 qi-kb 决定）；`scope` ∈ `global` / `project`。
响应 = `KbStatus`。

## 状态码

| 码 | 什么时候 |
| --- | --- |
| 400 | 参数 / 配置不对：缺 `kbId` / `provider`、地址或参数不合法、上游认为请求非法 |
| 401 | provider 没登录 / 令牌过期（上游 401 映射过来） |
| 404 | provider / KB / 文档不存在 |
| 409 | provider 下还有 KB（`cascade=false`）；同一个 `(instance, kbId)` 已绑在别的 provider 且 `rebind=false` |
| 422 | body 不符合 schema（pydantic，例如 `kbId` 为空、`strategy` 不在闭集） |
| 428 | 写操作缺 `X-Qi-Confirm: yes` |
| 502 | 上游 / 磁盘问题（`KbError.exit_code` 的非 400/401/404/409 分支） |

qi-kb 没装时读端点回 `available=false` + `error`（HTTP 200），流式问答回一帧 `error`；
写端点同样回 `available=false`，不会 500。
