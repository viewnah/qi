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
qi -r                           # 浏览/恢复历史会话(重命名与删除也在那个选择器里)
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
qi init --list-presets          # 内置的预置 provider(国产为主)
qi init --preset deepseek       # 直接用预置写 models.json + 设默认模型(可逗号写多个)
qi init --refresh deepseek      # 从厂商 /models 接口拉模型列表写回 models.json(只加不删)
qi auth login|logout|list       # 管理 ~/.qi/agent/auth.json(0600);qi init 交互默认保留已存凭证

── 模型 / 诊断 ─────────────────────────
qi --list-models | qi doctor

── 安全 / 信任 ─────────────────────────
qi -a | -na                      # 信任 / 不信任项目 .qi;两个同给报错(退出码 2)
                                 # 未表态时看 settings.defaultProjectTrust(ask 保守判不信任)

── 通用 ────────────────────────────────
qi -h | -v | --verbose | --offline | -t <tools> | -xt <tools> | -nt | -nbt
# 工具收窄四件套等价的长写法:--tools / --exclude-tools / --no-builtin-tools / --no-tools
# ⚠️ 选项写在**消息之前**(`qi -nt "问题"`,不是 `qi "问题" -nt`):顶层选项在遇到
#    消息后不再解析 —— 写反了会报错并提示怎么改

