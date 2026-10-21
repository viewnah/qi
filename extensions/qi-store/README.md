# qi-store

把 **hikqin** 当成应用商店：`qi store install` 一条命令，把 MCP server 配置 / Skill 包 /
A2A 名片装进 qi 自己的配置目录。

> **状态：P0–P3 已落地（2026-10-21）** —— 命令行九条 + qi-lab 面板（`/api/store/*` 七条 +
> 设置面「商店」一节：**四类标签 + 页面登录**）。测试：qi-store 91 条 + qi-lab 商店 API 17 条。
> 未做：`agent` kind（P4，格式未对齐）、`extension` kind（P4，上游已有 PEP 503 索引）。
> 本文就是实现规格 —— 命令面、契约、落位表、退出码都钉在这里；不一致时以本文为准
> （改实现要同时改这里）。
>
> 相关：[hikqin API 契约](docs/hikqin-api.md)（上游端点与陷阱）、
> [配置文件](docs/config-files.md)（`store.json` 两层 / `installed.json` 账本）。
> 上游侧**不改一行代码**，也**不要求注册专用账号** —— 这一点见 §2。

## 0. 一句话边界

| 做 | 不做 |
| --- | --- |
| 把 hikqin 上**登录账号能看到的**资产拉下来，转成 qi 的格式并落盘 | 不做商店服务端；不改 hikqin；不在 hikqin 注册专用账号 |
| 写账本，可 `uninstall` / 之后 `check` 上游更新 | 不搬运密钥（上游也不下发），不执行安装脚本 |
| `download` 只取件（不落配置、不记账） | 不替用户决定资产是否可信（只透出上游的 `scan_status`） |

v1 支持的 kind：**`skill` / `mcp` / `a2a`**。
`agent` 留槽但**未实现**（hikqin 的 agent 模板蓝图与 qi 的角色目录格式尚未对齐，
`--kind agent` 直接报「未实现」，不猜格式）。

## 1. 装

```bash
qi install qi-store                      # 装好的包(pip 通道 + settings.packages 声明)
```

开发时两条路(都不需要先装包):

```bash
# ① 目录通道:软链到用户级扩展目录(macOS/Linux 用 ln -s,Windows 用 mklink /J)
ln -s <repo>/extensions/qi-store ~/.qi/agent/extensions/qi-store
qi store sources

# ② 包通道:pip 装上就自动有 entry point,再在 settings.packages 里声明它
pip install -e extensions/qi-store
```

⚠️ **`qi -e ./extensions/qi-store store …` 不行**:`-e` 是 typer 的全局旗标,
而扩展子命令的派发要求 `argv[0]` 就是子命令名(`_dispatch_extension_command`)。
所以要么走上面两条路,要么直接用 python 调 `qi_store.cli.run`。

装完 `qi` 下次启动自动发现(entry point `qi.extensions` 或目录通道的 `extension.py`),
顶层多出 `store` 子命令。依赖只有 `httpx`;**不把 `qi-coding-agent` 写进 `dependencies`**
(宿主被自己扩展降级的坑,见 [../../docs/extensions.md](../../docs/extensions.md) §10)。

## 2. 谁能看到什么：完全由上游决定

**这是本扩展唯一一条"零假设"规则。** qi-store 不区分管理员与普通用户，不做提权，也不要求
专用账号 —— 它拿到的就是**你登录的那个账号在上游能看到的资产**：

| 你的账号 | `qi store search` 看到 |
| --- | --- |
| admin | 上游全量（hikqin 的 `share_filter` 对管理员不过滤） |
| 普通用户 | 自己的 + 被分享给自己的（hikqin 的 `Share.scope = public / specific`） |

所以"商店里有什么"是**运营问题**（谁上架、分享给谁），不是客户端问题。文档里不出现
"用 admin 登录"这种默认假设。

**账号存在哪**：就是 `store.json` 里那个源的 `auth` 段（`login` / `role` / `access` / `refresh` /
`exp`）—— 账号是**这个扩展自己的配置**，不借宿主的 `~/.qi/agent/auth.json`（那是模型 provider
的密钥库，同名不同物）。两层规则：

| 层 | 允许字段 | 理由 |
| --- | --- | --- |
| 用户级 `~/.qi/agent/store.json` | 全部，**含 `auth`** → 写时 0600 | 只在你本机，谁都不提交 |
| 项目级 `<cwd>/.qi/store.json` | 只准非敏感字段：`stores.<名>.{baseUrl,apiPrefix,priority,disabled}` / `defaultStore` / `install` / `wanted[]`；**出现 `auth` 忽略并警告** | 这份要进仓库，凭证落进去就是泄露 |

