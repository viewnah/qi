# CLI 命令面

> 原则:**参数尽量与 pi 保持一致**;差异只来自扩展(MCP / 多 agent / web)与 pip 生态。
> 相关文档:[extensions.md](extensions.md)(扩展与旗标)、[settings.md](settings.md)、[tools.md](tools.md)(`tools` 三态与会话级收窄)、[json.md](json.md)(`--mode json`)。

## 0. 速查(按用途分类)

```text
── 运行 ────────────────────────────────
qi                              # 进 TUI(交互)
qi "<问题>"                      # 进 TUI,并把该消息作为首条发出(对齐 pi)
qi -p "<问题>"                    # 无头一次执行(单 agent)
qi -p --ext agent=<name> "<问题>"  # 以某个角色运行(需装 qi-agents)

── 会话 ────────────────────────────────
qi -c | -r | --session <id> | --fork <id> | -n <名> | --no-session
qi sessions list | show <id> | rm <id>
qi --export <file>

── Agent ────────────────────────────────
# 角色相关命令(`qi agents list|show|import|export`)随 P-E4c 移出 core → 归 qi-agents
# 装角色:把 `agent.md` 放进 ~/.qi/agent/agents/<名>/ 或 <项目>/.qi/agents/<名>/(见 qi-agents README)
qi --ext agent=reviewer "<问题>"           # 用某个角色跑(名字见 /agents)

── 插件 ────────────────────────────────
qi list                          # 列出已装扩展 + 与 settings.packages 的声明比对
qi doctor                        # 诊断(含「声明了没装」的安装命令);无 install 子命令,见 §4

── 初始化 / 凭证 ──────────────────────
qi init                         # 引导默认模型(复刻 QwenPaw):Provider Config → Add Models → Activate LLM
qi auth login|logout|list       # 管理 ~/.qi/agent/auth.json(0600);qi init 交互默认保留已存凭证

── 模型 / 诊断 ─────────────────────────
qi models list | qi doctor

── 安全 / 信任 ─────────────────────────
qi -a | -na                      # 信任 / 不信任项目 .qi;两个同给报错(退出码 2)
                                 # 未表态时看 settings.defaultProjectTrust(ask 保守判不信任)

── 通用 ────────────────────────────────
qi -h | -v | --verbose | --offline | -t <tools> | -xt <tools> | -nt | -nbt
# 工具收窄四件套等价的长写法:--tools / --exclude-tools / --no-builtin-tools / --no-tools
# ⚠️ 选项写在**消息之前**(`qi -nt "问题"`,不是 `qi "问题" -nt`):顶层选项在遇到
#    消息后不再解析 —— 写反了会报错并提示怎么改
```

## 1. 启动与运行

