# 在终端里用 qi

交互形态的用法。一次性与脚本化用法在 [命令行](cli.md);斜杠命令清单在 [斜杠命令](slash-commands.md);
键位在 [键位](keybindings.md)。

## 输入第一句

```bash
qi                      # 进界面,什么都不发
qi "分析这个仓库"        # 进界面,并把这句话作为首条消息发出
```

- `@path` 把文件内容带进第一条消息(`@xx` 会补全路径:装了 `fd` 走全树搜索,否则只扫当前目录一层)。
- `enter` 提交,`shift+enter` / `ctrl+j` 换行;输入框最多 8 行。
- `↑` / `↓` 在空编辑器或行首时翻输入历史(首次进入留住草稿,手动改动即退出)。
- **选项写在消息之前**(`qi -nt "问题"`):顶层选项在遇到消息后不再解析。

## 看它怎么干活

transcript 从下往上长,底部固定是「编辑器 + 上下边框 + footer」。footer 最多三行:

```text
~/Desktop/qi (master) • 会话名          ← cwd(+git 分支)(+会话名)
↑12k ↓678 1.2%/1.0M (auto)   deepseek/v4.1 • medium   ← token 统计 + 模型与思考级别
排队 2 / 扩展状态 / 瞬时提示            ← 只在这些内容时出现
```

- 工具调用是一块带底色的卡片(标题 = `read <path>`,输出按工具截断);`ctrl+o` 展开 / 折叠。
- 思考内容与回答**分开流式**(灰色斜体),`ctrl+t` 随时隐藏 / 显示。
- 压缩与分支摘要在回放里显示成底色块(`[compaction]` / `[branch]`),`ctrl+o` 展开看摘要。
- 正在跑时上边框内嵌 `Working` spinner;`escape` 中断(协作式:未执行的工具补"已中断"结果、半截回答照常落盘)。

## 改变方向

| 操作 | 行为 |
| --- | --- |
| 回合中 `enter` | **steer**:排队,当前回合结束后立刻作为下一回合发出 |
| 回合中 `alt+enter` | **follow-up**:排队,排在所有 steer 之后 |
| `escape` | 中止当前轮;有排队消息就退回编辑器 |
| `alt+up` | 取回排队消息(多条按 steer → follow-up 拼回编辑器) |

排队消息会以 dim 行写进 transcript(带"当前回合结束后发送"/"follow-up"标签),不会静默丢。
**命令(`/x`)与 bash(`!x`)例外:回合进行中也立即执行** —— 否则 `/quit`、`/tree` 这类按不下去。
`/new` 清空队列;队列只存在当前进程里,不落盘。

## 换模型与设置

| 想换 | 怎么做 |
| --- | --- |
| 模型 | `/model`(选择器)或 `ctrl+l`;`ctrl+p` / `ctrl+shift+p` 在 `/scoped-models` 圈定的清单里轮换 |
| 思考级别 | `/thinking` 或 `shift+tab` 循环(off → minimal → … → max);`--thinking <级别>` 只本次运行 |
| 偏好(主题 / 交互开关) | `/settings` 面板(`enter` 换值、`ctrl+s` 保存到**用户级** settings) |
| 本次运行的模型 | `qi --provider <名> --model <provider/模型>:<级别>` |

**切换会落盘**:换模型写一条 `model_change`、换级别写一条 `thinking_level_change`,所以**下次打开这条会话
仍用当时的模型与级别**(CLI 显式给的 `--model` / `--thinking` 优先)。还原前会检查凭证:provider 已经没密钥
时退回默认并在启动提示里说明,不会拿着注定 401 的模型继续跑。

思考级别经 litellm 的 `reasoning_effort` 下发(`minimal` / `low` / `medium` / `high`);`xhigh` / `max`
收敛为 `high`。只有模型在 `models.json` 里声明 `reasoning: true` 时才带参。provider 拒收时自动去掉参数
重试并提示一次。**默认 `medium`**。

## 接着还是重开

| 想做的事 | 怎么做 |
| --- | --- |
| 接着最近一条 | `qi -c` |
| 指定某条 | `qi --session <id\|前缀\|路径>` |
| 浏览 / 恢复 / 重命名 / 删除 | `/resume`(或 `qi -r` 直接进选择器;`tab` 切"当前目录 ↔ 全部") |
| 从某条分叉出新会话 | `/fork [序号\|id]`(把那条消息放回编辑器)、`/clone`、`qi --fork <id>` |
| 跳到本会话的别的节点继续 | `/tree`(同文件内开分支;被放弃的那段会压成 `branch_summary` 挂过去) |
| 不落盘地跑一把 | `qi --no-session`(内存会话) |

**裸 `qi` 与 `/new` 走懒建**:会话对象立刻就有,但**文件推迟到第一条助手回答**才出现 ——"进来看看就退出"
不会在会话目录里留下空文件。`/session` 看当前会话信息(ID / 文件 / cwd / 消息数 / 分支点 / 模型 / 用量)。

## 跑一条终端命令

| 写法 | 行为 |
| --- | --- |
| `!<命令>` | 执行 shell;输出以「上下 `─` + `$ cmd` + 输出」块展示,并把「命令 + 输出」落成一条 user 消息供后续回合参考 |
| `!!<命令>` | 同样执行,但**不进上下文**(块边框变 dim,标题标出) |

这是**你亲手敲的**操作:`!` 命令不经任何过滤(与模型走 `bash` 工具是两回事,见 [security.md](security.md))。

## 复制、导出、分享

| 操作 | 命令 |
| --- | --- |
| 复制最后一条回答 | `/copy` 或 `ctrl+x`(剪贴板走 OSC 52) |
| 导出会话 | `/export [file]` 或 `qi --export out.jsonl`(按扩展名:`.html` → 自包含 HTML(只含当前分支),其余 → 原始 JSONL) |
| 导入会话 | `/import <file>` |
| 分享 | `/share` —— 传成**私有** GitHub gist 并复制链接(token 见 [providers.md](providers.md)) |

## 调整终端

- **渲染模式**:`fullscreen`(qi 默认)= 进备用屏、qi 拥有视口,滚轮只在界面内滚;**`regular`** = 不占全屏,
  滚动交给终端。改 `settings.tuiMode` 或 `--tui-mode <regular|fullscreen>`;改完要**重启**(`--tui-mode` 只覆盖当次)。
- **配色**:`dark` / `light` / `auto`(探测终端背景色);`QI_THEME=light qi`。见 [themes.md](themes.md)。
- **版式**:`editorPaddingX` / `outputPad` / `autocompleteMaxVisible` 在 settings 里调。

## 出问题时

```bash
qi doctor                  # 配置 / 凭证 / 扩展 / 包声明,一次全报(并给出可复制的修复命令)
qi config --json           # 合并后的设置(含来自哪几个文件)
qi --list-models           # provider / 模型 / 凭据状态
qi --verbose -p "问题"      # 无头模式下也看进度(stderr,不污染 stdout)
```

`qi doctor` 的凭证一节会给出**来源**(`auth(...)` / `env:XXX` / `config:...`)——
"我明明设了环境变量"这类问题能直接看出是"没读到"还是"读到了但为空"。
