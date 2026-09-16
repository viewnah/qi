# CLI 命令面

> 状态:设计讨论中。原则:**参数尽量与 pi 保持一致**;差异仅来自多 agent / pip 生态 / 无密钥存储。
> 相关文档:[agent-config.md](agent-config.md)、[plugins.md](plugins.md)、[web.md](web.md)(v2)。

## 0. 速查(按用途分类)

```text
── 运行 ────────────────────────────────
qi                              # 进 TUI(交互)
qi "<问题>"                      # 进 TUI,并把该消息作为首条发出(对齐 pi)
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

── 初始化 / 凭证 ──────────────────────
qi init                         # 引导默认模型(复刻 QwenPaw):Provider Config → Add Models → Activate LLM
qi auth login|logout|list       # 管理 ~/.qi/agent/auth.json(0600);qi init 交互默认保留已存凭证

── 模型 / 诊断 ─────────────────────────
qi models list | qi doctor

── 安全 / 信任 ─────────────────────────
qi -a | -na                      # 信任/不信任项目 .qi

── 通用 ────────────────────────────────
qi -h | -v | --verbose | --offline | -t <tools> | -xt <tools> | -nt | -nbt
```

## 1. 启动与运行

| 命令 | 说明 | pi 对齐 |
| --- | --- | --- |
| `qi [options] [--] [@files...] [messages...]` | **不带 `-p` 且 `--mode text` = 进 TUI**(无 `qi tui` 子命令,对齐 pi 的裸 `pi`);给了 messages 就在进界面后作为首条消息提交;`-p` = 无头执行后退出 | ✅ 形态同 pi |
| `-p, --print` | 无头一次执行;**auto 分派**;给 `--agent` 即 manual。**默认只输出答案**(对齐 pi 的 "Print response and exit");分派行/工具进度默认不显示,加 `--verbose` 才输出(走 stderr,不污染 stdout) | ✅ |
| `--agent <name>` | 指定 agent(长参;不用 `-a`,pi 的 `-a`=approve) | qi 新增 |
| `--mode <text\|json>` | 输出格式(`rpc` 二期)。`json` 输出事件 JSON 行,**隐含无头**(不进 TUI) | ✅ |
| `-t <tools>` / `-xt <tools>` | 工具 allowlist / denylist 临时覆盖(tools 三态) | ✅ |
| `--thinking <级别>` | 思考级别(off/minimal/low/medium/high/xhigh/max;非法值退出码 2)。不传则用 settings.json 的 defaultThinkingLevel,再退 off。provider 拒收 `reasoning_effort` 时自动去掉参数重试,并在 stderr 提示一次 | ✅ `pi --thinking` |
| `-nt` / `-nbt` | 禁用全部工具 / 保留插件工具 | ✅ |
| `--plugin <path>` | 本次运行临时加载插件 | 🟡 pi `-e` |

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
| `--agent <name>` | 指定 agent(manual);省略 = auto 分派 |
| `qi agents list` | 装载的 agent:来源/描述/工具/绑定 |
| `qi agents show <name>` | 单 agent 解析诊断(skills/mcp/data_sources 是否生效) |
| `qi agents import <source> [-l] [--force]` | 导入 agent(拷贝 + 装载校验器预检 + 凭证扫描);详见 agent-config.md §10 |
| `qi agents export <name> [-o <path>]` | 导出 agent 目录/压缩包(产物可直接 import) |

## 4. 插件(对齐 pi 命名与 `-l`,底层 pip 生态)

| 命令 | 说明 | pi 对齐 |
| --- | --- | --- |
| `qi install <source> [-l]` | 本地目录 → 插件目录通道;`pip:<pkg>` → 转 pip | ✅ `pi install` |
| `qi remove\|uninstall <name> [-l]` | 移除本地目录插件 | ✅ |
| `qi list [-l]` | 列出已安装插件 | ✅ |
| `qi update [source\|self]` | 插件 / 本体更新 | ✅ |

`-l`:写入项目 `.qi/`(而非用户 `~/.qi/`),同 pi 的 project 语义。

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

- `qi web`:HTTP 宿主(web.md)
- `--mode rpc`:headless JSONL-RPC,给外部客户端(IDE)

## 10. 明确不保留(理由落档)

| pi 命令/参数 | 不保留原因 |
| --- | --- |
| `pi config` 的 TUI 资源启停面板 | `qi config` 只做「查看 + 写键」(见 §5);资源启停随 packages 落地再补 |
| theme / prompt-template / skill 加载开关 | 概念不存在;内容跟 agent 走 |
| `--provider / --api-key / --models` | 模型在 `models.json` 配置(`qi init` 引导) |
| (`--thinking` 已实现,见 §1) | — |
