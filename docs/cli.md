# 命令行

qi 的内置命令与选项。装了什么扩展,顶层就还会多出它们注册的旗标与子命令 —— 以 `qi --help` 为准。

```sh
qi [options] [--] [@files...] [messages...]
qi install <来源> [options]
qi remove <来源> [options]
qi uninstall <来源> [options]
qi update [来源|self] [options]
qi list
qi doctor
qi config [options]
qi init [options]
qi auth <login|logout|list|print-api-key|print-bearer-token|check> [options]
```

## 调用与输出

```sh
qi                                  # 交互界面(默认)
qi "分析这个仓库"                    # 进界面,并把这句话作为首条消息发出
qi -p "把 CHANGELOG 最近 10 条汇总"   # 无头:只把最终答案写到 stdout
git diff | qi -p "审查这个改动"       # 管道进来的内容会加到第一条消息前面
qi --mode json "跑一遍测试" > e.jsonl # 事件流
qi --export out.html                # 导出会话后退出
```

| 输入 | 行为 |
| --- | --- |
| `message` | 作为第一条消息 |
| `@path` | 把文本文件内容带进第一条消息 |
| 管道 stdin | 内容**前置**到第一条消息 |
| `--` | 停止选项解析(消息本身以 `-` 开头时用) |

| 选项 | 行为 |
| --- | --- |
| `-p`, `--print` | 跑完退出,**只把最终答案写 stdout** |
| `--mode text` | 文本输出;**stdin/stdout 都是终端时仍进界面**(要一次执行就配 `-p`) |
| `--mode json` | 输出事件流(一行一个 JSON,**隐含无头**)→ [json.md](json.md) |
| `--mode rpc` | **未实现**:传了会明确报错(见 [PLAN.md](../design/PLAN.md) 的 v2 清单) |
| `--export <file>` | 导出会话:`.html` → 自包含 HTML,其余 → 原始 JSONL |

**选项写在消息之前**(`qi -nt "问题"`,不是 `qi "问题" -nt`):顶层选项在遇到消息后不再解析 —— 写反了会报错并提示怎么改。

### 输出与中断契约

| 流 | 内容 |
| --- | --- |
| stdout | **仅答案正文**(`--mode json` 时为事件 JSON 行) |
| stderr | 默认空;`--verbose` 时为工具进度(`⚙ ls {"path": "."}` / `↳ …`) |

所以 `qi -p "问题" > out.txt` 拿到的就是纯答案,脚本无需过滤;TUI 不受此限,仍实时显示工具调用。

轮次**不设上限**(交互式与无头都是):退出靠模型不再调工具、中止,或嵌入方的 `stop_after` 谓词(qi 自己从不传)。
中断退出码 `128+n`:`SIGTERM` / `SIGHUP` → 回收在跑的子进程组 + `exit(143/129)`;`SIGINT` → asyncio 取消 →
`run_shell` 的 finally 回收 + `exit(130)`,不打 traceback。两条路径的半截回答都会落盘。

## 模型

```sh
qi --model sonnet:high
```

选择与凭证见 [models.md](models.md)、[providers.md](providers.md)。