换人/换号只改用户级那一份，项目的共享配置一行不动。详见
[docs/config-files.md](docs/config-files.md) §2。

**结构性防护**：即使某个账号权限很宽，`client.py` 也只允许 `GET` + 三个认证端点
（`login` / `refresh` / `me`），白名单写成常量并有测试钉住。把"超权"从"依赖人小心"
变成"代码写不出破坏性请求"。

## 3. 命令面

```
qi store login   [URL]             # 建立/替换凭证
qi store logout   [--all]          # 删本地凭证(上游无吊销接口,见 §3.2)
qi store whoami   [--remote]       # 我是谁、还剩多久
qi store sources                   # 源清单(v1 一个)

qi store search   [关键词] [-k KIND] [--limit N] [--json]
qi store info     <id>
qi store install  [id...] [-k KIND] [--scope …] [-y] [--force] [--name KEY] [--dry-run] [--print] [--offline]
qi store download [id...] [-o DIR] [--raw] [--offline]

qi store list     [-k KIND] [--json]              # 已装(读账本,离线可用)
qi store uninstall [id...] [--scope …] [-y]

qi store check     [--json] [--offline]           # 账本 ↔ 上游:可升级 / 下架 / 声明未装
qi store upgrade   [id...] [--dry-run] [-y]       # 省略 id = 升级所有可升级的
qi store sync      [--scope …] [--dry-run] [-y] [--force]   # 按 wanted[] 补装缺的
```

### 3.0 三个“不动手”的命令

| 命令 | 做什么 | 不做什么 |
| --- | --- | --- |
| `check` | 把账本与上游比一遍(内容指纹),列出可升级 / 已下架 / **声明了但没装** | 不改任何东西(纯报告) |
| `upgrade` | 按记录**原作用域与原落盘名**重装新版本,旧版备份为 `*.bak-<时间>` | 不跨作用域合并,不推测“你想装到哪” |
| `sync` | 按 `wanted[]` 装**缺的** | **不替你卸“多出来的”** —— 声明是“应该有”,不是“只该有” |

`check` / `sync` 都吃 `--json`,给 qi-lab 的面板用。

挂载方式：`api.register_cli_command("store", handler)` + argparse（照
[`qi-lab/serve.py`](../qi-lab/qi_lab/serve.py)：`prog="qi store"`，用法错误自动退出码 2）。
`store` 不与 core 子命令撞名（`_core_subcommand_names()` 里没有它）。

### 3.1 id 语法

```
<id> := [<kind>:]<key>[@<store>]
kind := skill | mcp | a2a          (agent 预留，v1 报未实现)
key  := 数字 id | 名称 | agent_id | 名称子串
store:= store.json 里的源名(默认 defaultStore，v1 可省)
```

上游的真实数据长这样（部署实例）：

```
skill:4   ppt-generator      ← skills 表主键，GET /api/skills 每项的 id
skill:3   skill-creator
a2a:agent-4c4af5bf           ← agent 的字符串 id
```

三条解析规则（各有一条测试）：

1. **裸数字必须带 kind 前缀**。`skill:4` 与 `mcp:4` 是两套独立主键，`qi store install 4`
   一定歧义 → 报错并列候选，**不猜**。
2. 非数字 key 先在**账本**里找同名（所以 `uninstall ppt-generator` 不用记 id），再到
   catalog 找；多处命中 → 报歧义 + 候选表，退出码 4。
3. `@store` 为多源预留，v1 只校验源存在。

**账本用 id 认身份，用内容哈希认版本**：上游的下架是软删除（`SkillStatus.DELETED`：
"行保留作稳定身份，同名重传复用该行"），所以名字会被复用、id 不会。
`installed.json` 同时存 `itemId` 与 `version: sha256:…`，`check`/`upgrade` 才有意义。

### 3.2 `login` / `logout` / `whoami`

```bash
qi store login                                   # 交互：URL → 用户名 → 密码
qi store login http://10.1.43.6:6091 -u admin     # 只问密码
qi store login --token "$JWT"                    # CI：直接给 token
qi store login -u admin --password-stdin < pw    # CI：密码走管道(不落 shell 历史)
```

| 情形 | 行为 |
| --- | --- |
| `store.json` 无源且未给 URL | 报错 + 一行示例，退出码 2 |
| 给的 URL 与已配置源不同 | 提示"另一个实例，替换源？"（`-y` 跳过） |
| 密码 | `getpass` 不回显；`--password-stdin` 读一行。**不接受命令行明文**（逃不掉 shell 历史与 `ps`） |
| 成功 | 立刻 `GET /api/auth/me` 验证 → 打印 `admin (admin) @ http://… · 3h 有效，7 天内自动续` → 0 |
| 失败 | 透传上游 message（如"用户名或密码错误"）→ 3；**先验后记，不写任何文件** |

