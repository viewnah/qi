# 会话

会话 = 一份本地 JSONL 文件(树形:一条主分支 + 分叉)。这里管它的**列表、明细、标题、
删除、导出、压缩、分叉**。通用约定见 [`README.md`](README.md);跑起来见 [`ag-ui.md`](ag-ui.md)。

> 共同的占用规则:**`running=true` 的会话,删 / 压缩 / 分叉一律 `409`**。
> 它们都要动那份正在被追加的 JSONL,拷到半截回合或删掉正在写的文件都是不可逆的。
> 停下来再操作。

## `GET /api/sessions`

列表。**顺序 = 会话文件的 mtime**(最近活跃在最前)。

### 响应 `SessionList` (200)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sessions` | `SessionSummary[]` | 见下 |
| `now_running` | `string[]` | 当前正在运行的会话 id(与 `running` 同源,给"一眼看全"用) |

`SessionSummary`:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `string` | 会话 id = 文件名 |
| `title` | `string` | 标题(可以由第一句话自动起) |
| `created_at` | `string` | 创建时刻 `YYYY-MM-DDTHH:MM:SS`(本地时间) |
| `updated_at` | `string` | **文件 mtime** —— 左栏「15分钟」显示的是这个,**不是**创建时间 |
| `cwd` | `string \| null` | 项目目录。`null` = 老会话(header 里没记 cwd)→ 归「未分组」 |
| `message_count` | `int` | 消息条数 |
| `running` | `bool` | 这条是否在跑 |
| `path` | `string` | 会话文件绝对路径(弹层里显示"改的是哪个文件") |

## `POST /api/sessions`

新建会话。**在某个目录下新建会话 = 新建工作区**(工作区是派生的,没有"新建工作区"端点)。

### 请求体 `SessionCreate`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `title` | `string` | `""` | 空 = 由第一句话自动起 |
| `cwd` | `string \| null` | `null` | 不给 = `default_cwd` |

### 响应 `SessionSummary` (201)

| 码 | 什么时候 |
| --- | --- |
| 400 | `cwd` 给了但不是已存在的目录 |
| 422 | body 不是对象 / 字段类型不对 |

## `GET /api/sessions/{sid}`

明细。**只回一个窗口**,老会话不会因为细节太多而卡住(渐进恢复)。

### 查询参数

| 名 | 类型 | 默认 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `limit` | int | `200` | 1..2000 | 窗口里最多几条 |
| `before` | int | 无 | ≥0 | 窗口**右端**下标(不给 = 到末尾) |

窗口 = `[start, end)`,`end = min(before, len(entries))`,`start = end - limit`。

### 响应 `SessionDetail` (200)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` / `title` / `created_at` / `cwd` / `path` / `running` | 同上 | |
| `total_entries` | `int` | 当前分支的总条数(**只含当前分支** + header,别的分支不算这段对话的历史) |
| `entries` | `dict[]` | 窗口内的原始 entry(JSONL 原样形状,`entries[0]` 是 header) |
| `skipped` | `int` | 窗口**前面**还有几条 → 前端据此继续向更早翻页 |
| `usage` | `UsageSummary` | **整条分支**的用量汇总,与窗口无关(后端算,前端只拿窗口会静默少算) |

`UsageSummary`:`turns` / `steps` / `tools` / `tool_failures` / `llm_calls` /
`prompt_tokens` / `completion_tokens` / `total_tokens` / `context_tokens`。
`context_tokens` = 最后一轮的 prompt 大小(现在上下文里装着多少,不是各轮相加)。
老会话除 `turns` 外全是 0 —— 没有就是没有,不补估值。

| 码 | 什么时候 |
| --- | --- |
| 404 | 会话不存在 |
| 422 | `limit`/`before` 越界 |

## `PATCH /api/sessions/{sid}`

改标题。走 `store.set_title()` —— 内存、header entry、落盘**三处一起改**(与自动命名共用一条路)。

### 请求体 `SessionRename`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `title` | `string` | **至少 1 个字符**(`min_length=1`) |

### 响应 `SessionSummary` (200) · 404 会话不存在 · 422 空标题

## `DELETE /api/sessions/{sid}`

删一条(不可恢复)。

**响应 204**(空 body)。

| 码 | 什么时候 |
| --- | --- |
| 409 | 会话正在运行(先停止) |
| 404 | 会话不存在 |

