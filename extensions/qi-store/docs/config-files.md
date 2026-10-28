# qi-store 的文件：配置（含账号）/ 账本 / 缓存

> 只有**两份** qi-store 自己的文件 + 一个缓存目录。刻意少，因为多了没人记得住哪份能提交。
>
> 路径约定与 qi 其它扩展一致（`~/.qi/agent/` ↔ `<cwd>/.qi/`），见
> [configuration.md](../../../docs/configuration.md) 与 `qi_agent/paths.py`。

| 文件 | 层 | 含凭证 | 能进 git |
| --- | --- | --- | --- |
| `store.json` | 用户级 `~/.qi/agent/` | **能**（`stores.<源>.auth`） | ❌ 别提交 |
| `store.json` | 项目级 `<cwd>/.qi/` | ❌ **不允许**（出现即忽略 + 警告） | ✅ 该提交 |
| `installed.json` | 只在用户级 `~/.qi/agent/store/` | ❌ | ❌ 本机账本 |
| `cache/` | 同上 | ❌ | ❌ |

## 1. `store.json`（用户级）—— 配置 + 账号

```jsonc
{
  "stores": {
    "hikqin": {
      "baseUrl": "http://10.1.43.6:6091",
      "apiPrefix": "/api",
      "priority": 10,
      "auth": {                                  // ← 账号就在这里，login 写、logout 删
        "login": "admin",
        "role": "admin",
        "access": "eyJ…",
        "refresh": "eyJ…",
        "exp": 1792562546
      }
    }
  },
  "defaultStore": "hikqin",
  "install": { "scope": "global", "conflict": "skip", "limit": 30 },
  "wanted": []
}
```

- **账号是 qi-store 自己的配置**，所以住在 `store.json`，**不借**宿主的
  `~/.qi/agent/auth.json`（那是模型 provider 的密钥库，`paths.AUTH_FILE_NAME`，同名不同物）。
- 因为这份文件可能含 `auth`，**写入时 `chmod 0600`**。Windows 上 ACL 设不了就在 `login`
  输出里说明"该目录应仅本人可读"。
- `baseUrl` 是**后端**地址（`…:6091`），不是前端 `…:6090`。
- 文件坏了 → 报错并**带上路径**，不静默重建（照 qi-mcp 的 `ConfigError` 口径：
  "现场只看到一个 JSON 报错是没法修的"）。

## 2. `store.json`（项目级）—— 只放能共享的东西

```jsonc
// <cwd>/.qi/store.json —— 这个会进仓库
{
  "stores": { "hikqin": { "baseUrl": "http://10.1.43.6:6091" } },
  "defaultStore": "hikqin",
  "install": { "scope": "project" },
  "wanted": ["skill:ppt-generator", "mcp:ppt-tools"]
}
```

深合并且与 qi-a2a 同语义：嵌套对象递归合并、数组整体替换、`null` 删除键。

**凭证字段的作用域规则（硬规则，要有测试）**：

| 字段 | 用户级 | 项目级 |
| --- | --- | --- |
| `stores.<名>.baseUrl` / `apiPrefix` / `priority` / `disabled` | ✅ | ✅（近者胜） |
| `defaultStore` / `install.*` / `wanted[]` | ✅ | ✅（近者胜） |
| `stores.<名>.auth` | ✅ 唯一来源 | ❌ **忽略 + 一条警告**（"凭证只能放在用户级配置"） |

项目级**不合并** `auth`、也**不允许**它作为回退 —— 否则"把 token 提交进仓库"就变成一个
可以无意触发的事故，而不是一个能防御的错误。

## 3. `installed.json` —— 实装账本（本机，不提交）

`~/.qi/agent/store/installed.json`

```jsonc
{
  "items": [
    {
      "itemId": "skill:4",
      "store": "hikqin",
      "kind": "skill",
      "name": "ppt-generator",
      "version": "sig:ab12cd34ef56",          // 内容指纹 = 上游没有 semver 时的版本
      "scope": "global",                      // global | project
      "target": "C:/Users/x/.qi/agent/skills/ppt-generator",
      "files": ["SKILL.md", "scripts/gen.py"], // 卸载时按这个删
      "patches": [],                           // JSON 键类(mcp / a2a)才用
      "needs_config": [],                      // mcp 会填 ["PPT_TOOLS_TOKEN"]
      "installed_at": "2026-10-20T10:00:00Z",
      "upstream_updated_at": "2026-10-19T01:32:55"
    },
    {
      "itemId": "mcp:3", "kind": "mcp", "name": "ppt-tools",
      "target": "C:/Users/x/.qi/agent/mcp.json",
      "files": [],                             // **不记 json 文件**:否则卸载会把别人一起删掉
      "patches": [{"file": "…/mcp.json", "section": "mcpServers", "key": "ppt-tools"}],
      "needs_config": ["PPT_TOOLS_TOKEN"]
    }
  ]
}
```

为什么这几列是必须的：

| 列 | 用途 |
| --- | --- |
| `itemId` | **身份**。上游下架是软删除（行保留、同名重传复用该行），所以 id 稳定、名字会复用 |
| `version` | **内容**。`check` 靠它比上游哈希；同 id 换了内容才看得出来 |
| `target` + `files` / `patches` | `uninstall` 只删自己写过的：目录类按 `files[]` 删并清空目录；JSON 类只删 `patches[]` 里那个键，**文件里别人的内容原样活着** |
| `scope` | 同一 itemId 可以在 global 与 project 各装一份（两份独立账目） |
| `needs_config` | 装完的待办项，`list` 与 `qi-lab` 面板都要能显示 |

- **账本只在用户级**（一份），每条带 `scope` 与绝对 `target` → 项目级安装也记得住，
  不会因为换 cwd 就"忘了自己装过什么"。
- `uninstall` 断网可用（只读这份文件）—— 硬要求：网断了也要能卸自己装的东西。

## 4. `wanted[]` —— 声明层（P2 已落地）

`qi store sync` 按它对账安装，`qi store check` 把漂移报出来。三类漂移：

- 声明了没装 → `sync` 装
- 装了没声明 → 只在 `check` 里提示（**不替用户卸载**：删东西该由人显式做）
- 装了但上游更新了 → 提示 `qi store upgrade`

每项可以是字符串，也容忍 `{"id"/"name"/"spec"}` 结构体（照 qi 的 `settings.packages` 口径：
写法宽一点，别让人因为多包一层而失败）。

## 5. 缓存

`~/.qi/agent/store/cache/<store>.json`

- `search` / `info` 的 catalog 快照，TTL 5 分钟；`--offline` 直接用（不看 TTL）。
- `logout --all` 一并删。
- 缓存里**不含凭证**，也不含 skill 正文（只有元数据 + 文件树）。

## 6. 环境变量

| 变量 | 作用 |
| --- | --- |
| `QI_STORE_TOKEN` | 覆盖 `store.json` 里的 access token（CI；不落盘） |
| `QI_STORE_URL` | 覆盖默认 `baseUrl`（一次性指向别的实例，不写文件） |
| `QI_AGENT_HOME` | 覆盖全局 agent 目录（默认 `~/.qi/agent`）—— qi 自己的约定，跟着走 |

`logout` 只清 `auth` 段（幂等）；`logout --all` 连缓存一起清。
上游**没有吊销接口**，所以命令输出必须写明：要立即失效请改密码。