- 凭证就写在**用户级** `store.json` 的 `stores.<源名>.auth` 里（写时 0600）；项目级那份出现
  `auth` 会被忽略并警告（它要进仓库）。
- CI 第三条路：环境变量 `QI_STORE_TOKEN`（优先于文件，不落盘）。
- **自动续期**：请求前若 `exp - 60s < now` → `POST /api/auth/refresh`，写回。续期失败抛
  `NotLoggedIn`，命令层统一转成 `凭证已过期(7 天未使用)，请重新登录：qi store login` → 退出码 3。
  **不把凭证过期吞成上游 401** —— 那是两件事。
  ⚠️ 上游续期会**换发新的 refresh token**（3h access / 7d refresh），所以写回时**必须覆盖**
  refresh，只更新 access 会让凭证在 7 天后突然整个失效。
- `logout`：删本地凭证（`--all` 连缓存）。输出明确写「本地凭证已删除；**上游无吊销接口**，
  如需立即失效请改密码」。幂等，恒 0。
- `whoami`：默认**离线解 JWT**（payload 里有 `username/role/exp`），不发请求；
  `--remote` 才打 `/api/auth/me` 确认。未登录 → 3。

### 3.3 `install` vs `download`

**`download` 只搬文件，`install` 才让 qi 认识它。** `install = download + 格式转换 + 落位 + 记账`。

| | `qi store download skill:4 -o ./review` | `qi store install skill:4` |
| --- | --- | --- |
| 磁盘 | `./review/ppt-generator/SKILL.md` …（上游产物） | `~/.qi/agent/skills/ppt-generator/SKILL.md` … |
| 改配置 | ❌ | ❌（skill 靠目录发现） |
| 写账本 | ❌ | ✅ |
| qi 下次启动看见 | ❌ 完全不知道 | ✅ 进 `top_skills`（**进提示词**） |
| 要确认 | 不用（无副作用） | 要（会改变模型行为） |
| 可 `uninstall` | 不适用 | ✅ 只删账本登记过的文件 |

`download` 存在的两个理由（不是"install 的简化版"）：

1. **先审查再装**。skill 里是别人写的提示词与脚本（可能带 `scripts/*.py`），上游只给一个
   `scan_status` 标签。`download` 是唯一"无副作用"的查看方式。
2. **落点不该由商店假设**。mcp 片段可能属于全局 `mcp.json`，也可能是**某个角色私有**的
   `<role>/mcp.json`（qi-agents 支持）。硬写全局反而是错的。

`--print`（`install` 上）是更轻的退化：只输出 JSON 片段，一个文件都不写。

### 3.4 省略 id → 编号选择器

```bash
qi store install skill     # 列出所有 skill，选
qi store install           # 列出全部 kind（分组），选
qi store install skill:4   # 直给 → 不弹，脚本友好
qi store uninstall         # 列出**已装**的（读账本，断网也能卸）
```

```
$ qi store install skill

hikqin · 4 个 skill · admin

  #   id         名称            大小    安全     状态
  1   skill:4    ppt-generator   67 KB   safe     未装
  2   skill:3    skill-creator   16 KB   safe     已装 v=ab12cd
  3   skill:9    pdf-reader      12 KB   medium   —

输入编号（1,3 或 2-4；/关键词 过滤；回车取消）:
```

- **输入的是"序号"，不是主键**。所以 `#` 列与完整 `id` 列**必须同时显示**：`#` 给你敲，
  `id` 给你复制到命令行。（`skill:4` 完全可能就是列表第 2 行。）
- 多选：`1,3` / `2-4` / 混用；去重；按输入顺序处理。
- 底部打 `共 N 个，用 /关键词 缩小`；**不做翻页 UI**（默认最多 30 条 + 就地过滤）。
- **不替代 `install` 的计划步骤**：选完照样打"写哪里 / 冲突 / 待补配置"再 `y/N`；
  加了 `-y` 就跳过第二次确认（选择本身就是明示）。
- 选中 `critical` / `high` 的条目要**额外确认一次**，`-y` 不跳过它。

| 情形 | 行为 |
| --- | --- |
| 目录为空 | `没找到可安装的 skill` → 4（不同于 `search` 的 0） |
| 非 tty（管道/CI） | 2 + `非交互终端，请直接给 id：qi store install skill:4` |
| 输入非法/越界 | 重问，3 次后放弃 → 2 |
| 回车 / Ctrl-C / EOF | 取消 → **130**，一个文件都不写 |

