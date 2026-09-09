# CLI 命令面

> 状态:设计讨论中。原则:**参数尽量与 pi 保持一致**;差异仅来自多 agent / pip 生态 / 无密钥存储。
> 相关文档:[agent-config.md](agent-config.md)、[plugins.md](plugins.md)、[web.md](web.md)(v2)。

## 0. 速查(按用途分类)

```
── 运行 ────────────────────────────────
qi                              # 进 TUI(交互)
qi -p "<问题>"                    # 无头一次执行(auto 分派)
qi -p --agent <name> "<问题>"     # 指定 agent(manual)

── 会话 ────────────────────────────────
qi -c | -r | --session <id> | --fork <id> | -n <名> | --no-session
qi sessions list | show <id> | rm <id>
qi --export <file>

── Agent ────────────────────────────────
qi agents list | show <name>     # 查看装载的 agent / 单 agent 诊断
qi agents import <source> [-l] [--force]   # 导入(拷贝+校验,见 agent-config.md §10)
qi agents export <name> [-o <path>]        # 导出目录/包,产物可直接 import

── 插件 ────────────────────────────────
qi install <source> [-l] | remove|uninstall <name> [-l] | list [-l] | update
qi -p --plugin <path>            # 本次运行临时试用插件

── 模型 / 诊断 ─────────────────────────
qi models list | qi doctor

── 安全 / 信任 ─────────────────────────
qi -a | -na                      # 信任/不信任项目 .qi

── 通用 ────────────────────────────────
qi -h | -v | --verbose | --offline | -t <tools> | -xt <tools> | -nt | -nbt
```

## 1. 启动与运行

| 命令 | 说明 | pi 对齐 |
|---|---|---|
| `qi [options] [--] [@files...] [messages...]` | 无 `-p` = 进 TUI;`-p` = 无头执行后退出 | ✅ 形态同 pi |
| `-p, --print` | 无头一次执行;**auto 分派**;给 `--agent` 即 manual | ✅ |
| `--agent <name>` | 指定 agent(长参;不用 `-a`,pi 的 `-a`=approve) | qi 新增 |
| `--mode <text\|json>` | 输出格式(`rpc` 二期) | ✅ |
| `-t <tools>` / `-xt <tools>` | 工具 allowlist / denylist 临时覆盖(tools 三态) | ✅ |
| `-nt` / `-nbt` | 禁用全部工具 / 保留插件工具 | ✅ |
| `--plugin <path>` | 本次运行临时加载插件 | 🟡 pi `-e` |

## 2. 会话

| 命令 | 说明 | pi 对齐 |
|---|---|---|
| `-c, --continue` | 续上次会话 | ✅ |
| `-r, --resume` | 选择会话恢复 | ✅ |
| `--session <path\|id>` / `--session-id <id>` | 指定会话 | ✅ |
| `--fork <id>` | fork 出新会话 | ✅ |
| `--session-dir <dir>` | 会话目录 | ✅ |
| `--no-session` / `-n, --name` | 临时会话 / 显示名 | ✅ |
| `qi sessions list \| show <id> \| rm <id>` | 列表 / 查看 / 删除 | qi 扩展 |
| `--export <file>` | 会话导出 HTML | ✅ |

`-c/-r/--session` 管"接着跑哪段",`qi sessions` 管"历史浏览/删除",互补。

## 3. Agent 与分派

| 命令 | 说明 |
|---|---|
| `--agent <name>` | 指定 agent(manual);省略 = auto 分派 |
| `qi agents list` | 装载的 agent:来源/描述/工具/绑定 |
| `qi agents show <name>` | 单 agent 解析诊断(skills/mcp/data_sources 是否生效) |
| `qi agents import <source> [-l] [--force]` | 导入 agent(拷贝 + 装载校验器预检 + 凭证扫描);详见 agent-config.md §10 |
| `qi agents export <name> [-o <path>]` | 导出 agent 目录/压缩包(产物可直接 import) |

## 4. 插件(对齐 pi 命名与 `-l`,底层 pip 生态)

| 命令 | 说明 | pi 对齐 |
|---|---|---|
| `qi install <source> [-l]` | 本地目录 → 插件目录通道;`pip:<pkg>` → 转 pip | ✅ `pi install` |
| `qi remove\|uninstall <name> [-l]` | 移除本地目录插件 | ✅ |
| `qi list [-l]` | 列出已安装插件 | ✅ |
| `qi update [source\|self]` | 插件 / 本体更新 | ✅ |

`-l`:写入项目 `.qi/`(而非用户 `~/.qi/`),同 pi 的 project 语义。

## 5. 模型与诊断

| 命令 | 说明 | pi 对齐 |
|---|---|---|
| `qi models list` | 列 `[models.*]` 命名模型 | 🟡 pi `--list-models` |
| `qi doctor` | 诊断:env 密钥 / 配置 / 装载错误 | qi 新增(替代 pi auth) |

## 6. 安全与信任

| 命令 | 说明 | pi 对齐 |
|---|---|---|
| `-a, --approve` | 信任项目 `.qi`(本次运行) | ✅ |
| `-na, --no-approve` | 忽略项目 `.qi` | ✅ |

## 7. 通用

`-h/--help`、`-v/--version`、`--verbose`、`--offline`、`--`(结束参数解析)。

## 8. 二期

- `qi web`:HTTP 宿主(web.md)
- `--mode rpc`:headless JSONL-RPC,给外部客户端(IDE)

## 9. 明确不保留(理由落档)

| pi 命令/参数 | 不保留原因 |
|---|---|
| `pi auth` | 无密钥存储,全部 env 引用;诊断走 `qi doctor` |
| `pi config`(TUI 面板) | 配置即文件,`agents/models show` 可查 |
| theme / prompt-template / skill 加载开关 | 概念不存在;内容跟 agent 走 |
| `--provider / --api-key / --thinking / --models` | 模型在 `[models.*]` 配置、密钥在 env |
