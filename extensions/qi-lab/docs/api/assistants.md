# 应用助手目录

项目级 `assistants.json` 的增删改查。**推荐不在这里** —— 推荐由角色 `训练评估`
在对话里调用 `assistant_search` / `assistant_recommend` 完成（见
`extensions/qi-assistants/README.md`）。

三句话定义边界：

1. **唯一事实源**：`<cwd>/.qi/assistants.json`。没有用户级、没有分层合并、没有数据库。
2. **`url` 是外链**：只接受 `http(s)://` 绝对地址；qi 不跟目标系统交换任何数据，
   点了就在新标签打开。
3. **写操作四件套**：`X-Qi-Confirm: yes` + `guard` + 校验 + 返回整份列表视图。

## 文件

```json
{
  "version": 1,
  "assistants": [
    {
      "id": "as_7f3c1a2b",
      "name": "评估方案生成助手",
      "description": "意图分析、评估要素人在环确认，分部分流式生成评估方案。",
      "tasks": ["意图分析", "评估要素人在环确认", "分部分流式生成"],
      "outputs": ["评估方案文档"],
      "url": "https://eval.example.com/assistants/plan",
      "keywords": ["方案", "指标"],
      "enabled": true
    }
  ]
}
```

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `id` | ✅ | `as_` + 8 位十六进制，**服务端生成、不可改** |
| `name` | ✅ | 1–40 字，全目录唯一（忽略大小写） |
| `description` | ✅ | 1–400 字 |
| `tasks` | ✅ | 1–8 条，每条 1–60 字；自动 trim / 去重 |
| `outputs` | ✅ | 1–8 条，每条 1–60 字；自动 trim / 去重 |
| `url` | ✅ | `http(s)://` 绝对地址；拒绝相对路径、`javascript:` / `data:` / `file:`、`user:pass@`、空白与反斜杠 |
| `keywords` |  | 0–12 条，每条 1–20 字；**只用于召回，不展示** |
| `enabled` |  | `true` / `false`，默认 `true` |

- 只有 `enabled: true` 会被推荐；`false` = 停用（记录还在，只是不出现）。
- 旧文件里的 `status` 字段**读时兼容**：`published` → `enabled: true`，
  `draft` / `archived` → `false`；下次写入时自动改写成 `enabled`。
- 条目顺序 = 文件数组顺序。新建追加到末尾。
- 文件写入是**原子替换**（临时文件 + `fsync` + `os.replace`）。
- 文件坏了（非法 JSON / 坏字段 / 不认识的 `version`）→ 读端 `503`，并指出第几条。
  不静默跳过：少一条目录不会有任何报错，那是最难查的一种。

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/assistants` | 读整份目录（含已停用条目） |
| `POST` | `/api/assistants` | 新建（201，需确认头） |
| `PATCH` | `/api/assistants` | 按 `id` 局部更新（需确认头） |
| `DELETE` | `/api/assistants` | 按 `id` 删除（需确认头） |
| `POST` | `/api/assistants/import` | 导入包内示例（需确认头） |

### 读 `GET /api/assistants`

```json
{
  "path": "/work/proj/.qi/assistants.json",
  "exists": true,
  "assistants": [
    {"id": "as_7f3c1a2b", "name": "评估方案生成助手", "description": "…",
     "tasks": ["…"], "outputs": ["…"], "url": "https://…",
     "keywords": ["…"], "enabled": true}
  ],
  "unavailable": false,
  "error": "",
  "created": 0,
  "skipped": 0
}
```

- `exists=false` 也回 200：界面据此区分“没这个文件”与“文件在但为空”。
- 没装 `qi-assistants` 扩展 → `200` + `unavailable=true` + `error`，不是 500。

### 新建 `POST /api/assistants`

```bash
curl -X POST http://127.0.0.1:3456/api/assistants \
  -H 'X-Qi-Confirm: yes' -H 'Content-Type: application/json' \
  -d '{
    "name": "评估报告生成助手",
    "description": "套模板生成评估报告。",
    "tasks": ["模板选择", "逐部分生成"],
    "outputs": ["评估报告"],
    "url": "https://eval.example.com/report"
  }'
```

- body 与文件字段同名（没有 camelCase 翻译层）。
- `id` **不收**：传了就是 `400`，由服务端生成。
- 同名 → `409`；超过 200 条 → `409`。
- 响应是**整份列表视图**（同 `GET` 形状，`created=0`）。

### 更新 `PATCH /api/assistants`

```json
{"id": "as_7f3c1a2b", "description": "新描述", "keywords": []}
```

- **只改送来的键**；省略或 `null` = 不改。
- 清空数组传 `[]`（不能传 `null`）。
- 改名同样查重；`id` 不可改（传了会落到“不认识字段”）。
- 空 patch → `400`（不要发一个什么都没改的请求）。

### 删除 `DELETE /api/assistants`

```json
{"id": "as_7f3c1a2b"}
```

硬删。想“下线但保留”请 `PATCH {"enabled": false}`。

### 导入示例 `POST /api/assistants/import`

```json
{"mode": "merge"}
```

- `merge`（默认）：跳过同名条目，重复点不报错；
- `replace`：先清空再写 3 条示例。

响应比 `GET` 多两个字段：`created`（新建数）、`skipped`（跳过数）。

示例数据在 `extensions/qi-assistants/qi_assistants/seed/assistants.json`，
三条 `url` 是 `https://eval.example.com/...` 占位，导入后请改成真实地址。

## 状态码

| 码 | 什么时候 |
| --- | --- |
| 400 | 语义校验失败:长度超限、URL 不合规、空 patch、传了 `id` |
| 404 | `id` 不存在 |
| 409 | 同名条目；导入 `replace` 后超过 200 条 |
| 422 | body 不符合 schema:字段缺失 / 类型不对 / 多了一个不认识的键 |
| 428 | 写操作缺 `X-Qi-Confirm: yes` |
| 503 | 没装 `qi-assistants`；或 `assistants.json` 损坏 / `version` 不认识 |

## 能力位

`/api/meta` 的 `assistant_write` 由**真的 import 一次** `qi_assistants.catalog` 得出。
没有它 → 设置页「应用助手」一节只读，不摆点了才 404 的键。

## 为什么没有推荐端点

推荐是**对话**的一部分：角色 `训练评估` 先 `assistant_search` 召回，再由模型选 id，
最后 `assistant_recommend` 把目录里的链接回填成卡片。模型拿不到 `url`，
所以不会编造链接。如果以后有独立前端要一次性的 `task → cards`（一条 `/api/assistants/recommend`），再把这条链
抽成一个只读端点即可；现在不做，避免同一套策略维护两份提示词。
