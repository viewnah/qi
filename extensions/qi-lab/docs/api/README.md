# qi-lab HTTP API

`qi_lab/app.py` 是这些接口的**唯一真相**;本文与代码同仓,改端点的人顺手改这里。
`extensions/qi-lab/tests/test_lab_docs_api.py` 会把 `create_app()` 实际注册的每一条 `/api` 路由逐条对着
本文目录查一遍 —— 少写一个就红,防止"文档说有、代码已删"与"代码有、文档没有"两种漂移。

## 起服务

```bash
qi lab                      # 默认 127.0.0.1:3456(4 位端口)
qi lab -p 4321 --no-open    # 换端口、不自动开浏览器
qi lab -H 0.0.0.0 --password 口令     # 跨回环:必须给口令,否则拒启
```

| 项 | 值 |
| --- | --- |
| 基址 | `http://127.0.0.1:3456` |
| 口令 | `--password` 或环境变量 `QI_LAB_PASSWORD`(不设 = 不校验) |
| 允许的 Host | `QI_LAB_ALLOWED_HOSTS`(逗号分隔;回环与绑定地址永远放行) |
| 默认工作目录 | 启动时的 cwd(可用 `--cwd` 指定) |
| OpenAPI / Swagger | **没有**(`docs_url=None, redoc_url=None`)—— 本文就是文档 |

Python 侧是 editable 安装,改完即生效;前端改动要 `cd ui && npm run build` 才进 `qi_lab/static/`。

---

## 通用约定

### 1. 认证与 Host(`guard`)

**除 `GET /api/health` 外,每一条 `/api` 路由都先过 `guard`**,顺序是先 Host 后口令:

1. **Host 头**不认识 → `400`(细节见 `QI_LAB_ALLOWED_HOSTS`)。反代场景要把外部域名显式加进去,
   否则伪造的 `Host` 头能过。
2. **口令**:设了口令却没有(或错的)凭证 → `401`,并带 `WWW-Authenticate: Basic realm="qi-lab"`。
   两种写法都认:
   ```bash
   -H "Authorization: Bearer <口令>"
   -H "Authorization: Basic $(printf '任意用户名:<口令>' | base64)"   # 用户名随意
   ```
   比较用 `secrets.compare_digest`(定长,不按字符提前返回)。

没设口令时(默认只绑回环)全部放行 —— 这正是"跨回环必须给口令"那条拒启规则换来的。

### 2. 写操作的第二道闸:确认头

**9 条写端点**要求显式带:

```
X-Qi-Confirm: yes
```

缺(或不是 `yes`,大小写与首尾空白不计)→ **`428 Precondition Required`**:

| 端点 | 干什么 |
| --- | --- |
| `POST /api/mcp` | 新建 MCP server |
| `PATCH /api/mcp` | 改 MCP server |
| `DELETE /api/mcp` | 删 MCP server |
| `POST /api/auth/{provider}` | 写入凭证 |
| `DELETE /api/auth/{provider}` | 删除凭证 |
| `POST /api/skills` | 新建技能 |
| `DELETE /api/skills` | 删技能(整目录) |
| `POST /api/skills/import` | 导入技能 |
| `DELETE /api/agents` | 删角色(整目录) |

为什么:`.qi/mcp.json` 跟着仓库走、凭证文件在 `~/.qi/`,一次误写会被提交出去或悄悄改掉
**下一次请求用的密钥**;技能则是**会进提示词的代码**,项目层就在 `.qi/skills/` 里 ——
误写会被 `git add .` 带走、再在下一轮对话里被模型读进去;角色目录里还带着自己的
`mcp.json`,一次误删连那层私有声明一起没。与写凭证同一道闸,
前端弹层确认后才带上这个头。

```bash
curl -X PATCH http://127.0.0.1:3456/api/mcp -H 'X-Qi-Confirm: yes' -H 'Content-Type: application/json' -d '…'
# 不带头:428 {"detail":"写操作需要二次确认:带上请求头 X-Qi-Confirm: yes"}
```

