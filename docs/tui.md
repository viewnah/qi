# TUI(交互界面)

> 状态:v1 实现(视觉基线对齐 pi)。本文只放交互形态:布局、`/` 内部命令、消息渲染。与 [cli.md](cli.md) 区分:CLI = 一次性命令;TUI = 交互命令。
> 相关:[dispatcher.md](dispatcher.md)(分派行渲染)、[web.md](web.md)(v2 同款视图)、[settings.md](settings.md)(`theme`)。

## 1. 布局(已实现;逐项对齐 pi)

配色/版式以 pi 的 `modes/interactive` 为基线,数据源是同一份 `theme/dark.json` /
`theme/light.json`(`src/qi_agent/themes/` 逐字移植),渲染细节对照过 pi 的
`user-message.js` / `assistant-message.js` / `tool-execution.js` / `footer.js` / `dynamic-border.js`。

```text
(终端滚动历史保留 —— inline 渲染,不占全屏、不进备用屏)

 qi v0.1.0                                                    ← bold accent + dim
 escape interrupt · ctrl+c clear/exit · ctrl+o tools · / commands · @agent   ← 快捷提示
 qi 是多 agent 编码框架:专职角色 + auto 分派;/help 看全部命令。       ← dim 引导

[Agents]
  code-analyst, general

 读一下 pyproject.toml 然后总结          ← 用户消息:userMessageBg 底色块(padding 1,1)

● → qi (router, 0.90)                    ← qi 的 auto 分派(pi 无此概念,按其行风格)

 我来读一下这个文件。                      ← 助手 markdown(padding 0,1,无底色)

 read pyproject.toml                     ← 工具块:tool{Pending,Success,Error}Bg 底色
 [project]                                 标题 = `read <path>`(工具名 bold + 路径 accent)
 name=qi                                   输出 10/15/20 行(按工具),超出给省略提示

────────── ⠦ Working ──────────          ← 编辑器上边框;工作中内嵌 spinner(80ms)
 输入…
────────────────────────────────          ← 下边框(空闲时上下都是整行 ─)
~/Desktop/qi (master) • tui               ← footer 1:cwd(+git 分支)(+会话名),dim
↑12k ↓678 1.2%/1.0M (auto)      deepseek/v4.1 • medium   ← footer 2:token 统计 + 右对齐模型
qi · auto                                 ← footer 3:状态行
```

实现要点:

- **inline**:`App.run(inline=True, inline_no_clear=True)`;transcript 最多占
  `终端高 - 6`(编辑器 3 + footer 3),再长在内部滚动(不吞终端滚动历史)。
- **主题**:`theme` 设 `auto`(默认)时用 OSC 11 探测终端背景色 → dark/light,
  并把探测到的背景色设为 Textual 主题底色 —— 于是 inline 区域看不出“被填色”。
  `QI_THEME=dark|light|auto` 可直接覆盖。
- **编辑器**:上下两条 `─` 动态边框(`border` 色);工作态上边框嵌入
  `⠋…⠏ Working`(accent + muted)。
- **markdown**:Rich `Markdown` 渲染,样式名字映射到 pi 的 `md*` 键;
  代码高亮用 pygments 主题但颜色取自 pi 的 `syntax*` 键(无底色)。
- **分派行**:`● → <display_name> (source, 置信度)`,无底色,不加卡片。

已知差异(Textual 与 pi 自研渲染器的边界,不做逐字节对齐):OSC133 zone 标记、
代码块左侧 `│` 边线、图片/kitty 协议、超长历史进终端原生 scrollback。

## 2. `/` 内部命令

> 以**实现状态**为准(不再写“草案”)。pi 的 23 个内置命令见 `dist/core/slash-commands.js`;
> 下表里「计划中」= qi 尚无对应后端能力,单独提示,不冒充“未知命令”。

### 已实现(对齐 pi)

| 命令 | 说明 |
| --- | --- |
| `/help` | 命令帮助(含实现/计划分区) |
| `/hotkeys` | 快捷键(明写哪些 pi 键位还没做) |
| `/quit` | 退出 |
| `/new` | 新会话 |
| `/resume [id]` | 不给 id = 列出历史会话;给 id = 恢复 |
| `/sessions` | 列出历史会话 |
| `/name <name>` | 会话显示名(进 footer) |
| `/session` | 会话信息(ID/文件/cwd/消息数/模型/用量) |
| `/model [p/m]` | 当前模型 / 切换模型(等同 ctrl+l / ctrl+p) |
| `/export [file]` | 导出会话 JSONL(默认 `./qi-<id>.jsonl`;**pi 默认导出 HTML** —— qi 无 HTML 导出器) |
| `/import <file>` | 从 JSONL 导入并切换会话(重名给提示,不静默覆盖) |
| `/copy` | 复制最后一条回答到剪贴板(OSC 52) |
| `/reload` | 重载 agents / plugins / 配置(主题改动需重开) |
| `/login <provider>` | **只给指引**:`qi auth login <provider>`(密钥不进会话记录) |
| `/logout [provider]` | 删除已存凭证(无密钥输入,可直接在 TUI 里做) |
| `/changelog` | 显示 `CHANGELOG.md`(qi 仓库暂无该文件) |

### qi 独有

| 命令 | 说明 |
| --- | --- |
| `/agents` | 列出 agent |
| `/mode auto\|manual` | 切换分派模式 |
| `/agent <name>` | manual 下锁定执行 agent |
| `/tools` | 当前/全部 agent 工具清单(简版) |
| `/clear` | 清屏 |
| `@name` 开头 | 直接点名 agent(与 pi 无关,qi 的 auto 分派补充) |

### 计划中(pi 有,qi 缺后端能力)