实现上 **CLI 自己写 `_choose()`**（`input()` + 编号解析，约 30 行）：零新依赖、Windows cmd
与 Git Bash 都能用、测试可直接喂 stdin。**不 import `qi_agent.prompt`** —— 它不在公开白名单里
（[../../docs/extensions.md](../../docs/extensions.md) §3 只有 `qi_agent.extensions`）。
TUI 里的 `/store install` 用 `ctx.ui.select`（公开面，箭头键与取消语义白送）。两处都调到同一个
`service.install(ids)`，选择器只是 id 的输入适配器。

## 4. 取货与落位

### 4.1 落位表

| kind | 上游产物 | 落盘目标 | 冲突默认 | `--force` | `--name` |
| --- | --- | --- | --- | --- | --- |
| skill | zip（顶层目录 = 技能名） | `<skills_root>/<名>/`（含 `SKILL.md`） | skip | 备份 `<名>.bak-<ts>` 再换 | ❌ 明确不支持（显示名来自 frontmatter，改名等于改内容） |
| mcp | 一个 server 定义 | `mcp.json` → `mcpServers.<名>` | skip | 先备份整个 `mcp.json` | ✅ 改条目 key |
| a2a | Agent Card | `a2a.json` → `a2aServers.<名>.cardUrl` | skip | 先备份整个 `a2a.json` | ✅ |

`<skills_root>`：`~/.qi/agent/skills`（global）/ `<cwd>/.qi/skills`（project）。
`--scope global|project`，`-l` = `--scope project`（对齐 `qi install -l`）。project scope
要求项目已信任，否则 2（`qi -a` 可信任本次运行）。

**"冲突"只在目标已存在且不是本账本装的时成立**；本账本装的同名条目走 `install` 幂等/升级，
不算冲突。

### 4.2 skill 的校验（不落"看不见的技能"）

解压 → ≤3 层下钻找 `SKILL.md`（对齐 qi-lab 的 `MAX_LOCATE_DEPTH`）→ 解析 frontmatter →
**没有 `description` 直接失败**（qi 的规则是"没有 description 的技能等于不存在"，装了也白装，
`resources_discover` 与提示词都看不见它）→ 校验名字与目录名一致 → 原子 rename 落盘。
zip 有成员数与解压体积上限（对齐 qi-lab 的 `MAX_ZIP_MEMBERS` / `MAX_ZIP_BYTES`）。

装/卸 skill 后：CLI 路径提示"下次启动生效"；**qi-lab 侧**在 `/api/store/install|uninstall`
里调 `WebState.refresh_skills()` —— 不调的话 `/api/skills` 与提示词读的还是旧缓存
（`runtime.top_skills` 按 cwd 缓存），症状是"商店里装好了、技能列表里没有"。

### 4.3 mcp：上游给什么就写什么

上游**永不下发 `auth_config`**（`/api/mcp/servers/{id}` 只给 `auth_type` 与明文 `headers`），所以按
"上游给没给"分两种情况处理，不做额外加密，也不做"看起来像密钥"的猜测：

**① 上游给了的 → 原样抄。** `headers` 上游就是明文返回，那就**原样写进本地 `mcp.json`**；
用户看到的东西和自己手写的一模一样，所见即所得。装完提示一句
`注意：mcp.json 里含明文凭据（上游 headers 原样抄），别提交进仓库` —— 提示而已，不阻止。

**② 上游没给的 → 留引用。** `auth_config`（bearer / apikey / basic 的真实值）不在任何响应
schema 里，只能写引用，否则就是伪造或空置：

| 上游 `auth_type` | 写进 `mcpServers.<名>` |
| --- | --- |
| `bearer` | `{"auth": "bearer", "bearerTokenEnv": "<NAME>_TOKEN"}` |
| `apikey` / `custom` | `{"headers": {"X-API-Key": "${<NAME>_API_KEY}"}}` |
| `basic` | `{"headers": {"Authorization": "Basic ${<NAME>_BASIC>"}}` |
| `none` | 原样（只有 `headers` 就够） |

选引用而不是空字符串，是因为 qi-mcp 对 `${VAR}` / `bearerTokenEnv` 有"**缺了就在发请求前报错**"
的保护（`extensions/qi-mcp/qi_mcp/client.py:146-178`）；空字符串的表现是静默 401。
装完打印一行 `export <NAME>_TOKEN=…`，并记进账本的 `needs_config[]`。

**值放哪（不是 qi-store 决定的）**：上面那些引用只是名字，值必须在**环境变量**里 ——
qi-mcp 在连接那一刻从 `os.environ` 读。