| 命令 | 说明 | pi 对齐 |
| --- | --- | --- |
| `qi [options] [--] [@files...] [messages...]` | **不带 `-p` 且 `--mode text` = 进 TUI**(无 `qi tui` 子命令,对齐 pi 的裸 `pi`);给了 messages 就在进界面后作为首条消息提交;`-p` = 无头执行后退出 | ✅ 形态同 pi |
| `-p, --print` | 无头一次执行;**单 agent**;要按角色跑用 `--ext agent=<名>`(qi-agents)。**默认只输出答案**(对齐 pi 的 "Print response and exit");分派行/工具进度默认不显示,加 `--verbose` 才输出(走 stderr,不污染 stdout) | ✅ |
| ~~`--agent <name>`~~ | **已删(P-E4c)**:角色选择归 qi-agents(`qi --ext agent=<名>`);`-a` 现在是**信任项目** | — |
| `--mode <text\|json>` | 输出格式。`json` = **事件流(一行一个 JSON 对象,隐含无头**、不进 TUI;stdout 只有事件,提示走 stderr) → 见 [json.md](json.md)。`rpc` 是二期 | ✅ |
| `--tools <tools>`(=`-t`) | 工具**严格白名单**(逗号/空格分隔):最终工具集就是这些(不再叠加默认集)。与 `--no-tools`/`--no-builtin-tools` **互斥**(同给报错退出码 2)。未注册的名字**会报一条提示**,不静默丢掉 | ✅ `pi --tools` |
| `--thinking <级别>` | 思考级别(off/minimal/low/medium/high/xhigh/max;非法值退出码 2)。不传则用 settings.json 的 defaultThinkingLevel,再退 off。provider 拒收 `reasoning_effort` 时自动去掉参数重试,并在 stderr 提示一次 | ✅ `pi --thinking` |
| `--exclude-tools <tools>`(=`-xt`) | 从**最终**工具集里排除这些工具(在 `--tools` / `--no-tools` / `--no-builtin-tools` 之后过滤) | ✅ `pi -xt` |
| `--no-builtin-tools`(=`-nbt`) | 禁用内置工具,**保留扩展装的工具** | ✅ `pi -nbt` |
| `--no-tools`(=`-nt`) | 禁用**全部**工具 | ✅ `pi -nt` |
| `-e, --extension <path>` | 本次运行临时加载一个扩展目录(可重复;仅本进程,`scope=temporary`)。**TUI 与无头都生效**。与 `--ext` 区分开:`--ext` 是给扩展声明过的**旗标**传值 | ✅ pi `-e` |
| `--append-system-prompt <文本>` | 追加到**每回合** system prompt 的末尾(可重复,空行连接)。区别:`.qi/SYSTEM.md` 是**整体替换**,这个是**追加**且不落盘 → 见 [system-prompt.md](system-prompt.md) §6.2 | ✅ pi 同名 |

## 2. 会话

| 命令 | 说明 | pi 对齐 |
| --- | --- | --- |
| `-c, --continue` | 续上次会话 | ✅ |
| `-r, --resume` | 选择会话恢复 | ✅ |
| `--session <path\|id>` / `--session-id <id>` | 指定会话 | ✅ |
| `--fork <path\|id>` | 从已有会话分叉出新会话(复制它的当前分支;`/fork` `/clone` 的另一入口) | ✅ `pi --fork` |
| `--session-dir <dir>` | 会话目录 | ✅ |
| `--no-session` / `-n, --name` | 临时会话 / 显示名 | ✅ |
| `qi sessions list \| show <id> \| rm <id>` | 列表 / 查看 / 删除。列表带会话 cwd(有分支时标 `分支点×N`);`show` 只回放**当前分支**、回放时用**记录时的展示名**(`display_name`,回落到 name)、用户消息不标说话人,并回放 `tool` entry(状态/耗时/退出码/参数) | qi 扩展 |
| `--export <file>` | 拷出会话 JSONL(**整个文件**,含其它分支) | 🟡 pi 默认导 HTML |

`-c/-r/--session` 管"接着跑哪段",`qi sessions` 管"历史浏览/删除",互补。

## 3. Agent 与分派

| 命令 | 说明 |
| --- | --- |
| ~~`--agent <name>`~~ | **已删(P-E4c)**;改用 `--ext agent=<名>`(qi-agents) | — |
| ~~`qi agents list\|show\|import\|export`~~ | **四条都已删(P-E4c)**:角色归 qi-agents。
装角色 = 把 `agent.md` 放进 `~/.qi/agent/agents/<名>/`;用它跑 = `qi --ext agent=<名>`;
列角色 = TUI 里 `/agents`。详见 qi-agents 的 README |

## 4. 扩展的安装与声明

**qi 没有 `install` / `remove` / `update` 子命令**(pi 有 `pi install`;qi 不对齐这一项)。
不做的理由:装扩展就是调 pip,而 pip 的目标环境分为「qi 自己的 venv」「uv tool 托管」
「只读的系统解释器」三种,封装一层只会把环境差异藏起来。qi 只负责**告诉你不一致**,
并把两条命令原样给你复制。

| 命令 | 说明 |
| --- | --- |
| `qi list` | 列出已装扩展(entry point + 目录通道),并与 `settings.packages` 的声明**双向**比对 |
| `qi doctor` | 同上,并输出「声明了没装」项的**可复制**装法 |