### 3. 错误形状

一律 `{"detail": "…"}`(FastAPI `HTTPException`),HTTP 状态码按语义:

| 码 | 什么时候 |
| --- | --- |
| 400 | 参数/文件问题:Host 不允许、路径越界、目录不是目录、`mcp.json` 解析失败、传输不认识、声明不完整、server 名有空白、技能名不合规 / 没有 description / 导入源结构不对、角色删除目标越界(符号链接)/删不动、`provider` 与 `model` 只给了一个 |
| 401 | 缺口令或口令错 |
| 403 | 读目录/文件没有权限 |
| 404 | 会话 / server / 角色 / 文件 / 目录 / 凭证 / 技能 不存在(导入的 `source` 也不在) |
| 409 | **占用冲突**:同层已有同名 server / 同名技能(或路径被占)、导入不覆盖;会话正在运行(删/压/分叉);同一会话已有活跃 run;工作区/未分组里有会话在跑 |
| 415 | `/api/files/raw` 的类型不在原样预览白名单 |
| 422 | body 不符合 schema(pydantic)、AG-UI 输入不合法、模型配对不完整 |
| 428 | 写操作缺 `X-Qi-Confirm: yes` |
| 503 | 配置/装载错误(`ConfigError` / `LoadError`)—— 比 500 可读;没装 qi-agents 时**删角色** |

### 4. 契约与能力位

`GET /api/meta` 回契约版本(`contract = "2"`)与**能力位**;前端据此降级,不靠猜。
拿不准某个功能在不在,读 `capabilities`:

| 能力位 | 含义 |
| --- | --- |
| `sse` / `streaming` / `cancel` | 流式输出、可中断(中断 = 客户端 abort 请求本身) |
| `auth_write` | `/api/auth/*` 可写 |
| `config_read` / `trajectory` | `/api/config` 只读视图、轨迹回放 |
| `model_switch` | `/api/models` + `/api/model`(清单与切换) |
| `mcp_write` | `/api/mcp` 的 POST/PATCH/DELETE(**没有它 → 界面把 MCP 节当只读**) |
| `skill_write` | `/api/skills` 的 POST/DELETE + `/api/skills/import`(**没有它 → 技能节只读**) |
| `agent_write` | `/api/agents` 的 DELETE(**没有它 → Agent 节只读**;读端点一直在) |
| `activity` | 委派直播帧 `ACTIVITY_SNAPSHOT`(**没有它 → 运行中的子运行看不到在干什么**,退回一期) |
| `ag_ui` / `ui_v1` | 事件形状是 AG-UI;`details["ui"]` 的词汇表(缺则退回折叠显示原始 JSON) |

### 5. 密钥不回显(读写两侧同一条)

- **凭证**:响应里只有 `credential_masked`(`…` + 尾 4 位)与 `credential_source`,永远没有明文。
- **MCP 声明**:`env` / `headers` **只回键名**;stdio 的 `command` / `args` 连给都不给。
  所以这三个值**只进不回** —— 它们是 `npx -y x --token=sk-…` 这类最常写明文密钥的地方。
  读不回来 ⇒ 界面那几个框在编辑态是「留空 = 不改」(见 `mcp.md`)。
- 写端点的**响应回的是结构视图**(`McpList`),同样一个值都不带。

---

## 端点总表(64)