| 放法 | 怎么做 | 适用 |
| --- | --- | --- |
| shell 环境（建议） | `export X_TOKEN=…`（`~/.bashrc` / `~/.zshrc`）；Windows `setx X_TOKEN "…"` | 本机日常 |
| CI secret | 注入环境变量 | 流水线 |
| 项目 `.env` | ⚠️ **qi 不自动加载 `.env`** —— 需配合 `direnv` 或 `set -a; source .env; set +a` 后再启动 `qi` | 项目隔离 |
| 手填明文 | 直接写进 `mcp.json`（本地私用，别提交） | 嫌麻烦的人 |

`transport` → `type`：`streamable-http` 原样；`sse` 保留但提示 qi-mcp 未实现旧 SSE。

装完要**新会话**模型才用得上：mcp 的 server 清单在 `session_start` 时取（lazy 设计），
不是每轮重读。设置面的 MCP 节是每次请求重读文件，所以那里立刻可见。

### 4.4 a2a

`GET /a2a/agents` **无认证** → 所以 a2a 这个 kind **未登录也能装**。落成
`{"cardUrl": "<baseUrl>/a2a/agents/<id>/card.json"}`。装完提示：qi-a2a 下次启动会把固定
tenant 的条目注册成远端角色（`subagent` 的 `agent` 参数即可选到）。

## 5. 退出码与输出

| 码 | 含义 | 脚本该做什么 |
| --- | --- | --- |
| 0 | 成功（含 `no-op` / `skip`） | — |
| 1 | 网络 / 上游 5xx / 磁盘失败 | 重试 |
| 2 | 用法错误、非 tty 且未 `-y`、项目未信任 | 修命令 |
| 3 | 未登录 / 凭证过期且续期失败 | `qi store login` |
| 4 | 条目不存在 / id 歧义 | 改 id（错误里已列候选） |
| 5 | 冲突（已存在且未 `--force`） | `--force` 或 `uninstall` |
| 130 | 用户取消 | — |

- 人读用 rich 表格；`--json` 给脚本（`search` / `info` / `list`）；错误一律 **stderr**。
- `search` 无结果 → 0 + "没找到"（不是错误）。目录为空时的**选择器**才用 4。

### 5.1 状态矩阵（别让用户猜）

| 状态 | `search` | `info` | `install skill/mcp` | `install a2a` | `list` | `download` |
| --- | --- | --- | --- | --- | --- | --- |
| 未登录 | 只列 a2a + 表尾 `⚠ 未登录，skill/mcp 未列出` | a2a 可，其它提示 | 3 | ✅ | ✅ | 仅 a2a |
| 凭证过期且续期失败 | 同上 | 同上 | 3 | ✅ | ✅ | 仅 a2a |
| 离线 | 用缓存（TTL 5 min；`--offline` 强制） | 同上 | 1 | — | ✅ | 缓存可 |
| 项目未信任 + `-l` | — | — | 2 + 提示 `qi -a` | 同 | — | — |

未登录时 `search` **不报错退出**（a2a 是公开的），但一定在表尾说明"为什么少了两个 kind"——
"商店看起来是空的"是最坏的那种误解。

## 6. 计划先行（plan）

多件一起算，**永远先给人看**：

```
$ qi store install skill:4 mcp:3

hikqin · http://10.1.43.6:6091

  skill:4   ppt-generator   安全(safe/已扫描)   12 个文件 67 KB
            → ~/.qi/agent/skills/ppt-generator/
  mcp:3     ppt-tools       需补配置            transport=streamable-http
            → ~/.qi/agent/mcp.json  mcpServers.ppt-tools  (auth=bearer → bearerTokenEnv=PPT_TOOLS_TOKEN)

冲突 无 · 预计写入 13 个文件
装完待办 1 项：export PPT_TOOLS_TOKEN=…

继续? [y/N]
```

- `--dry-run` 打同一份计划后停；`--json` 把计划输出成 JSON（给 qi-lab 用）。
- **非 tty 且没 `-y` → 2**（不静默装东西）。
- 落盘：先写 `<target>.tmp-<pid>` / 临时目录 → 校验（`SKILL.md` / JSON 可解析）→ **原子 rename**
  → 写账本。失败不留半成品。

## 7. 安全模型

商店内容是**别人写的提示词和配置**，所以：

1. **写入前必须人确认**（§6）；`-y` 是明示，不是默认。
2. **`scan_status` 透出**并参与默认策略：`critical` / `high` 需要**额外**明示
   （`--allow-unsafe` 或当场确认），`-y` **不**跳过它。
