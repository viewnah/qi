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
 escape interrupt · ctrl+c/ctrl+d clear/exit · / commands · ! bash   ← 快捷提示
 qi 是多 agent 编码框架:专职角色 + auto 分派(@name 可点名)。       ← dim 引导

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

## 2. `/` 内部命令(草案)

### 会话与恢复(对齐 pi)

| 命令 | 说明 |
| --- | --- |
| `/resume` | 选历史会话恢复 |
| `/new` | 新会话 |
| `/name <name>` | 会话显示名 |
| `/session` | 会话信息(文件/ID/消息数) |
| `/fork` | 从某条历史消息 fork 新会话 |
| `/export [file]` | 导出 HTML/JSONL |
| `/import <file>` | 从 JSONL 导入并恢复 |
| `/trust` | 保存项目 `.qi` 信任决定 |
| `/quit` | 退出 |

### Agent 与分派(qi 特色)

| 命令 | 说明 |
| --- | --- |
| `/agents` | 列出 agent(可 `@name` 直派) |
| `/mode auto\|manual` | 切换分派模式 |
| `/agent <name>` | manual 下锁定执行 agent |
| `/todos` | 预留(二期 todo 工作流) |

### 检查与维护

| 命令 | 说明 |
| --- | --- |
| `/help` | 命令帮助(含快捷键) |
| `/tools` | 当前 agent 的工具清单 |
| `/skills` | 当前 agent 绑定的技能 |
| `/doctor` | 装载/配置诊断 |
| `/reload` | 重载 agents/plugins/配置(改 agent.md 即生效) |

### 对齐 pi 但砍掉/改造的

| pi 命令 | qi 处理 |
| --- | --- |
| `/login` | 选 provider,写入 auth store(`~/.qi/agent/auth.json`,0600) |
| `/logout` | 清除某 provider 凭证 |
| `/model /thinking /scoped-models /settings` | 配置化:改 `models.json`,不进 TUI |
| `/llama /share /tree /clone /changelog` | 无对应能力(v2 按需,如 `/share` 随 web v2) |
| `/hotkeys` | 并入 `/help` |
| `/compact` | 等会话摘要设计(v2) |

## 3. 消息队列行为(对齐 pi)

- **Enter**:排队 steering 消息,当前轮工具执行完后送达
- **Alt+Enter**:follow-up,agent 全部工作完成后送达
- **Esc**:中止当前轮,队列消息退回编辑器
- **Alt+Up**:取回排队消息

## 4. 扩展点(v2)

- 插件可注册自定义 TUI 命令(对齐 pi `registerCommand`)
- web v2 视图复用同一事件流与渲染结构(web.md)