| 方法 | 路径 | 说明 | 文档 |
| --- | --- | --- | --- |
| GET | `GET /api/meta` | 宿主自述:契约、能力位、默认 cwd | `meta.md` |
| GET | `GET /api/health` | 探活(**唯一不带 guard** 的 API) | `meta.md` |
| GET | `GET /api/sessions` | 会话列表 | `sessions.md` |
| POST | `POST /api/sessions` | 新建会话(201) | `sessions.md` |
| GET | `GET /api/sessions/{sid}` | 会话明细(分页窗口) | `sessions.md` |
| PATCH | `PATCH /api/sessions/{sid}` | 改标题 | `sessions.md` |
| DELETE | `DELETE /api/sessions/{sid}` | 删一条(204) | `sessions.md` |
| DELETE | `DELETE /api/sessions` | 批量删「未分组」 | `sessions.md` |
| GET | `GET /api/sessions/{sid}/export` | 导出 JSONL 文件 | `sessions.md` |
| POST | `POST /api/sessions/{sid}/compact` | 手动压缩上下文 | `sessions.md` |
| POST | `POST /api/sessions/{sid}/fork` | 分叉出新会话(201) | `sessions.md` |
| POST | `POST /api/ag-ui` | **运行入口**:POST → SSE 流 | `ag-ui.md` |
| GET | `GET /api/workspaces` | 工作区显示名表 | `workspaces.md` |
| PATCH | `PATCH /api/workspaces` | 改显示名(目录不动) | `workspaces.md` |
| DELETE | `DELETE /api/workspaces` | 删工作区(连同其会话) | `workspaces.md` |
| GET | `GET /api/fs/dirs` | 目录选择器(只列目录) | `workspaces.md` |
| GET | `GET /api/files` | 列一层目录(会话目录内) | `files.md` |
| GET | `GET /api/files/content` | 文本预览 | `files.md` |
| GET | `GET /api/files/raw` | 原字节预览(图片/PDF/HTML) | `files.md` |
| GET | `GET /api/agents` | 角色列表(qi-agents) | `agents.md` |
| DELETE | `DELETE /api/agents` | 删角色(整目录,需确认头) | `agents.md` |
| GET | `GET /api/mcp` | MCP 声明表(三层,只回结构) | `mcp.md` |
| POST | `POST /api/mcp` | 新建 server(201,需确认头) | `mcp.md` |
| PATCH | `PATCH /api/mcp` | 改 server(只改送来的键) | `mcp.md` |
| DELETE | `DELETE /api/mcp` | 删 server(只删一层) | `mcp.md` |
| GET | `GET /api/a2a` | A2A 条目(两层原样 + 合并后的生效视图) | `a2a.md` |
| POST | `POST /api/a2a` | 新建条目(201,需确认头) | `a2a.md` |
| PATCH | `PATCH /api/a2a` | 改条目(只改送来的键) | `a2a.md` |
| DELETE | `DELETE /api/a2a` | 删条目(只删一层) | `a2a.md` |
| POST | `POST /api/a2a/probe` | 去真实端点拉一次名片(只读) | `a2a.md` |
| GET | `GET /api/config` | provider/凭证/默认模型只读视图 | `config.md` |
| GET | `GET /api/models` | 可选模型清单 + 当前值 | `config.md` |
| POST | `POST /api/model` | 换模型 / 换思考级别 | `config.md` |
| GET | `GET /api/skills` | 顶层技能清单 | `skills.md` |
| POST | `POST /api/skills` | 新建技能(201,需确认头) | `skills.md` |
| DELETE | `DELETE /api/skills` | 删技能(整目录,需确认头) | `skills.md` |
| POST | `POST /api/skills/import` | 从本机路径导入技能(201,需确认头) | `skills.md` |
| POST | `POST /api/skills/reload` | 重扫技能目录(需确认头;不改文件,只刷缓存) | `skills.md` |
| GET | `GET /api/store/status` | 商店源与登录状态(不发请求) | `store.md` |
| POST | `POST /api/store/login` | 登录(需确认头;密码只过一次,令牌不回浏览器) | `store.md` |
| POST | `POST /api/store/logout` | 退出(需确认头;删本地凭证,上游无吊销) | `store.md` |
| GET | `GET /api/store/search` | 搜索条目(带已装状态,含"哪一类没查成") | `store.md` |
| GET | `GET /api/store/installed` | 已装条目(读账本,离线可用) | `store.md` |
| POST | `POST /api/store/plan` | 只算不写:写哪里 / 冲突 / 待补配置 | `store.md` |
| POST | `POST /api/store/install` | 安装(需确认头;高危要 allowUnsafe) | `store.md` |
| POST | `POST /api/store/uninstall` | 卸载(需确认头;只删自己写的) | `store.md` |
| GET | `GET /api/store/check` | 账本 ↔ 上游对比 + 声明未装 | `store.md` |
| GET | `GET /api/kb/status` | provider / 已绑定 KB / 默认参数 | `kb.md` |
| GET | `GET /api/kb/providers` | provider 列表(不含令牌) | `kb.md` |
| POST | `POST /api/kb/providers` | 新建/更新 provider(需确认头) | `kb.md` |
| POST | `POST /api/kb/providers/login` | provider 登录(需确认头) | `kb.md` |
| DELETE | `DELETE /api/kb/providers` | 删除 provider(需确认头;有 KB 时 409) | `kb.md` |
| POST | `POST /api/kb/plan` | 只算不写:安装 / 已装 / rebind | `kb.md` |
| GET | `GET /api/kb/available` | 可用 KB + 已绑定状态 | `kb.md` |
| GET | `GET /api/kb/docs` | 知识库文档列表 | `kb.md` |
| POST | `POST /api/kb/search` | 只检索 chunk | `kb.md` |
| POST | `POST /api/kb/ask` | 上游 RAG 问答 | `kb.md` |
| POST | `POST /api/kb/ask/stream` | 流式 RAG 问答（SSE） | `kb.md` |
| POST | `POST /api/kb/install` | 安装 KB(需确认头;冲突 409) | `kb.md` |
| POST | `POST /api/kb/uninstall` | 卸载 KB(需确认头) | `kb.md` |
| PATCH | `PATCH /api/kb/defaults` | 写默认参数(需确认头) | `kb.md` |
| GET | `GET /api/extensions` | 已装载的扩展名 | `config.md` |
| POST | `POST /api/auth/{provider}` | 写入凭证(204,需确认头) | `config.md` |
| DELETE | `DELETE /api/auth/{provider}` | 删除凭证(204,需确认头) | `config.md` |