3. **项目级安装过信任门控**（未信任项目只能装 global）。
4. **不执行任何东西**：只写配置文件。extension kind 即使将来做，也走显式 `qi install`。
5. **保密边界画在"上游给没给"上**：上游给了的（明文 `headers`）原样抄，只提示一句；
   上游没给的（`auth_config`）写环境变量引用，不伪造、不空置。

## 8. 代码结构

```
extensions/qi-store/
  pyproject.toml            # name=qi-store；entry-points: qi.extensions → store = "qi_store:register"
  extension.py              # 目录通道入口(开发用)
  README.md                 # ← 本文
  docs/hikqin-api.md        # 上游契约 + 陷阱(fixtures 的依据)
  docs/config-files.md      # store.json（两层）/ installed.json（账本）
  qi_store/
    __init__.py             # register(api)：只注册 CLI 子命令
    paths.py                # 文件位置(QI_AGENT_HOME)
    errors.py               # 退出码的唯一来源
    config.py               # store.json 两层（凭证只用户级）
    client.py               # 登录/续期、只读白名单、逐端点拆包
    ops.py                  # cli 与 service **共用**的语义(建客户端 / 声明解析 / 账本↔目录比对)
    catalog.py              # 统一 StoreItem + 缓存(TTL/离线) + id 解析
    kinds/{skill,mcp,a2a}.py# fetch / plan / (download)
    kinds/base.py           # 作用域根、命名约定、zip 定位、frontmatter
    actions.py              # 落盘动作(Write/ExtractZip/PatchJson) + Planned
    planner.py              # 计划 + 冲突判定 + 需要补的配置
    installer.py            # 原子落盘 / 备份 / 卸载只删自己写的
    ledger.py               # installed.json 读写
    cli.py                  # argparse 子命令 + 编号选择器
    service.py              # ★ qi-lab 消费的稳定面(无 HTTP / 无 UI / 无 asyncio)
  tests/                    # 4 个测试文件 + 8 份 fixtures
```

**路径自己推**（`QI_AGENT_HOME` / `QI_CONFIG_DIR`，不 import `qi_agent.paths` —— 它不在
白名单）。⚠️ 两个环境变量都要认：只认前者时，设了 `QI_CONFIG_DIR` 的环境（qi 自己的测试
隔离、多实例部署）会被我们写进**真实的** `~/.qi/agent`。

## 9. 与 qi-lab 的关系（P3 ✅）

`add_route` / `add_static` 在 qi 里**明确未实现**（[../../docs/extensions.md](../../docs/extensions.md) §11），
所以 qi-lab 不能"挂" HTTP 路由进来。实际做法是**进程内懒 import**（与 `/api/mcp` 消费
`qi_mcp.config` 同一套）：

| 位置 | 内容 |
| --- | --- |
| `qi_store/service.py` | 稳定面：`status` / `search` / `installed` / `plan` / `install` / `uninstall` / `check`。**同步阻塞**，不 import asyncio |
| `qi_lab/app.py` | `/api/store/*` 七个端点；每个都用 `asyncio.to_thread` 包一层，`StoreError.exit_code` → HTTP 状态码（2→400、3→401、4→404、5→409、其它→502） |
| `qi_lab/schemas.py` | `Store*` 模型（camelCase，只出展示需要的字段） |
| `qi_lab/ui/src/components/StorePanel.tsx` | 设置面「商店」一节：搜索 / 已装 / 计划确认层 / 待补 env |
| `qi_lab/docs/api/store.md` | 接口文档（`test_lab_docs_api.py` 会双向对账） |
| `/api/meta` 的 `store_write` | **真去 import 一次** qi-store 的结论；没装 → 那一节只读 |

安全口径：

1. **浏览器不直连 hikqin**：请求全部经 qi-lab 后端代理，不依赖上游 CORS；
2. **令牌不进浏览器**：`/api/store/login` 收账号密码 → 交给上游 → 只回 `StoreStatus`。
   磁盘上是令牌（用户级 `store.json`，0600），浏览器里什么都没有；
3. 登录/退出是写操作 → 与其它写端点同一条 `X-Qi-Confirm` 闸；
4. 面板按**四类标签**摆：Agent / 技能 / MCP / A2A —— 每个标签只放它那一类（能装 + 已装）。
   Agent 标签明说"暂不提供"及原因（见 §12），不摆一个永远空着的列表。

## 9.1 账本 ≠ 事实:每次读都复核磁盘

账本只记"商店装过什么"。**删东西的路不止一条** —— 设置面 A2A/MCP 两节的删除、手改
`a2a.json`、`rm -rf` 一个技能目录,都不经过商店。所以 `verify.presence(record)` 把
"还在不在"落在磁盘上:

