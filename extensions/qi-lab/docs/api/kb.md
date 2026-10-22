# 知识库（`/api/kb/*`）

把 **hikqin 知识库**接进 qi-lab。宿主进程内懒 import `qi_kb.service`；qi-kb 没装时
返回 `available: false` + `error`，不报 500。

三条规矩与商店一致：

| 规矩 | 为什么 |
| --- | --- |
| 网络调用走 `asyncio.to_thread` | `qi_kb` 是同步 `httpx.Client`，不阻塞事件循环 |
| 写操作要 `X-Qi-Confirm: yes` | provider / KB / 默认参数都会落盘 |
| 令牌不回显 | 响应只有登录态；密码只在一次请求里用 |

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

## GET /api/kb/providers

只读 provider 列表（不含 access / refresh）。

```json
{"providers": [{"name": "alice", "loggedIn": true, "kbCount": 1}], "warnings": []}
```

## POST /api/kb/providers

需要 `X-Qi-Confirm: yes`。新建/更新 provider（不登录）。

```json
{"name": "alice", "baseUrl": "http://10.1.43.6:7091",
 "apiPrefix": "/api", "login": "alice", "role": "user",
 "instance": "hikqin-7091", "scope": "global"}
```

响应 = `KbStatus`。

## POST /api/kb/providers/login

需要 `X-Qi-Confirm: yes`。密码只在这次调用里用，响应不回显。

```json
{"name": "alice", "login": "alice", "password": "…",
 "baseUrl": "", "apiPrefix": "/api", "scope": "global"}
```

## DELETE /api/kb/providers

需要 `X-Qi-Confirm: yes`。provider 下还有 KB 时 **409**；`cascade=true` 才连 KB 一起删。

```json
{"name": "alice", "cascade": false}
```

## POST /api/kb/plan

**只算不写**。安装前先看：`install` / `update` / `rebind`，以及是否 `blocked`。

```json
{"kbId": "54aa…", "provider": "bob", "scope": "global", "rebind": false}
```

```json
{"available": true, "action": "rebind", "kbId": "54aa…",
 "provider": "bob", "existingProvider": "alice",
 "targetProvider": "bob", "blocked": true}
```

## GET /api/kb/available

查询参数：`provider`、`q`、`boundOnly`。省略 `provider` 时聚合所有 provider。

```json
{"available": true, "items": [
  {"id": "54aa…", "provider": "alice", "name": "智能体平台相关案例材料",
   "docCount": 2, "chunkCount": 6, "visibility": "private",
   "bound": true, "boundProvider": "alice"}
], "total": 1, "warnings": [], "error": ""}
```

## GET /api/kb/docs

查询参数：`kbId`（必填）、`provider`（可选）。

```json
{"available": true, "provider": "alice", "kbId": "54aa…", "name": "…",
 "documents": [{"filename": "a.pdf", "chunk_count": 5, "status": "completed"}],
 "total": 1, "warnings": [], "error": ""}
```

## POST /api/kb/search

只检索，不调用上游 LLM。

```json
{"query": "情报分析", "kbIds": ["54aa…"], "provider": "alice",
 "topK": 3, "strategy": "hybrid", "useRerank": false, "rerankTopK": 3}
```

```json
{"available": true, "provider": "alice", "query": "情报分析",
 "results": [{"content": "…", "score": 0.31, "provider": "alice",
              "metadata": {"filename": "a.pdf", "chunk_index": 3}}],
 "total": 1, "strategy": "hybrid", "rerankMethod": null,
 "kbIds": ["54aa…"], "warnings": []}
```

## POST /api/kb/ask

上游 RAG LLM 作答；会在上游写一轮 QA 历史。

```json
{"question": "这份材料讲什么", "kbIds": ["54aa…"], "provider": "alice",
 "topK": 3, "strategy": "hybrid", "useRerank": true}
```

```json
{"available": true, "provider": "alice", "question": "这份材料讲什么",
 "answer": "…", "sources": [{"doc_name": "a.pdf", "score": 0.0}],
 "kbIdsUsed": ["54aa…"], "evidenceSufficient": true, "warnings": []}
```

## POST /api/kb/ask/stream

流式问答（SSE）：逐 token 推，最后 `sources` / `done`。请求体同 `/api/kb/ask`。

```text
data: {"type":"token","content":"这份","provider":"alice"}
data: {"type":"token","content":"材料","provider":"alice"}
data: {"type":"sources","content":[{"doc_name":"a.pdf"}],"provider":"alice"}
data: {"type":"done","provider":"alice","kbIds":["54aa…"]}
```

一次只允许一个 provider；多个 provider 命中时返回 `error` 事件（400 级语义），
用 `provider` 或 `名称@provider` 收窄。

## POST /api/kb/install

需要 `X-Qi-Confirm: yes`。同一个 `(instance, kbId)` 已由别的 provider 安装且
`rebind=false` → **409**；前端据此弹“保留 / 覆盖为当前 provider”。

```json
{"kbId": "54aa…", "provider": "bob", "scope": "global", "rebind": true}
```

```json
{"available": true, "action": "rebind", "kbId": "54aa…",
 "provider": "bob", "scope": "global", "name": "…"}
```

## POST /api/kb/uninstall

需要 `X-Qi-Confirm: yes`。只删本地绑定，不动上游数据；provider 保留。

```json
{"kbId": "54aa…"}
```

## PATCH /api/kb/defaults

需要 `X-Qi-Confirm: yes`。只允许白名单键（`topK` / `retrievalStrategy` /
`hybridAlpha` / `useRerank` / `rerankTopK` / `rerankScoreThreshold` / `maxChars` /
`evidenceScoreThreshold` / `reuseSession`）。

```json
{"defaults": {"topK": 5, "retrievalStrategy": "hybrid"}, "scope": "global"}
```

响应 = `KbStatus`。