声明的**全部**写法(含归一、裸 URL 为什么不猜名字)、两条装法的失效面对比、以及输出的逐项读法,
见 [packages.md](packages.md);这里只给命令。

装法自己跑(两条路,失效面不同):

```bash
# 直接装进 qi 自己的 venv:最直接,但 uv tool 重建环境时会丢
/path/to/qi/venv/bin/python -m pip install qi-mcp
# 写进 uv 的托管依赖:升级 / 重建都不丢
uv tool install qi-agent --with qi-mcp
```

装完把它写进 `settings.json` 的 `packages`,否则 `qi doctor` 下次重建环境后无从知道它丢过:

```json
{ "packages": ["pip:qi-mcp", "pip:qi-agents"] }
```

声明里认不出名字的写法(如裸 `git+https://…`)会被 `qi list` / `qi doctor` **原样报出**,
不静默丢掉 —— 写成 `名字 @ URL` 才认得出来。

## 5. 设置、初始化与凭证

| 命令 | 说明 | pi 对齐 |
| --- | --- | --- |
| `qi config [-l] [--set K=V] [--unset K] [--json]` | 查看/编辑 `settings.json`;`-l` 作用于项目 `.qi/settings.json` | ✅ `pi config` |
| `qi init [-y] [-l] [--provider … --model …]` | 引导默认模型(写 `settings.json`);凭证写 `auth.json` | qi 新增(生态惯例) |
| `qi auth login\|logout <provider>` | 存/删该 provider 凭证 | ✅ pi `/login` `/logout` |
| `qi auth list` | 只列已存 provider 名(不回显 key) | ✅ |
| `qi auth print-api-key [--provider P] [--model M]` | 解析出的 key 打到 stdout(可管道) | ✅ `pi auth print-api-key` |
| `qi auth print-bearer-token [--provider P] [--model M] [--min-expiry 30m]` | 打印 Bearer 凭证 | ✅ `pi auth print-bearer-token` |
| `qi auth check [--provider P] [--model M] [--json] [--credentials]` | 就绪检查(带退出码) | ✅ `pi auth check` |

**`qi config`** —— 字段清单、合并规则与资源路径解析见 [settings.md](settings.md);
`--set` 的值先按 JSON 解析、失败则当字符串;`--unset` 支持点号路径(`compaction.enabled`)。