| 场景 | 判据 | 界面/输出的样子 |
| --- | --- | --- |
| 文件类(skill) | `files[]` 里的文件还有没有 | `本地已被移除(文件都不在了)` |
| 配置键类(mcp/a2a) | 文件里那个`section.key`还在不在 | `本地已被移除(a2aServers.技能调试 已不在 a2a.json)` |

三处消费它:`installed()` 给 `present/missingReason`;`search()` **按磁盘**算 `installed`
(盘上没了就回到「安装」);`check()` 报 `status="missing"`(优先于"上游有没有")。
清理就是对该记录跑一次 `uninstall` —— 它只删还存在的文件/键,然后划掉这笔账。

## 10. 分期

| 期 | 范围 | 验收 |
| --- | --- | --- |
| **P0** ✅ | fixtures + `client.py`（login/refresh/拆包/白名单）+ `login`/`logout`/`whoami`/`sources` | 三个 kind 的响应样本落进 `tests/fixtures/`；拆包与字段名有测试钉住（**已过，47 条**） |
| **P1** ✅ | `search`/`info`/`install`/`download`/`list`/`uninstall`，skill + mcp + a2a，编号选择器 | `qi store install ppt-generator` → 技能落盘且 **qi 的 loader 真能扫到**；`uninstall` 干净（**已过，75 条**） |
| **P2** ✅ | `check` / `upgrade` / `sync` + `wanted[]` 声明层 | 上游更新后 `check` 报漂移，`upgrade` 保持原落点并备份旧版，`sync` 只补不删（**已过，90 条**） |
| **P3** ✅ | qi-lab：`/api/store/*`（7 条）+ 设置面「商店」一节 | `qi lab` 里搜到 → 看计划 → 装 → 技能面板立刻出现（**已过：qi-store 91 + qi-lab 商店 API 13**） |
| **P4** | `agent` kind（等 hikqin 蓝图与 qi 角色目录对齐）+ `extension` kind（上游已有 PEP 503 索引） | — |

## 11. 落地记录（实测，不是计划）

### P3（qi-lab 面板）

| 项 | 结果 |
| --- | --- |
| 后端 | `/api/store/{status,search,installed,plan,install,uninstall,check}`；写操作要 `X-Qi-Confirm`；没有 login |
| 降级 | 没装 qi-store → 200 + `available:false`；`/api/meta` 的 `store_write` 是真 import 一次（连 `_store_available()` 都用它，不写死 True） |
| 前端 | 设置面新增「商店」一节：搜索、安装/更新、已装表、计划确认层、`needsConfig` 待办、`missing[]` 警告 |
| 两条安全口径 | 浏览器不直连 hikqin；**没有 `/api/store/login`**（密码不经手浏览器） |
| 踩到的坑 | qi_store 的路径起初只认 `QI_AGENT_HOME`，于是 qi-lab 测试用 `QI_CONFIG_DIR` 隔离时被我们写进了**真实的** `~/.qi/agent`（store.json / 账本 / 技能）。现在两个变量都认，并且有一条测试钉住 |

### P2（生命周期）

| 项 | 结果 |
| --- | --- |
| `check` | 线上装 `skill:3` 后报“最新”；把上游 `file_size` 改 1 字节就报“可升级”；上游删掉就报“上游不可见” |
| `upgrade` | 保持原 target、旧版备份成 `ppt-generator.bak-*`、账本版本跟着变；`--dry-run` 一个字节不写 |
| `sync` | 按 `wanted[]` 装 `skill:4` + `a2a:…`，**已有的不重装、声明外的不卸载** |
| 踩到的坑 | `scope_root(scope, None)` 会在真实调用里回到 `Path.cwd()` —— 于是 `install -l` 把项目级技能装到了你**当前所在目录**而不是 `run(cwd=…)` 指的项目。现在 `cwd` 由 `run()` 统一挂到 args 上（一条测试钉住） |

### P1（取货）

| 项 | 结果 |
| --- | --- |
| `search` | 线上列出 3 个 skill + 2 张 a2a 名片（实例上 mcp server 为空，这一类空着是**真实**的） |
| `install skill:3` | 落 `~/.qi/agent/skills/skill-creator/`，6 个文件；`qi_agent.loader.scan_skill_root` **真的扫到了**（名字 + description 都读出来了） |
| `install a2a:…` | 落 `a2a.json` 的 `{baseUrl, tenant}`（卡片自己给的两半，不猜 agent id） |
| `uninstall` | skill 删到空目录全清（包括 `scripts/` 这类子目录）；a2a 删键后文件空了就**整个删掉**，不留 `{}` |
| `install skill`（只给 kind） | 进编号选择器；非 tty 时退出码 2 并**直接给出可用 id 示例**（不会糊里糊涂装了 `skill-creator`） |
| `--dry-run` / `--offline` | dry-run 一个文件不写；offline 只能 `search`/`info`/`list`，`install` 明确拒绝 |

