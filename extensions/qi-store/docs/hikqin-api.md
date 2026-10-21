# hikqin 上游契约（qi-store 的实际依赖面）

> 本文只记录 **qi-store 真正读的字段**，以及踩过的坑。全部在部署实例
> `http://10.1.43.6:6091` 上实测过；`tests/fixtures/` 里的样本就是按这份写的。
>
> 原则：**上游不改**。所以这里既写"能用的部分"，也写"必须绕开的陷阱"。

## 1. 认证

| 端点 | 说明 |
| --- | --- |
| `POST /api/auth/login` | body 是 **`{login, password}`**（字段名是 `login`，不是 `username`） |
| `POST /api/auth/refresh` | body `{refresh_token}` → **旋转**:回 `{access_token, refresh_token, token_type}`，旧 refresh 不再用 |
| `GET /api/auth/me` | 验证令牌真的还有效（`login` / `whoami --remote` 用） |

**响应形状（裸体，无 `data` 包装）**：

```json
{
  "access_token": "eyJ…", "refresh_token": "eyJ…", "token_type": "bearer",
  "user": {"id": "user-builtin-admin", "username": "admin", "role": "admin",
           "status": "active", "onboarding_completed": true, "created_at": "…"}
}
```

- access **3 小时**、refresh **7 天**（实测 `iat/exp` 差值）。
- access JWT 的 payload 含 `user_id / username / role / exp` → `whoami` 可**离线解码**。
- **没有 logout / revoke 端点**（`/api/auth` 下只有 register / login / refresh / me / password）。
  → `qi store logout` 只能删本地，文档与提示必须说清。

## 2. 错误与包装（**最容易翻车的地方**）

同一台服务上，**成功响应有三套形状**：

| 形状 | 出现在 |
| --- | --- |
| 裸对象 / 裸分页体 `{items,total,page,page_size}` | `/api/auth/*`、`/api/skills`、`/api/mcp/servers`、`/api/agents` |
| `{code,message,data}` | `/api/agent-templates`、`/api/a2a-admin`、`/api/pypi/admin/*` |
| 裸数组 | `GET /a2a/agents`（`JSONResponse([...])`） |

**错误响应统一是** `{"code": <int>, "message": "<中文>", "data": null}`（`HTTPException` 处理器）；
校验失败是 `{"code": 422, "message": "login不能为空"}`（字段名会被翻译成中文）。
未认证是 `{"code": 401, "message": "无效的认证凭据"}`。

未认证有**两种**文案，别当同一个错：

- 根本没带 `Authorization` → `{"code": 401, "message": "Not authenticated"}`
  （实测 `tests/fixtures/error_401.json`）
- 带了但无效/过期 → `{"code": 401, "message": "无效的认证凭据"}`

对 qi-store 来说两者都是"该重新登录了"（退出码 3），但日志里保留原文 ——
否则"我明明登了"这种排查会从第一句就被带偏。

⇒ `client.py` 里**逐端点拆包**，不允许一个 `_unwrap()` 通吃。拆错的表现是"列表永远是空的"，
而不是报错 —— 这就是必须有 fixtures 测试的原因。

## 3. skill

### 列表 `GET /api/skills`

| 参数 | 值 |
| --- | --- |
| `page` / `page_size` | 1 起；`page_size` ≤ 100 |
| `keyword` | 匹配名称或描述 |
| `status` | `parsing` / `parsed` / `failed` / `deleted` |
| `is_available` | `true` = 仅在架 |
| `sort_by` / `sort_order` | 默认 `upload_time` / `desc` |

**商店口径：`status=parsed&is_available=true`。**
`deleted` 是"下架软删除"：行**保留**作稳定身份（注释原话："已装账本/绑定引用不悬空"），
同名重传复用该行 —— 所以 **id 才是稳定身份，名字不是**。

响应（裸分页体）每项：

```json
{"id": 4, "name": "ppt-generator", "description": "…", "status": "parsed",
 "scan_status": "safe", "reviewed": false, "is_available": true,
 "file_size": 68369, "upload_time": "2026-10-19T01:32:55",
 "is_pinned": false, "download_count": 0, "owner_name": "admin",
 "installed_sandboxes": ["默认沙箱"], "agents": [{"id": "agent-…", "name": "技能调试"}]}
```

`scan_status`：`unscanned` / `scanning` / `safe` / `critical` / `high` / `medium` / `low`。
→ `critical` / `high` 在安装前额外确认一次。

### 详情与文件

| 端点 | 用途 |
| --- | --- |
| `GET /api/skills/{id}` | `info` |
| `GET /api/skills/{id}/files` | 文件树（`info` 展示、`download` 预检） |
| `GET /api/skills/{id}/files/content?path=…` | 单文件正文（**审查用**：看 SKILL.md 与脚本） |
| `GET /api/skills/{id}/scan` | 扫描详情（findings） |

### 取包 `GET /api/skills/{id}/download`

返回 zip：