**`qi init`** —— 流程:`Provider Configuration`(选已有/新建 → Base URL → API 类型 → API Key,
直接写 `auth.json`)→ `Add Models`(`Add a model?` 循环)→ `Activate LLM Model`(选 provider →
选 model,写默认模型)。上下键选择 + 可见输入;已有凭证回车保留。
完整用法与示例见 [model-config.md §7](model-config.md#7-qi-init-用法)。

**`qi auth`** —— 解析顺序一律为 **auth store(`~/.qi/agent/auth.json`,0600) → 约定环境变量 →
`models.json` 的 `apiKey` 引用**(与 pi 一致)。三个只读子命令的细节:

- 必须给 `--provider` 或 `--model` 之一(只给 `--model` 时反查 provider;多个 provider 都含该模型时报错)。
- `check` 退出码对齐 pi:`ready`=0,`not_ready`=1,`invalid`=2(含参数错误);
  `--credentials` 在输出里带上凭证,`--no-refresh` 接受但不做任何事(qi 无 OAuth)。
- `print-bearer-token` 的 `--min-expiry` **只校验格式**(如 `30m` / `1h`)——qi 的凭证没有
  过期时间,该值不参与判断。
- `apiKey` 的 `!command` 形式按 `shlex` 拆参数、`shell=False` 执行(见
  [model-config.md §3](model-config.md#3-apikey-值语法与-pi-一致))。

## 6. 模型与诊断

| 命令 | 说明 | pi 对齐 |
| --- | --- | --- |
| `qi models list` | 列 `models.json` 的 provider/模型(标记默认) | 🟡 pi `--list-models` |
| `qi doctor` | 诊断:配置/provider/凭证(按 pi 凭证顺序验) | 🟡 替代 pi auth |

## 7. 安全与信任

| 命令 | 说明 | pi 对齐 |
| --- | --- | --- |
| `-a, --approve` | 信任项目 `.qi`(本次运行) | ✅ |
| `-na, --no-approve` | 忽略项目 `.qi` | ✅ |

## 8. 通用

`-h/--help`、`-v/--version`、`--verbose`、`--offline`、`--`(结束参数解析)。

- **`--offline` 无实际作用**:它对齐 pi 的"关掉全部启动期网络操作"(版本检查 / 包检查 /
  遥测)。qi 启动期**没有任何网络操作**(唯一联网的地方就是模型调用,而那是它存在的意义),
  所以这个旗标被接受但什么都不做 —— 宁可接受也不假装。
- **短旗标 `-t` / `-xt` / `-nt` / `-nbt` 是 argv 展开,不是 click 的短选项**:click 的短选项
  按**字符**解析(`-nt` 会被拆成 `-n t`,于是会话名静默变成 `"t"` —— 这是修掉的一个真实缺陷)。
  qi 在交给 typer 之前把它们展开成长写法(见 `cli.normalize_short_flags`);代价是值位置上的
  `-nt` 不会被当旗标(`qi -n -nt` = 会话名叫 `-nt`),这是对的。

### 输出约定(无头)

| 流 | 内容 |
| --- | --- |
| stdout | **仅答案正文**(`--mode json` 时为事件 JSON 行) |
| stderr | 默认空;`--verbose` 时为分派行(`→ qi (router, 0.90)`)与工具进度(`⚙ ls {"path": "."}` / `↳ …`) |

所以 `qi -p "问题" > out.txt` 拿到的就是纯答案,脚本无需过滤;对齐 pi 的
`-p`(`Print response and exit`:pi 实测 stdout 只有回答、stderr 为空)。
交互式 TUI 不受此限:仍实时显示分派与工具调用(pi 的 TUI 同样如此)。

### 轮次与中断(无头 vs 交互式)

| | 轮次上限 | 中断 / 兔底 |
| --- | --- | --- |
| 交互式(TUI / Web) | **无** —— runner 里根本没有“最多几轮”的概念(对齐 pi) | TUI `escape`、Web 客户端断开(ASGI 取消) |
| 无头(`-p` / `--mode json`) | **也无** —— pi 的 print 模式同样不限轮次 | 退出码都是 `128+n`:`SIGTERM`/`SIGHUP` → 回收在跑的子进程组 + `exit(143/129)`(对齐 pi 的 `killTrackedDetachedChildren`);`SIGINT`(Ctrl-C)→ asyncio 取消主任务 → `run_shell` 的 finally 回收 + `exit(130)`,不打 traceback;两条路径的半截回答都会落盘 |

轮次钩子 `RunnerSettings.stop_after` 与 pi 的 `shouldStopAfterTurn` 同形(每轮结束问一次嵌入方),
但 **qi 自己从不传它** —— pi-coding-agent 也从不实现那个钩子。“无头会不会跑飞”靠的是上面的信号 + 自动压缩,
而不是一个拍脑袋的数字。

## 9. 二期

- `qi web`:HTTP 宿主([web.md](../design/web.md))
- `--mode rpc`:headless JSONL-RPC,给外部客户端(IDE)

## 10. 明确不保留(理由落档)

| pi 命令/参数 | 不保留原因 |
| --- | --- |
| `pi config` 的 TUI 资源启停面板 | `qi config` 只做「查看 + 写键」(见 §5);资源启停随 packages 落地再补 |
| theme / prompt-template / skill 加载开关 | 概念不存在;内容跟 agent 走 |
| `--provider / --api-key / --models` | 模型在 `models.json` 配置(`qi init` 引导) |
| (`--thinking` 已实现,见 §1) | — |