- `--provider <名>`:把 `--model` 的查找限制在一家。
- `--model <模式>`:写 `provider/模型`,可带 `:<思考级别>` 后缀(**只认已知级别**,所以 `openrouter/x:free` 不会被误切);只给 `--provider` 时用它 `models.json` 里的第一个模型。
- `--api-key <键>`:**按次**覆盖密钥,不落盘(优先于 auth store / 环境变量 / `models.json`)。
- `--thinking <级别>`:`off` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max`(非法值退出码 2;不传用 settings 的 `defaultThinkingLevel`,没配就 `medium`)。
- `--models <清单>`:本次运行 `ctrl+p` 的轮换清单(逗号分隔),**不回写** settings。
- `--list-models [搜索词]`:列出模型后退出(每行带 `credential` 列;缺密钥的也列,这是诊断面)。

## 会话

```sh
qi -c
```

细节见 [sessions.md](sessions.md)。

- `-c`, `--continue`:接着最近一条(按**文件修改时间**,不是 header 时间)。
- `-r`, `--resume`:打开会话选择器(**需要 TTY**;与 TUI 的 `/resume` 同一条路)。
- `--session <路径|id>`:先当文件路径,再当 id / 文件名前缀。
- `--session-id <id>`:精确的项目会话 id,不存在则建。
- `--fork <路径|id>`:从已有会话分叉出新会话(复制当前分支)。
- `--session-dir <dir>`:会话目录(优先于 `settings.sessionDir`)。
- `--no-session`:内存会话,**不落盘**。
- `-n`, `--name <名>`:会话显示名(命名即落盘)。

## 工具

```sh
qi --tools read,grep,find,ls -p "审查这个项目"
```

内置工具清单与三态语义见 [how-qi-works.md](how-qi-works.md);默认档在 `settings.defaultTools`。

- `-t`, `--tools <清单>`:**严格白名单** —— 最终工具集就是这些(不再叠加默认集)。
- `-xt`, `--exclude-tools <清单>`:从最终集合里排除(在其它选择之后生效)。
- `-nbt`, `--no-builtin-tools`:禁用内置工具,**保留扩展装的**。
- `-nt`, `--no-tools`:禁用全部工具。

`-t` 与 `-nt` / `-nbt` **互斥**(同给报错退出码 2);未注册的名字会报一条提示,不静默丢。

## 资源

- `--skill <路径>`:额外加载一个技能(可重复,叠加;优先级高于所有自动发现层)。
- `--no-skills` / `-ns`:关掉技能的**自动发现**(`--skill` 仍生效)。
- `-e`, `--extension <目录>`:本次运行临时加载一个扩展目录(仅本进程,`scope=temporary`;TUI 与无头都生效)。
- `--ext name=value`:给扩展声明过的**旗标**传值(与 `-e` 不是一回事)。见 [extensions.md](extensions.md)。
- `--no-extensions` / `-ne`:关掉扩展**发现**,`-e` 显式给的仍生效。

## 提示词与上下文

- `--system-prompt "<文本\|文件>"`:**整体替换**基座(与 `.qi/SYSTEM.md` 同语义,只本次运行)。
- `--append-system-prompt "<文本\|文件>"`:追加到每回合提示词末尾(可重复,空行连接)。
- `--no-context-files` / `-nc`:不注入 `AGENTS.md` / `CLAUDE.md`。

语义与副作用见 [configuration.md](configuration.md)。

## 界面

- `--tui-mode <regular|fullscreen>`:渲染模式(非法值退出码 2),压过 `settings.tuiMode`。
  `fullscreen`(默认)= 备用屏、qi 拥有视口;`regular` = inline、滚动交给终端。见 [usage.md](usage.md)。

## 包命令

```sh
qi install <来源> [-l]      # 调 pip + 写进 settings.packages;本地目录只登记(不调 pip)
qi remove <来源> [-l]       # 只从声明里移除(不卸包),并把 pip uninstall 命令打出来
qi uninstall <来源> [-l]    # 同 remove
qi update [来源|self] [--self|--extensions|--all|--extension <来源>|--force]
qi list                     # 列出已装扩展,并与声明双向比对
qi doctor                   # 诊断(含「声明了没装」的可复制装法)
```

`qi update` 默认只更 qi 自己;`--models` 在 qi **无意义**(模型全在 `models.json` 里自己维护,它只说明这一点然后退出)。
`qi install` 每次都先把要跑的 pip 命令打出来 —— 目标环境(自家 venv / `uv tool` / 只读解释器)的行为真的不同,
声明与比对的意义见 [packages.md](packages.md)。

## 设置与初始化

```sh
qi config [-l] [--get K] [--set K=V] [--unset K] [--json]
qi init [-y] [-l] [--provider … --model …]
qi init --list-presets | --preset <名>[,<名>] | --refresh <名>[,<名>] | --refresh-all
```

- `qi config` 不带旗标:TTY 下开**资源启停面板**(space 勾选 · `ctrl+s` 保存 · `escape` 取消;`-l` 切作用域),
  非 TTY 打一张资源表 + 设置总览。`--set` 的值先按 JSON 解析、失败则当字符串;`--get` / `--unset` 支持点号路径。
  字段清单见 [settings.md](settings.md)。
- 资源的"关"写的是**声明**:目录扩展 → `settings.extensions[]` 加 `-<路径>`;pip 包 → `packages` 里那条换成
  `{"source": …, "extensions": []}`。所以状态被记住,也能表达"项目关掉、全局还开着"。
- `qi init` 引导默认模型,详见 [models.md](models.md)。

## 凭证命令

```sh
qi auth login <provider>            # 输入可见地写入 auth.json(0600);TUI 里的 /login 是遮罩输入
qi auth logout <provider>           # 删除该 provider 的凭证(= qi auth rm)
qi auth list                        # 只列有凭证的 provider 名
qi auth print-api-key [--provider P] [--model M]
qi auth print-bearer-token [--provider P] [--model M] [--min-expiry 30m]
qi auth check [--provider P] [--model M] [--json] [--credentials]
```

- 解析顺序:auth store(`auth.json`)→ 约定环境变量 → `models.json` 的 `apiKey` 引用。
- 只读子命令必须给 `--provider` 或 `--model` 之一(只给 `--model` 时反查 provider;多家都有该模型时报错)。
- `check` 退出码:`ready`=0 / `not_ready`=1 / `invalid`=2。`print-bearer-token` 的 `--min-expiry` **只校验格式**
  (`qi` 的凭证没有过期时间)。两个命令都接受 `--no-refresh` 但它是空操作 —— **qi 没有 OAuth**。

## 信任

- `-a`, `--approve`:信任本项目 `.qi`(本次运行,加载项目级扩展)。
- `-na`, `--no-approve`:明确不信任。两个同给报错退出码 2。

未表态时看用户级 `settings.defaultProjectTrust`(`ask` 在无头下保守判不信任,并在 stderr 提示用 `-a`)。
见 [security.md](security.md)。

## 通用

- `-h`, `--help` / `-v`, `--version` / `--`(结束参数解析)。
- `--verbose`:把工具进度打到 stderr(TUI 里本来就有)。
- `--offline`:**被接受但什么都不做** —— qi 启动期没有任何网络操作(唯一联网的地方是模型调用)。
- **短旗标 `-t` / `-xt` / `-nt` / `-nbt` 是 argv 展开**:click 会按字符解析短选项(`-nt` 会被拆成 `-n t`,
  会话名静默变成 `"t"`),所以 qi 在交给 typer 之前把它们展开成长写法。代价是值位置上的 `-nt` 不被当旗标
  (`qi -n -nt` = 会话名叫 `-nt`)—— 这是对的。