### P0（认证）

对着部署实例 `http://10.1.43.6:6091` 跑通的东西，以及顺手验出来的两条：

| 项 | 结果 |
| --- | --- |
| `login` → `whoami` → `sources` → `logout` | 全通（写入 `~/.qi/agent/store.json`，`defaultStore` 首次登录自动设） |
| 自动续期 | 用"把时钟拨到 4 小时后"强制过期 → `/auth/refresh` 换新 access → 原子写回 → 请求成功 |
| **refresh 会换发 refresh token** | 上游回 `{access_token, refresh_token, token_type}` —— 写回时**必须覆盖** refresh，只更新 access 会让凭证 7 天后突然整个失效 |
| 401 有两种文案 | 不带令牌 → `Not authenticated`；令牌无效 → `无效的认证凭据`。本地都归为退出码 3，但日志保留原文 |
| `exp` 字段是冗余但有用 | `_ensure_token` 判断过期用的是 **JWT 里的 `exp`**（真相），不是 `store.json` 里那个 `exp`（只用于展示） |

`tests/fixtures/` 里 7 份真实响应 + 1 份**合成**的 mcp 样本（线上 mcp servers 为空，文件内
第一行 `_note` 写明了，别当真实数据用）。

两条工程约定（踩过的）：

- **测试文件名要带 `qi_store_` 前缀**。仓库的测试目录没有 `__init__.py`，两个 `test_config.py`
  会在收集期撞车（`import file mismatch`）—— 其它扩展也是这个前缀习惯。
- **`conftest.py` 必须把 `QI_AGENT_HOME` 指到 tmp**。否则测试会往开发机真实的
  `~/.qi/agent/store.json` 写凭证，而那个文件里现在可能真的存着令牌。

## 12. 已知限制与**刻意的偏差**

| 项 | 契约 |
| --- | --- |
| `agent` kind | 未实现。理由：hikqin 模板蓝图（`format_version: 1`，skills 内联 base64、`sub_agents` 快照）与 qi 的角色目录（`agent.md` + 私有 `mcp.json` + 私有 `skills/`）**格式没对齐**；且蓝图里的 `model`/`api_key` 是剥离状态，装了也跑不起来 |
| **project scope 不走 `trust.json`** | 与设计稿不同：扩展不去复制宿主的信任判定（那是内部语义），改成**每次安装都要人点头**（`-y` 或交互确认），非 tty 且没 `-y` 就拒绝。比“读一个我们并不拥有的状态”更诚实 |
| `version` | 上游没有 semver → 用**内容哈希**当版本（`sig:` 前缀提醒它不是上游给的版本号）。真正的 `version` 字段是 hikqin 侧的事 |
| `install` 会 +1 下载计数 | skill 的取包端点就是 `GET /api/skills/{id}/download`，上游会 `download_count++`。这是我们对上游唯一的数据影响，也是“装了多少”的唯一信号 |
| `download` 对 mcp/a2a | 产物是 `mcp-<key>.json` / `a2a-<key>.json`（一段可直接粘贴的 JSON），**不挑落点** —— 它可能是某个角色私有的 `mcp.json` |
| 卸载会删空文件 | `mcp.json` / `a2a.json` 被我们删空（`{}`）时直接删文件；但**只要里面还有别人的键就绝不删** |
| `sync` 不卸载 | 声明层是“应该有”。多装的条目只会在 `check` 里被列出（不自动删）：删东西该由人显式 `uninstall` |
| 上游无吊销接口 | `logout` 只删本地；要立即失效请改密码 |
| 上游响应包装不统一 | `login` / `skills` / `mcp` 是裸体，`agent-templates` / `a2a-admin` 是 `{code,message,data}`；逐端点拆包，见 [docs/hikqin-api.md](docs/hikqin-api.md) |
| `sse` 传输 | qi-mcp 认得出但连接时明确报未实现 → 安装只写配置，不假装能连 |

---

## 附：面板里**没有**的东西（刻意的）

| 没有 | 为什么 |
| --- | --- |
| "装全部"按钮 | 安装会改提示词与配置，一次一件、先看计划 |
| 自动升级 | `check` 只报告。升级由人点（`qi store upgrade` / 面板上的「更新」） |
| 卸载"多出来的" | 声明层（`wanted[]`）只管"应该有"，删东西永远是显式动作 |