- **顶层目录名 = `skill.name`**，`SKILL.md` 在其下（所以解压后要按需下钻定位）。
- ⚠️ **这个端点会 `download_count++`** —— 它是 qi-store 对上游**唯一的数据影响**（写操作）。
  正常 `install` 走它（顺带给商店贡献计数）；`download --no-count` 留给纯审查场景
  （实现上可能只能二选一，若做不到就在文档里承认：审查也会计一次下载）。
- 打包时**跳过隐藏文件**（`.` 开头），所以上游的隐藏配置不会跟过来。

## 4. mcp

| 端点 | 说明 |
| --- | --- |
| `GET /api/mcp/servers?page&page_size` | 裸分页体 `{total,page,page_size,items}` |
| `GET /api/mcp/servers/{id}` | 详情，**额外给 `headers`（明文 JSON）与 `tool_count`** |

每项字段：`id / name / transport / description / icon / url / auth_type / enabled /
connection_status / last_connected_at / last_error / created_at / updated_at / tool_count /
owner_name`。

- `transport` ∈ `stdio` / `sse` / `streamable-http`（默认后者）。
- `auth_type` ∈ `none` / `bearer` / `apikey` / `basic` / `custom`。
- 🔴 **`auth_config` 永不下发**（创建/更新请求体里才有，响应 schema 里没有）
  → 装到本地必须走 `bearerTokenEnv` / `${VAR}`（见 README §4.3）。
- `headers` **是明文**的（上游只加密 `auth_config`，而 `auth_config` **不在任何响应 schema 里**）。
  ⇒ 下载/安装时 **`headers` 原样抄**，不猜、不改写成变量；只有 `auth_config` 那部分因为
  上游根本没给，本地才需要留占位（见 README §4.3）。
- `enabled=false` 的 server 仍会出现在列表里 → 商店照样能装（本地由用户决定是否 `disabled`）。

## 5. a2a

| 端点 | 认证 | 说明 |
| --- | --- | --- |
| `GET /a2a/agents` | **无** | 已发布智能体的 Agent Card **裸数组** |
| `GET /a2a/agents/{agent_id}/card.json` | **无** | 单张名片 |
| `GET /.well-known/agent-card.json` | **无** | 入口目录卡 |

⇒ **a2a 这个 kind 未登录也能列、也能装**（README §5.1 的状态矩阵据此而来）。
卡片里 `supportedInterfaces[0].url` 与 `tenant` 是 qi-a2a 认的两个键 → 本地只写
`{"cardUrl": "<base_url>/a2a/agents/<id>/card.json"}`，其余交给 qi-a2a 解析。

## 6. 未实现的 kind（留槽，别顺手实现）

### agent —— hikqin 侧有两套格式，都没对齐

1. **模板蓝图** `GET /api/agent-templates/{id}`（`{code,message,data}`，`data.content`）：
   `format_version: 1`，`agent{name,description,system_prompt,config,opening_*,model,
   mcp_services[],skills[],data_sources[],knowledge_bases[],sub_agents[]}`；
   skill 文件是 **base64 内联**；`model.api_key` 置空 + `requires_config`；
   `mcp_services[].auth_config` 也置空。
2. **离线包** `GET /api/agents/{id}/download`（zip）：Agent Plugins Spec v1.0.0，
   根 `plugin.json` + 规范 `mcp.json`，hikqin 私有配置在
   `extensions["com.hikqin.agent"]`；**密钥是 AES-256-GCM 密文**（qi 没有那把密钥）。

对 qi 的角色目录（`agent.md` + 私有 `mcp.json` + 私有 `skills/`）来说，两套都要写转换器，
且**装完都没有可用模型**。所以 v1 不做，`--kind agent` 直接报未实现。

### extension —— 上游其实已经有 PEP 503 索引

`GET /api/pypi/simple/`（**无认证**）、`/api/pypi/simple/{包}/`、`/api/pypi/packages/{包}/{文件}`。
理论上 `qi install --index-url http://10.1.43.6:6091/api/pypi/simple/ <包>` 就能让商店
同时分发 qi 扩展本身。**留给 P4**：这是 `pip` 的活，不该由 qi-store 重新实现一遍。

## 7. 写代码前先抓的 fixtures

按这份清单抓真实响应存进 `tests/fixtures/`（脱敏：token 换成假值）：

| 文件 | 来源 |
| --- | --- |
| `login.json` | `POST /api/auth/login` |
| `skills.json` | `GET /api/skills?status=parsed&is_available=true&page_size=2` |
| `skill_detail.json` | `GET /api/skills/4` |
| `skill_files.json` | `GET /api/skills/4/files` |
| `skill_download.zip` | `GET /api/skills/4/download`（只留目录结构，不存全量） |
| `mcp_servers.json` | `GET /api/mcp/servers`（**当前实例是空的** → 用 `MCPServerResponse` 手造一条，注明"合成样本"） |
| `a2a_agents.json` | `GET /a2a/agents` |
| `error_401.json` | 无 token 打 `GET /api/skills` |

> 抓取时记下实例状态：**skills 4 个、agent 模板 1 个、mcp server 0 个**。
> mcp 那条 fixture 是合成的，不是真实响应 —— 这一点必须写在文件注释里，
> 否则下一个人会以为线上真有那个 server。
