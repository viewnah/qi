# 使用指南

按**任务**组织。要按选项字母查全部命令面,看 [cli.md](cli.md);本文只讲"想做什么 → 敲什么"。

## 1. 三种运行形态

| 形态 | 命令 | stdout | 什么时候用 |
| --- | --- | --- | --- |
| 交互 TUI | `qi` / `qi "问题"` | 界面 | 边看边改、要看过程 |
| 无头一次 | `qi -p "问题"` | **只有答案** | 脚本里要结果(进度走 stderr,`--verbose` 才显示) |
| 事件流 | `qi --mode json "问题"` | 一行一个 JSON 事件 | 喂给上层程序 —— [json.md](json.md) |

两个容易记错的点:

- **不带 `-p`、`--mode text` = 进 TUI**(没有 `qi tui` 这种子命令,裸 `qi` 就是界面)。
- **`--mode json` 隐含无头**:就算不加 `-p` 也不进 TUI。

`qi "问题"` 与 `qi` 的区别只有一条:前者进界面后立刻把这句话作为首条消息发出。

## 2. 会话

默认每次 `qi` 都开新会话。

| 想做 | 命令 |
| --- | --- |
| 接着**最近**一条 | `qi -c` / `--continue` |
| 指定某条(支持 id 或文件名前缀) | `qi --session <id\|前缀>` |
| 从某条分叉出新会话 | `qi --fork <id\|前缀>` |
| 先起个名 | `qi -n "给这个会话起的名"` |
| **不落盘**(临时跑一把) | `qi --no-session`(内存会话,不产生文件) |
| 把会话导出成 JSONL | `qi --export out.jsonl`(可配 `--session`;省略则导最近一条,导出后**不跑模型**) |

管理(在仓库里有子命令,不必记路径):

```bash
qi -r                   # 浏览并选择恢复(重命名 / 删除也在那个选择器里,对齐 pi)
```

会话文件在哪、格式是什么、用量怎么算(以及为什么 `context_tokens` 不是各轮相加)见
[sessions.md](sessions.md) 与 [session-format.md](session-format.md)。

## 3. 上下文与思考

**上下文变长会自动压缩,不用管** —— 超过 `contextWindow - reserveTokens` 时,旧消息被压成一段结构化
摘要再继续。阈值与保留量可调(`settings.json` 的 `compaction`),机制见 [compaction.md](compaction.md)。

| 想做 | 怎么做 |
| --- | --- |
| 现在压一次 | TUI 里 `/compact`(也可带一段"重点压什么"的说明) |
| 换思考级别 | `--thinking off\|minimal\|low\|medium\|high\|xhigh\|max`;或在 TUI 里 `shift+tab` 轮转 |
| 看这轮用了多少 | TUI 状态行 / TUI 里回放会话;口径见 [sessions.md](sessions.md) §5 |
| 关掉自动压缩 | `settings.json` 写 `"compaction": {"enabled": false}`(手动 `/compact` 仍可用) |

**非法思考级别会以退出码 2 报错**(不会静默退回默认),因为打错了字却"看起来生效了"最难查。

## 4. 资源与扩展

| 想做 | 命令 |
| --- | --- |
| 额外加载一个技能(可重复,叠加) | `qi --skill <路径>` |
| 关掉技能的**自动发现** | `qi --no-skills` / `-ns`(`--skill` 仍生效) |
| 临时试用一个扩展目录 | `qi -e <目录>` / `--extension`(仅本进程,`scope=temporary`) |
| 给扩展声明过的**旗标**传值 | `qi --ext name=value`(也可直接写 `--name=value`) |
| 看装了哪些扩展 | `qi list` |
| 列已装 / 声明是否一致 | `qi list` 或 `qi doctor` 的"包声明"一节 |

`--no-skills` 与 `--skill` 的组合是刻意的:**"把自动发现的都关掉,只跑我指定的这个"** ——
调试单个技能时最常用。

`--ext` 与 `-e/--extension` 不是一回事:`--ext` 传的是**旗标值**(`name=value`),`-e` 传的是
**一个扩展目录**。装扩展的完整方式(含 `settings.packages` 声明层)见 [cli.md](cli.md) §4。

## 5. 信任项目

项目里的 `.qi/` 可能包含**可执行代码**(扩展),所以默认**不信任**:

| 命令 | 作用 |
| --- | --- |
| `qi -a` / `--approve` | 信任本项目(加载项目级扩展) |
| `qi -na` / `--no-approve` | 明确不信任 |

`settings.json` 里的 `defaultProjectTrust` 可取 `ask`(默认)/ `always` / `never`;`ask` 目前**等价于
不信任**(交互询问尚未实现),所以不想每次加 `-a`,就写 `"defaultProjectTrust": "always"`。

**未信任时挡的是项目级扩展**;项目级**技能**与 `AGENTS.md` **不挡**(它们算文本指令)。这条界线与
后果见 [security.md](security.md) §2。

## 6. 出问题时先跑这几个

```bash
qi doctor                       # 配置 / 凭证 / 扩展 / 包声明,一次全报
qi doctor | grep -A3 包声明      # 只看扩展声明与已装是否一致
qi --list-models                # provider / 模型 / 凭证状态
qi config --json                # 合并后的设置(含来自哪几个文件)
qi --verbose -p "问题"           # 无头模式下也看进度(走 stderr,不污染 stdout)
```

`qi doctor` 的凭证一节会给出**来源**(`auth(...)` / `env:XXX` / `config:...`),所以"我明明设了
环境变量"这类问题能直接看出来是"没读到"还是"读到了但为空"。

## 7. 几个完整场景

```bash
# 脚本里拿答案(不要进度、不要 TUI)
qi -p "把 CHANGELOG 里最近 10 条整理成一句话"

# 拿结构化事件喂给上层(比如统计工具调用)
qi --mode json "跑一遍测试" | jq -r 'select(.kind=="tool_end") | .data.status' | sort | uniq -c

# 只在本次运行里试用一个正在开发的扩展目录
qi -e ~/src/my-ext "试一下你的新工具"

# 调试单个技能(关掉其它技能的自动发现)
qi --no-skills --skill ./my-skill/SKILL.md "用这个技能做事"

# 换个模型跑同一句话(不改进默认配置)
qi --thinking high -p "这个 bug 的根因是什么"

# 项目里第一次跑(信任它,以加载项目级扩展)
qi -a "这个仓库的测试怎么跑"
```

## 8. 与 pi 的对应

命令行形态**刻意与 pi 对齐**:裸命令进 TUI、`-p` 无头、`-c` 续会话、`--session` / `--fork` / `-n`、
`--mode json`、`-e <path>`、`--ext`、`-a`。差异集中在 qi 多出来的部分(扩展声明层、`qi list`、
信任不记忆)。要按 pi 的习惯敲命令通常能work,遇到不认识的就 `qi --help` —— **不要照 pi 的
`docs/cli.md` 抄 qi 的选项**(两者并不完全一致)。