| pi 命令 | 缺什么 |
| --- | --- |
| `/thinking` | 思考级别(要贯穿 llm 调用与系统提示) |
| `/scoped-models` | Ctrl+P 轮换清单(qi 现在轮完 models.json 里全部) |
| `/compact` | 上下文压缩/摘要 |
| `/tree` `/fork` `/clone` | 会话树与分支 |
| `/settings` | TUI 内设置面板 |
| `/share` | GitHub gist 分享 |
| `/trust` | 项目信任门控(qi 只有 `defaultProjectTrust` 字段,没有信任判定) |

### 键位(已对齐 pi;`core/keybindings.js`)

| 键 | qi 行为 | pi 的 action |
| --- | --- | --- |
| `escape` | 中断当前回合(取消 worker) | `app.interrupt` |
| `ctrl+c` | 清空输入框;再按一次退出 | `app.clear` + `app.exit` |
| `ctrl+d` | 输入框为空时退出;非空删右侧字符 | `app.exit` |
| `ctrl+o` | 展开/折叠工具输出 | `app.tools.expand` |
| `ctrl+x` | 复制最后一条回答 | `app.message.copy` |
| `ctrl+g` | `$EDITOR` 编辑当前输入 | `app.editor.external` |
| `ctrl+l` | 模型选择器(模态列表) | `app.model.select` |
| `ctrl+p` / `ctrl+shift+p` | 下一个 / 上一个模型 | `app.model.cycleForward/Backward` |
| `ctrl+z` | 挂起(回到 shell) | `app.suspend` |

实现注记:Textual 的 `Input` 默认把 `ctrl+c/ctrl+x/ctrl+d` 绑到「复制/剪切/删右侧」,
qi 用 `priority=True` 抢过来以匹配 pi 语义(`ctrl+d` 非空时仍自己调 `delete_right`);
另关闭 Textual 的 command palette,因为它的默认键 `ctrl+p` 在 pi 里是切模型。

**尚未对齐**(qi 缺后端能力或输入层,`/hotkeys` 里也如实列出):

| 键 | pi 用途 | qi 缺什么 |
| --- | --- | --- |
| `shift+tab` / `ctrl+t` | 思考级别 / 折叠思考块 | qi 无 thinking 概念(事件流里就没有 thinking 内容) |
| `ctrl+n` / `ctrl+r` | 会话列表过滤 / 重命名 | 无会话选择器 UI(`/sessions` 只打印列表) |
| `alt+enter` / `alt+up` | 排队 follow-up / 取回排队 | 无消息队列 |
| `ctrl+y` / `alt+y` | kill-ring 的 yank / yank-pop | Textual 无 kill-ring,`ctrl+y` 仍是它的 redo |
| `tab` | 补全(`@` 文件 / `/` 命令) | 无补全引擎;qi 的 tab = 缩进 |
| `ctrl+v` | 粘贴图片 | 无图片输入,目前只会粘文本 |

### 输入层:多行编辑器(对齐 pi `pi-tui/components/editor.js`)

qi 用 Textual 的 `TextArea` 做成 pi 的编辑器,补齐了这些键(其余是 Textual 自带):

| 键 | 行为 | pi 的 action |
| --- | --- | --- |
| `enter` | 提交 | `tui.input.submit` |
| `shift+enter` / `ctrl+j` | 换行 | `tui.input.newLine` |
| `ctrl+b` / `ctrl+f` | 光标左 / 右 | `cursorLeft` / `cursorRight` |
| `alt+b` `alt+f` `alt+←/→` | 按词移动 | `cursorWordLeft` / `cursorWordRight` |
| `alt+d` | 删后一个词 | `deleteWordForward` |
| `ctrl+-` | 撤销 | `undo`(pi 不用 `ctrl+z` —— 那个是挂起) |
| `ctrl+w` / `ctrl+u` / `ctrl+k` / `alt+backspace` | 删词 / 删到行首 / 删到行尾 | 同名 action(Textual 自带) |
| `ctrl+v` | 粘贴(含括号粘贴,多行可用) | 文本部分对齐 |

实现注记:

- `TextArea` 在 `tab_behavior="indent"` 下会把 **escape 当成「换焦点」并吞掉**,而 pi 的 escape 是中断;
  qi 在 `Editor._on_key` 里直接拦下 escape 并发消息给 App(不走 priority 绑定 ——
  那会抢掉模型选择器的 escape)。
- 编辑器 `height: auto`(最多 8 行,矮终端还会再降);transcript 上限按
  `终端高 - (编辑器当前行数 + 上下边框 + footer)` 动态算 —— 编辑器长高时 transcript 让位,
  否则 inline 区域会把输入框挤掉。
- `enter` 是提升到 priority 的绑定(`TextArea` 原生会把 enter 插成换行,必须先抢)。

qi 的输入层是**多行编辑器**(见下面「输入层」一节):`/` 与 `@` 补全、`!` bash 模式、
消息队列仍未实现——它们现在是纯输入层功能,不再受控件限制。

## 3. 消息队列行为(pi 现状;qi 未实现)

pi 的行为(供后续对齐参考):

- **Enter**:排队 steering 消息,当前轮工具执行完后送达
- **Alt+Enter**:follow-up,agent 全部工作完成后送达
- **Esc**:中止当前轮,队列消息退回编辑器
- **Alt+Up**:取回排队消息

qi 现状:回合进行中输入框仍可输入并可提交(并发起新的 worker),没有排队/回退语义。

## 4. 扩展点(v2)

- 插件可注册自定义 TUI 命令(对齐 pi `registerCommand`)
- web v2 视图复用同一事件流与渲染结构(web.md)