── 模型 / 会话 / 资源(按次覆盖)─────────────
qi --provider beta --model beta/m3:high      # 换模型与思考级别(仅本次)
qi --api-key sk-xxx                          # 换密钥(**不落盘**)
qi --list-models [搜索词]                     # 列模型后退出
qi -r | --session <path\|id> | --session-id <id> | --session-dir <dir>
qi --system-prompt "<文本\|文件>" | --append-system-prompt "<文本\|文件>"
qi -ne | -nc                                  # 关扩展发现 / 关 AGENTS.md 注入
```

## 1. 启动与运行

| 命令 | 说明 | pi 对齐 |
| --- | --- | --- |
| `qi [options] [--] [@files...] [messages...]` | **不带 `-p` 且 `--mode text` = 进 TUI**(无 `qi tui` 子命令,对齐 pi 的裸 `pi`);给了 messages 就在进界面后作为首条消息提交;`-p` = 无头执行后退出 | ✅ 形态同 pi |
| `-p, --print` | 无头一次执行;**单 agent**;要按角色跑用 `--ext agent=<名>`(qi-agents)。**默认只输出答案**(对齐 pi 的 "Print response and exit");分派行/工具进度默认不显示,加 `--verbose` 才输出(走 stderr,不污染 stdout) | ✅ |
| ~~`--agent <name>`~~ | **已删(P-E4c)**:角色选择归 qi-agents(`qi --ext agent=<名>`);`-a` 现在是**信任项目** | — |
| `--mode <text\|json>` | 输出格式。`json` = **事件流(一行一个 JSON 对象,隐含无头**、不进 TUI;stdout 只有事件,提示走 stderr) → 见 [json.md](json.md)。`rpc` 是二期 | ✅ |
| `--tools <tools>`(=`-t`) | 工具**严格白名单**(逗号/空格分隔):最终工具集就是这些(不再叠加默认集)。与 `--no-tools`/`--no-builtin-tools` **互斥**(同给报错退出码 2)。未注册的名字**会报一条提示**,不静默丢掉 | ✅ `pi --tools` |
| `--thinking <级别>` | 思考级别(off/minimal/low/medium/high/xhigh/max;非法值退出码 2)。不传则用 settings.json 的 defaultThinkingLevel,没配就用 `medium`(pi 的默认档)。provider 拒收 `reasoning_effort` 时自动去掉参数重试,并在 stderr 提示一次 | ✅ `pi --thinking` |
| `--tui-mode <regular\|fullscreen>` | TUI 渲染模式(非法值退出码 2),压过 settings.json 的 `tuiMode`。`fullscreen`(qi 默认)= 进备用屏、qi 拥有视口,滚轮只滚 transcript;**`regular` = inline**(不占全屏、不进备用屏,滚动交给终端,滚轮/PageUp 会翻到启动前的 shell 输出)。详见 [tui.md](tui.md) §1 | ✅ `pi --tui-mode`(默认值不同:pi 默认 regular) |
| `--exclude-tools <tools>`(=`-xt`) | 从**最终**工具集里排除这些工具(在 `--tools` / `--no-tools` / `--no-builtin-tools` 之后过滤) | ✅ `pi -xt` |
| `--no-builtin-tools`(=`-nbt`) | 禁用内置工具,**保留扩展装的工具** | ✅ `pi -nbt` |
| `--no-tools`(=`-nt`) | 禁用**全部**工具 | ✅ `pi -nt` |
| `-e, --extension <path>` | 本次运行临时加载一个扩展目录(可重复;仅本进程,`scope=temporary`)。**TUI 与无头都生效**。与 `--ext` 区分开:`--ext` 是给扩展声明过的**旗标**传值 | ✅ pi `-e` |
| `--append-system-prompt <文本\|文件>` | 追加到**每回合** system prompt 的末尾(可重复,空行连接)。值是**可读文件**时读文件内容。区别:`.qi/SYSTEM.md` 是**整体替换**,这个是**追加**且不落盘 → 见 [system-prompt.md](system-prompt.md) §6.2 | ✅ pi 同名 |
| `--system-prompt <文本\|文件>` | **整体替换**基座(与 `.qi/SYSTEM.md` 同一语义,只是从命令行给) | ✅ pi 同名 |
| `--provider <名>` / `--model <模式>` | 按次覆盖模型。`--model` 写 `provider/模型`,可带 `:<思考级别>` 后缀(**只认已知级别**,所以 `openrouter/x:free` 不会被误切);只给 `--provider` 时用它 models.json 里的第一个模型 | ✅ pi 同名 |
| `--api-key <键>` | 按次覆盖密钥,**不落盘**(优先于 auth store / 环境变量 / `models.json` 的引用) | ✅ pi `--api-key` |
| `--models <清单>` | 本次运行 Ctrl+P 的轮换清单(逗号分隔)。**不回写** settings —— 要持久化用 TUI 的 `/scoped-models` | ✅ pi `--models` |
| `--list-models [搜索词]` | 列出可用模型后退出(ctx / max 两列来自模型条目),每行带 `credential` 列(缺密钥的也列 —— 这是诊断面)。搜索词写成**位置参数**:`qi --list-models sonnet`(pi 的可选值形态 click 表达不了)。**解析不出凭证的 provider 不列**(例外:当前默认模型那个照列,否则看不出缺哪把钥匙);没登录的预置去 `/login` | ✅ 形态同 pi |
| `--no-extensions`(=`-ne`) | 关掉扩展**发现**(entry point / `~/.qi/agent/extensions/` / `settings.extensions[]`);`-e` 显式给的仍生效 | ✅ pi `-ne` |
| `--no-context-files`(=`-nc`) | 不注入 `AGENTS.md` / `CLAUDE.md` | ✅ pi `-nc` |

## 2. 会话

| 命令 | 说明 | pi 对齐 |
| --- | --- | --- |
| `-c, --continue` | 续上次会话 | ✅ |
| `-r, --resume` | 浏览并选择一个历史会话(**需要 TTY**;无头下用 `--session <id>`)。与 TUI 的 `/resume` 是同一条路 | ✅ |
| `--session <path\|id>` | 指定会话:先当**文件路径**(`.jsonl`),再当 id / 文件名前缀 | ✅ |
| `--session-id <id>` | 用**精确的项目会话 id**,不存在则建 | ✅ |
| `--fork <path\|id>` | 从已有会话分叉出新会话(复制它的当前分支;`/fork` `/clone` 的另一入口) | ✅ `pi --fork` |
| `--session-dir <dir>` | 会话目录(优先于 `settings.sessionDir`) | ✅ |
| `--no-session` / `-n, --name` | 临时会话(**内存,不落盘**)/ 显示名 | ✅ |
| 历史浏览 / 恢复 / 重命名 / 删除 | 都在 **TUI 的 `/resume` 选择器**里(`qi -r` 直接进那里)。范围(当前目录 / 全部)、搜索(`"短语"` / `re:<正则>`)与排序(树状 / 最近 / 最相关)都在面板里,键位见 [tui.md](tui.md);回放用**记录时的展示名**(`display_name`,回落到 name) | 无 CLI 子命令(pi 也是在选择器里 Ctrl+D 删,且删前要确认) |
| `--export <file>` | **按扩展名选格式**(pi 同口径):`.html` / `.htm` → **自包含 HTML**(只含当前分支;CSS 内联、内容全部转义);其余 → 原始 **JSONL**(整个文件,含其它分支) | ✅ |

`-c/-r/--session` 管"接着跑哪段";**历史浏览 / 重命名 / 删除在 TUI 的选择器里**(`qi -r` 直接进)—— 与 pi 同一种分工(它也没有 `pi sessions`)。

## 3. Agent 与分派

| 命令 | 说明 | pi 对齐 |
| --- | --- | --- |
| ~~`--agent <name>`~~ | **已删(P-E4c)**;改用 `--ext agent=<名>`(qi-agents) | — |
| ~~`qi agents list\|show\|import\|export`~~ | **四条都已删(P-E4c)**:角色归 qi-agents。装角色 = 把 `agent.md` 放进 `~/.qi/agent/agents/<名>/`;用它跑 = `qi --ext agent=<名>`;列角色 = TUI 里 `/agents`。详见 qi-agents 的 README | — |

## 4. 扩展的安装与声明

| 命令 | 说明 |
| --- | --- |
| `qi install <来源> [-l]` | 装一个扩展:**调 pip**(本地目录只登记,不调)+ 写进 `settings.packages`。`-l` 写项目 `.qi/settings.json` |
| `qi remove <来源> [-l]` / `qi uninstall` | 只从 `settings.packages` 里**移除声明**(**不卸包** —— 与 pi 同义);同时把 `pip uninstall` 命令打出来 |
| `qi update [来源\|self] [--self\|--extensions\|--all\|--extension X\|--force]` | 更新 qi 自己或已声明的扩展。**无目标时只更自己**(pi 同默认);`--models` 在 qi 无意义(见下) |
| `qi list` | 列出已装扩展(entry point + 目录通道),并与声明**双向**比对 |
| `qi doctor` | 同上,并输出「声明了没装」项的**可复制**装法 |

声明的**全部**写法(含归一、裸 URL 为什么不猜名字)、输出的逐项读法,见 [packages.md](packages.md)。

> **为什么 `qi install` 只是“帮你跱一步”而不是封装 pip**:目标环境有「qi 自己的 venv /
> `uv tool` 托管 / 只读的系统解释器」三种,而它们的行为真的不同:
>
> - `uv tool` **会整个重建环境**(uv 文档原话:"or re-created entirely via subsequent
>   `uv tool install`")—— 所以 pip 装进去的扩展会在下次重建时**被抹掉**,声明还在,
>   由 `qi doctor` 把它报出来并给出装法;
> - 只读解释器(Homebrew / 系统 Python)下 pip 自己会报错 —— qi 不拦,把 pip 的输出
>   原样给你,并补上 `uv tool install qi-agent --with <包>` 那条出路。
>
> 所以 `qi install` **每次都把要跑的 pip 命令打出来**后再跑:你能看出装到了哪个环境,
> 也能直接照拄。想永久不丢就用 `uv tool install qi-agent --with <包>`。
>
> **`qi update --models` 是个例外**:pi 会去拉远端模型目录,qi **没有那个概念** ——
> 模型全在 `models.json` 里自己维护,所以它只说明这一点然后退出(不假装做了)。

装完把它写进 `settings.json` 的 `packages`,否则 `qi doctor` 下次重建环境后无从知道它丢过:

```json
{ "packages": ["pip:qi-mcp", "pip:qi-agents"] }
```

声明里认不出名字的写法(如裸 `git+https://…`)会被 `qi list` / `qi doctor` **原样报出**,
不静默丢掉 —— 写成 `名字 @ URL` 才认得出来。

