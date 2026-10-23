# 元信息

前端的**第一站**:先读这里,再决定哪些功能要画、哪些要藏。通读通用约定见 [`README.md`](README.md)。

## `GET /api/meta`

宿主自述。老前端靠它降级 —— **不靠猜,也不白屏**。

带 `guard`(Host + 口令)。

### 响应 `Meta` (200)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `app` | `string` | 恒为 `"qi-lab"`(应用身份,与 qi-web 的 `"qi-web"` 区分) |
| `contract` | `string` | 契约版本,当前 `"2"`。形状不兼容时前端**显式报错**,而不是白屏 |
| `version` | `string` | `qi_agent.__version__` |
| `default_cwd` | `string` | 默认工作目录绝对路径(会话新建、文件面板的根) |
| `auth_required` | `bool` | 这次启动有没有设口令 —— 界面据此显示"要不要登录" |
| `static_ready` | `bool` | `qi_lab/static/` 在不在。`false` = 前端没构建(`GET /` 会给占位页) |
| `capabilities` | `object<string, bool>` | 能力位表(见下) |

```bash
curl -s http://127.0.0.1:3456/api/meta | jq
```

```json
{
  "app": "qi-lab",
  "contract": "2",
  "version": "0.x.0",
  "default_cwd": "/Users/you/proj",
  "auth_required": false,
  "static_ready": true,
  "capabilities": {
    "sse": true, "streaming": true, "cancel": true,
    "auth_write": true, "config_read": true, "trajectory": true,
    "model_switch": true, "provider_write": true,
    "mcp_write": true, "skill_write": true,
    "agent_write": true, "a2a_write": true,
    "store_write": true, "kb_write": true,
    "activity": true, "ag_ui": true, "ui_v1": true
  }
}
```

### 能力位

| 键 | 含义 | 没有它会怎样 |
| --- | --- | --- |
| `sse` / `streaming` | 流式输出 | 前端不接流,退成"等整段结果" |
| `cancel` | 可中断 | 不画停止按钮(中断 = 客户端 abort 请求本身,没有单独端点) |
| `auth_write` | `POST/DELETE /api/auth/{provider}` 可用 | 凭证只能去 TUI 配 |
| `config_read` | `/api/config` 有只读视图 | 设置页不画 provider 卡片 |
| `trajectory` | 轨迹回放(`CUSTOM qi.history` 那一份) | 刷新后只剩对话,分派卡/工具卡没了 |
| `model_switch` | `/api/models` + `/api/model` 存在 | 模型名只显示、不能换(TUI 才能换) |
| `provider_write` | `POST /api/providers` 存在 | 「添加 Provider」只保留“配置凭证 / 登录已有” |
| `mcp_write` | `/api/mcp` 的 POST/PATCH/DELETE 存在 | **MCP 节退回只读**:不画"添加/编辑/删除" |
| `skill_write` | `/api/skills` 的 POST/DELETE/import/reload 存在 | **技能节退回只读**:不画"添加/删除/导入/重扫"(老宿主根本没有这些端点) |
| `agent_write` | `DELETE /api/agents` 存在 | **Agent 节退回只读**:不画"删除"(读端点一直在,所以只是少一个键) |
| `a2a_write` | `/api/a2a` 的 POST/PATCH/DELETE + `/api/a2a/probe` 存在 | **A2A 节退回只读**:不画"添加/编辑/删除/探测"(读端点一直在) |
| `store_write` | `/api/store/*` 的写端点可用 | **商店节只读**:不画"登录/安装/卸载";面板仍可读 `status` / `search` / `installed` |
| `kb_write` | `/api/kb/*` 的写端点可用 | **知识库节只读**:不画"provider / KB / 默认参数"的写操作;检索与问答仍可用 |
| `activity` | 委派直播(`ACTIVITY_SNAPSHOT` 帧)在发 | **看不到子运行在干什么**:结束前的工具卡里只有参数(退回一期) |
| `ag_ui` | 事件形状是 AG-UI | 退成折叠显示原始 JSON |
| `ui_v1` | `details["ui"]` 的词汇表可用 | 同上 |

`store_write` / `kb_write` **不是写死的 `true`**:宿主每次请求都真的 `import` 一次
`qi_store.service` / `qi_kb.service`,没装就是 `false`。这样界面不必靠“点了才 404”来发现
少装了一个扩展。老前端不认识某个新键 → 按上表退回上一档,不会白屏。

> 老宿主没有某个键 ≠ 前端崩:每个键都对应一条"退回上一档"的分支。
> 新增端点时**先加能力位**,老前端才不会去调一个 404/405 的地址。

## `GET /api/health`

探活。**唯一不带 `guard` 的 `/api` 路由** —— 探活的意义就是"没口令也答一声",
回的三个字段不含任何环境信息(不吐路径、不吐版本号以外的东西)。

### 响应 (200)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ok` | `bool` | 恒 `true`(能答就说明进程活着) |
| `app` | `string` | `"qi-lab"` |
| `contract` | `string` | `"2"` |

```json
{"ok": true, "app": "qi-lab", "contract": "2"}
```

## 相关状态码

`/api/meta` 自己也过 `guard`(只有 `/api/health` 不过),所以通用码是:

| 码 | 什么时候 |
| --- | --- |
| 400 | `Host` 头不在允许表里(适用于**所有**带 `guard` 的端点,不只 `/api/meta`) |
| 401 | 有口令但没带凭证(同样适用于所有带 `guard` 的端点) |
| 503 | 配置/装载错误(`ConfigError` / `LoadError`)—— 打在任何走 `runtime` 的端点上 |