## `DELETE /api/sessions`

批量删:目前只有一种作用域 —— 左栏「未分组」桶。

「未分组」= **没有 cwd 的旧会话**(qi 早先版本的 header 里没记 cwd,无从判断它属于哪个项目)。
它们既不能被"删除工作区"带走,也只能一条一条删,所以给这一桶一个批量出口。

### 查询参数(必填)

| 名 | 类型 | 允许值 | 说明 |
| --- | --- | --- | --- |
| `scope` | string | `"ungrouped"` | 只有这一种;不给 → 422 |

### 响应 `DeletedSessions` (200)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ids` | `string[]` | **真正被删掉**的 id(前端据此判断"我正开着的那条是不是也没了") |

| 码 | 什么时候 |
| --- | --- |
| 409 | 未分组里**任一条**在运行中 → 整体拒绝(不会只删一半) |

## `GET /api/sessions/{sid}/export`

导出 JSONL(= TUI 的 `/export`)。**原样发文件** —— 会话文件本身就是 JSONL,
再序列化一遍只会引入第二份真相。

### 响应 (200)

- `Content-Type: application/x-ndjson`
- `Content-Disposition: attachment; filename="qi-<sid>.jsonl"; filename*=UTF-8''<百分号编码>`
  (HTTP 头只能 latin-1,标题里有中文会把 `filename=` 写爆 → 所以按 RFC 5987 双写,
  浏览器优先用 `filename*`)
- `Access-Control-Expose-Headers: Content-Disposition`(前端要能读到文件名)

| 码 | 什么时候 |
| --- | --- |
| 404 | 会话不存在 / 文件已被外部删掉 |

## `POST /api/sessions/{sid}/compact`

手动压缩上下文(= TUI 的 `/compact`:把旧消息摘要掉)。

### 响应 `CompactionResult` (200)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `compacted` | `bool` | `false` = **没什么可压**(消息太少/没超阈值),**不是错误** —— 调用方据此提示,而不是报错 |

| 码 | 什么时候 |
| --- | --- |
| 409 | 会话正在运行(压缩要读整条分支 → 摘要 → 追加,与正在追加的那轮会打架) |
| 404 | 会话不存在 |

## `POST /api/sessions/{sid}/fork`

把**当前分支**复制成新会话(与 CLI/TUI 的 fork 同语义):**新文件**,不是同一个文件里开叉。
副标题带 `@fork`、cwd 继承、entry 的 `id`/`parentId` 原样拷过去(链在新文件里自洽)。

### 请求体 `SessionFork`(可省略整个 body)

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `at` | `string \| null` | `null` | 分叉点 entry id。`null` = 当前节点(最后一个完整回合);`""` = 从第一条消息之前 —— **两者含义不同** |

### 响应 `SessionSummary` (201)

| 码 | 什么时候 |
| --- | --- |
| 409 | 会话正在运行(文件正在被追加,拷到的可能是半截回合) |
| 400 | `at` 不是这条会话里的 entry id |
| 404 | 会话不存在 |

---

## 例

```bash
# 列表
curl -s http://127.0.0.1:3456/api/sessions | jq '.sessions[0] | {id,title,cwd,running}'

# 新建(在指定项目里)
curl -s -X POST http://127.0.0.1:3456/api/sessions \
  -H 'Content-Type: application/json' -d '{"cwd":"/Users/you/proj"}' | jq .id

# 明细:最后 50 条
curl -s 'http://127.0.0.1:3456/api/sessions/<sid>?limit=50' | jq '{total_entries, skipped}'

# 改名
curl -s -X PATCH http://127.0.0.1:3456/api/sessions/<sid> \
  -H 'Content-Type: application/json' -d '{"title":"修一下回归"}'

# 导出
curl -OJ 'http://127.0.0.1:3456/api/sessions/<sid>/export'

# 分叉(从某个 entry 之后另起一条)
curl -s -X POST http://127.0.0.1:3456/api/sessions/<sid>/fork \
  -H 'Content-Type: application/json' -d '{"at":"<entry-id>"}'

# 删(运行中 → 409)
curl -i -X DELETE http://127.0.0.1:3456/api/sessions/<sid>

# 清空「未分组」
curl -s -X DELETE 'http://127.0.0.1:3456/api/sessions?scope=ungrouped' | jq .ids
```