## 5. 设置、初始化与凭证

| 命令 | 说明 | pi 对齐 |
| --- | --- | --- |
| `qi config [-l] [--get K] [--set K=V] [--unset K] [--json]` | 不带旗标:**TTY 下开资源启停面板**(space 勾选 · ctrl+s 保存 · escape 取消;`-l` 切作用域),非 TTY 打一张资源表 + 设置总览;`--get` 读键、`--set` / `--unset` 写删键 | ✅ `pi config` |
| `qi init [-y] [-l] [--provider … --model …]` | 引导默认模型(写 `settings.json`);凭证写 `auth.json` | qi 新增(生态惯例) |
| `qi init --list-presets` / `--preset <名>[,<名>]` / `--refresh <名>[,<名>]` / `--refresh-all` | 列出 / 物化**预置 provider**(国产为主:baseUrl + 约定环境变量 + 模型,每个模型带核过的 `contextWindow` / `maxTokens`;只补缺不覆盖,幂等)。见 [providers.md §2.1](providers.md#21-预置-providerqi-init---preset) | qi 新增 |
| `qi auth login\|logout <provider>` | 存/删该 provider 凭证 | ✅ pi `/login` `/logout` |
| `qi auth list` | 只列已存 provider 名(不回显 key) | ✅ |
| `qi auth print-api-key [--provider P] [--model M]` | 解析出的 key 打到 stdout(可管道) | ✅ `pi auth print-api-key` |
| `qi auth print-bearer-token [--provider P] [--model M] [--min-expiry 30m]` | 打印 Bearer 凭证 | ✅ `pi auth print-bearer-token` |
| `qi auth check [--provider P] [--model M] [--json] [--credentials]` | 就绪检查(带退出码) | ✅ `pi auth check` |

**`qi config`** —— 字段清单、合并规则与资源路径解析见 [settings.md](settings.md);
`--set` 的值先按 JSON 解析、失败则当字符串;`--get` / `--unset` 支持点号路径(`compaction.enabled`)。

**资源的“关”写的是声明,不是删条目** —— 目录扩展→`settings.extensions[]` 里加 `-<路径>`
否定项;pip 包→`settings.packages` 里那一条换成对象形态 `{"source": X, "extensions": []}`。
所以状态**被记住**了(`qi config` 那张表能区分“启用”与“主动关了”),而且能表达“项目关掉、
全局还开着”。详见 [packages.md](packages.md) §1。

> **面板/清单的范围与 pi 逐字相同**:已声明的包 + 本地资源数组(`extensions[]` 等)。
> 用 `pip install` 自己装进去、**没写进 `settings.packages`** 的扩展**不出现** ——
> 那是个该被修的状态,由 `qi doctor` 报出来并告诉你该写什么(pi 也一样:它的
> `pi config` 只看 `packages` 与本地目录)。

**`qi init`** —— 流程:`Provider Configuration`(选 provider → Base URL → API 类型 → API Key,
直接写 `auth.json`)→ `Add Models`(**`Models` 菜单三选一循环**:`＋ Add a model` /
`↻ Refresh model list`(`GET /models`,只加不删,等价 `--refresh`)/ `✓ Done`)→
`Activate LLM Model`(选 provider →
选 model,写默认模型)。上下键选择 + 可见输入;已有凭证回车保留。选择器里三种都在:
**已有**(`[✓]`/`[✗]`)、**预置**(标 `[预置]`,选中即物化进 `models.json`,没选中的不写盘)、
**新建**(永远排最后;敲预置名同样套用预置)。
完整用法与示例见 [model-config.md §7](model-config.md#7-qi-init-用法)。
不想手写时用 `--list-presets` / `--preset <名>` 走预置(见
[providers.md §2.1](providers.md#21-预置-providerqi-init---preset))。

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
| `qi --list-models [搜索词]` | 列 `models.json` 的 provider/模型(标记默认;缺密钥的也列 —— 诊断面) | ✅ pi `--list-models` |
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

- `--mode rpc`:headless JSONL-RPC,给外部客户端(IDE)—— 现在传它会**明确报未实现**(§10)
- TUI 的 `/settings` 里改 `tuiMode` 立即切换模式(pi 能当场换渲染器;qi 现在要重启)

## 10. 与 pi 的差异(逐条落档)

**第一类:接受旗标,但功能还没实现** —— 传了会退出码 2 说清缺什么(不假装生效):

| pi 旗标 | 缺什么 |
| --- | --- |
| `--prompt-template` / `--no-prompt-templates` | qi 没有 prompt 模板的发现 / 注入机制。要固定前缀就写进 `.qi/SYSTEM.md`,或做成技能 |
| `--theme` / `--use-theme` / `--no-themes` | 不支持自定义主题文件(内置只有 `dark` / `light` / `auto`)。选主题用 `QI_THEME=light` 或 `qi config --set theme=` |
| `--mode rpc` | 输出模式只有 `text` / `json`;stdio JSON-RPC 见 §9 |

**第二类:明确不保留**(理由落档):

| pi 的 | 为什么 qi 不做 |
| --- | --- |
| `/share` 的 **Radius** 路径 | 那是 pi 的**托管服务**(上传 JSONL,并先注入 `pi.share` entry:`systemPrompt` + 工具定义);qi 没有对应物,不做 |
| `/share` 的 **viewer 预览链接** | pi 展示自己的预览服务链接(`getShareViewerUrl(gistId)`);qi 只能给 gist 链接本身 |
| `/share` 的 **`gh` 依赖** | **刻意偏离**:pi 的 gist 路径就是 shell 出 `gh`(先 `gh auth status` 查登录,再 `gh gist create --public=false`);qi **直调 API**(stdlib `urllib`),少一个外部依赖 |
| `pi config` 的资源启停 TUI | **已对齐**(`qi config` TTY 下面板;见 §5) |
| `-e/--extension <source>` 的 **npm / git** 来源 | Python 侧的分发走 pip(`settings.packages`);没有 npm / git 那种"从任意源拉"的通道 |
| `--append-system-prompt` 传**文件路径** | **已实现**(值是可读文件就读文件内容) |

> `pi install` / `remove` / `uninstall` / `update` **已对齐**(见 §4)—— 它们曾在这一节里,
> 理由写的是"封装 pip 会藏起环境差异";现在的做法把那条顾虑直接讲给了用户
> (先打命令再跑、失败补 uv 出路),所以不再需要靠"不做"来避开它。
>
> 曾经把 `--provider` / `--api-key` / `--models` 列在这里(理由写"模型在 `models.json` 配置")——
> 那三条现在都实现了(见 §1),而且那条理由记错了对象:`--models` 是 Ctrl+P 的轮换清单,
> 不是"模型配在哪"。skill 加载开关(`--skill` / `--no-skills`)也早已对齐。