静态前端挂在 `GET /`(最后匹配,不进 `/api`):没有构建产物时回一个占位页,
告诉你去 `cd extensions/qi-lab/ui && npm run build`。

## 按文件查

| 文件 | 内容 |
| --- | --- |
| [`meta.md`](meta.md) | `/api/meta`、`/api/health` —— 能力位怎么读 |
| [`sessions.md`](sessions.md) | 会话的增删改查、导出、压缩、分叉、批量删 |
| [`ag-ui.md`](ag-ui.md) | `POST /api/ag-ui`:请求体、SSE 帧格式、事件表、取消语义 |
| [`mcp.md`](mcp.md) | MCP 声明的读与增删改(三层、确认头、只改送来的键) |
| [`a2a.md`](a2a.md) | A2A 条目:两层深合兼、确认头、`null` = 不改、名片探测 |
| [`agents.md`](agents.md) | 角色:读(元数据,不回正文)与删除(两层、按显示名、整目录、只删该层) |
| [`config.md`](config.md) | 配置/模型/扩展的只读视图,凭证写入 |
| [`skills.md`](skills.md) | 技能:读(六层来源)与添加 / 删除 / 导入 / 重扫缓存(确认头、可写边界、zip 防御) |
| [`workspaces.md`](workspaces.md) | 工作区显示名、删除语义、目录选择器 |
| [`files.md`](files.md) | 文件三端点:边界、排序、限额、预览白名单 |
| [`store.md`](store.md) | 商店:hikqin 条目 → 本地落盘;四类标签、登录(密码只过一次)、确认头、高危闸、失败映射 |
| [`kb.md`](kb.md) | 知识库:provider / KB / 检索 / 问答 / 默认参数;`(instance, kbId)` 去重与 rebind |
