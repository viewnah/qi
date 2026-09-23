# 实现细节与决策记录(手册之外)

> 这里放**从手册搬出来的东西**:实现要点、内部机制、以及只对改动者有意义的设计决策记录。
> 手册只讲"怎么用"与"行为契约";想知道"为什么这么实现"、或要改这几块代码,再看这里。
>
> 来源:2026-09 的重排中,`docs/how-qi-works.md`、`docs/configuration.md`、`docs/tui.md`(旧版)、
> `docs/models.md` 里的实现与决策部分。原文都在 git 历史里。

## 1. 系统提示词的拼装

- 入口:`qi_agent.system_prompt.build_system_prompt(unit, base_prompt, cwd=…, tools=…, context_files=…)`;
  `qi_agent.runner` 里同名可导入(向后兼容)。
- 默认基座的静态文案:`system_prompt.py` 的 `DEFAULT_IDENTITY` / `DEFAULT_ENVIRONMENT` / `DEFAULT_METHOD`;
  「可用工具」「指南」两块按**解析后**的工具集动态生成(`build_guidelines` 按工具名条件化:
  有 `bash`/`powershell` 但没有 `grep`/`find`/`ls` 时给"用 shell 做文件操作",措辞分三种)。
- 基座来源解析:`loader.resolve_base_prompt()` → `("", "builtin")` 表示用代码内默认;
  项目 > 用户 > 内置,**取第一个非空文件**(空文件/读失败视为未配置)。
- 项目上下文:`paths.project_context_ancestors()`(全局 + 祖先链,止于 git 根,按路径去重),
  每级目录按 `AGENTS.override.md > AGENTS.md > AGENTS.MD > CLAUDE.md > CLAUDE.MD` 取第一个命中。
- 技能块:只给 name / description / location 的 XML;有 `read` 就说"用 read 读全文",只有 `bash` 就说 bash;
  两者都没有 → 整块不注入。相对路径以 SKILL.md 所在目录为基准解析,并在工具命令里用绝对路径。
- 手册索引:由 `docs/docs.json` 的 `navigation` 生成(`system_prompt.docs_navigation()`,
  **递归展平**分组);根目录取 `paths.docs_dir()`(wheel 里的 `qi_agent/docs/` → 源码树 `<repo>/docs/`),
  两处都找不到就整块不注入。只在**默认基座**分支出现。
- `--append-system-prompt`:追加到每回合提示词的**末尾**,多段空行连接,空白段忽略;
  值是**可读文件路径**时读文件内容。

#### 决策记录

| 决策 | 结论 |
| --- | --- |
| 基座形态 | **代码内字符串**(`system_prompt.py`),按工具集动态生成「可用工具 / 指南」——v1 的包数据 Markdown 方案废弃(静态、无法按工具集变化) |
| `SYSTEM.md` 语义 | **整体替换**默认基座;动态块照旧追加(不是"整条 prompt 只剩你的文件") |
| `APPEND_SYSTEM.md` | 不实现;追加走 `--append-system-prompt` 或写进 `SYSTEM.md` |
| 空文件 | 视为未配置,继续向下找;不产生空基座 |
| 工具清单 | 列**解析后**的工具集(模型可调用全集);不写"你可能还有其他工具" |
| 项目上下文 | 候选顺序 + 全局/祖先链 + 止于 git 根;空文件跳过;**不需要项目信任** |
| 技能注入 | XML(含 location);无 `read`/`bash` 则整块不注入 |
| 自身文档索引 | 由 `docs.json` 生成;两处目录都找不到就整块不注入 |
| 模板化 | 不做 `{{cwd}}` 之类占位符;动态块由代码追加 |

## 2. 内置工具的实现要点

- 结构:工具是 `Tool` 数据类(`name` / `description` / `parameters` / `execute` + 可选
  `prompt_snippet` / `prompt_guidelines` / `label` / `prepare_arguments` / `render_*` / `execution_mode`);
  执行结果是 `str` 或结构化 `ToolOutcome(status / result / duration_ms / exit_code / error / details)`。
  `details` 是**给前端**看的自由结构,不进 LLM 上下文;`duration_ms` 由 runner 统一计时。
- 截断:`MAX_RESULT = 200` 行 / `read` 默认 `MAX_FILE_CHARS = 50_000`;超长只返回前 N 行 + 提示。
- 错误以结果形式返回(`status=error`),不抛异常 —— 让模型可读、可重试。
- `bash` 的 shell 解析顺序:`settings.shellPath` → Windows 上 Git Bash 已知路径 → PATH 的 bash →
  Unix `/bin/bash` → `sh` 兜底。老版 WSL 的 `C:\Windows\System32\bash.exe` 只认 stdin,走 `-s` + 管道。
  旧实现用 `create_subprocess_shell`(Windows 上等于 cmd.exe —— 工具名叫 bash 却跑 cmd)。
- 子进程 stdin 是 `DEVNULL`;超时按**进程组**回收。TUI 的 `!` 命令同样不经过滤。
- `file tools` 的路径边界:`ToolContext.guard()`(绝对化 + 断言落在 `workdir` 内)。
- 会话环境变量写入点:`Runtime._session_env()`(`_tool_ctx` 交给工具)/ `Runtime._child_session_env()`
  (子运行换成自己的模型)。

#### 决策记录

| 决策 | 结论 |
| --- | --- |
| 内置 | **9 个**:read / ls / find / grep / write / edit / bash / powershell + `clarify` |
| 工具命名 | 文件名搜索用 `find`,不用 `glob` |
| `edit` 方案 | 从一开始就是精确替换,diff 匹配多处直接报错 |
| `tools` 三态 | 省略 / `["*"]` = 全部;显式名单 = allowlist;未知名报错 |
| denylist | `disallowed_tools` 在 **core 未实现**;现存手段是白名单(`tools` / `-t` / `-xt` / `-nt` / `-nbt`) |
| bash 策略 | **无命令级过滤**;限制靠工具白名单或容器/VM;文件工具路径限会话目录 |
| 审批 / HITL | 未做(见 [PLAN.md](PLAN.md) 的 v2 清单) |

## 3. 终端界面(Textual)的实现注记

- **两种模式**:`get_default_screen()` 在 `regular` 时给首屏打 `.regular` 类(**必须在首屏创建时**带上 —— 放到
  `on_mount` 里再加,inline 启动的第一帧会按 fullscreen 规则把 Screen 撑到终端高,先空一屏再缩回去)。
  fullscreen 用 Textual 的非 inline 模式(`App.run()`),`#log { height: 1fr }` 内部滚动,编辑器 + 边框 + footer 固定;
  `_sync_log_height()` 在这条路上**不插手**(inline style 塞 `max-height` 会把 `1fr` 盖掉)。
- **鼠标**:只在 fullscreen 开(为了把滚轮喂给自己的 transcript)。regular 下必须关:上报鼠标会让不支持
  SGR(1006)的终端退回 X10 报文,坐标 ≥ 0x80 时整段不是合法 UTF-8,Textual inline 驱动的严格解码器会抛
  `UnicodeDecodeError` → 输入线程死 → `App.panic`(上游未修:textualize/textual#6456)。`tui.py` 因此加了两层:
  进界面前 `_reset_mouse_reporting()` 清残留上报,`_harden_inline_input()` 把 `linux_inline_driver` 的解码器
  换成 `errors="replace"`。
- **命令选择器**用 `EditorSlotPanel`(ModalScreen):全宽、`align-vertical: bottom`、
  `margin-bottom` = footer 实际行数、`border-top/bottom: solid`、底色取探测到的终端背景色、backdrop 透明 ——
  于是它看起来"贴在同一条底线上",与编辑器同宽。列表渲染与补全面板共用一份 `pi_select_text`。
- **priority 绑定**:Textual 的 App 级 bindings 会盖掉模态里同名的键,所以 `QiTui.check_action` 在
  `screen_stack > 1` 时把 App 级快捷键返回 `False`,让模态自己的 priority 绑定接管(仅模态打开时生效)。
  Textual 的 `Input` 默认把 `ctrl+c/x/d` 绑到复制/剪切/删右侧,qi 用 `priority=True` 抢过来。
- **kill-ring**:`KillRing` 在 `Editor` 里覆写删除 action,按"删前/删后文本求差"取真正删掉的段;
  `ctrl+k` 在行尾/空行时 Textual 走"并下一行 / 删整行",这两支不进环。
- **为什么默认 fullscreen**:`regular` 把滚动权交给终端,向上翻会看到启动 qi 之前的 shell 输出;
  qi 要的是"滚动只在本界面内",所以默认进备用屏。
- **已知差异**(不做逐字节复刻):OSC133 zone 标记、代码块左侧 `│` 边线、图片/kitty 协议;
  fullscreen 退出时不重放 transcript。
- `tuiMode` **不在 `/settings` 面板里**(面板只放值域有限的键),改它走 `qi config --set tuiMode` 或 `--tui-mode`,
  且要重启才生效。

## 4. `qi init` 的完整交互流程

`qi init` 复刻 QwenPaw 的引导,三步走。打印出来的是**逐问的界面契约**(改这里要同步改它):

```text
Working dir: ~/.qi

=== LLM Provider Configuration ===
--- Provider Configuration ---
? 选择 provider                       # 上下键:已有([✓]/[✗])+ 预置([预置])+ ＋ 新建
Provider name: <新 provider 名>       # 仅新建时(敲预置名也会自动套用预置)
Base URL (OpenAI-compatible endpoint): <必填>
? API 类型                            # openai-completions / openai-responses / anthropic-messages / google-generative-ai
<provider> API key: <可见输入>          # 已有凭证回车保留;预置是 `$ENV` 引用时可回车跳过

--- Add Models ---
? Models                              # 上下键;没模型时默认「＋ Add a model」,已有模型默认「✓ Done」
  ❯ ＋ Add a model
    ↻ Refresh model list (GET /models)   # = `qi init --refresh`,只加不删
    ✓ Done
Model identifier / display name / Supports reasoning / contextWindow / maxTokens

? Configure another provider?         # 上下键,默认「否」

--- Activate LLM Model ---
? Select provider for LLM → ? Select LLM model
✓ Configuration saved to ~/.qi/agent/models.json
✓ 默认模型已写入 ~/.qi/agent/settings.json
```

要点:全程上下键选择,只有必填项(provider 名 / Base URL / 模型 id / API Key)手动输入;预置选中即物化;
`Activate LLM Model` 把 `defaultProvider` / `defaultModel` 写进 `settings.json`(不再进 `models.json`);
`-l` 写项目 `.qi/models.json`。非交互 `-y` 的旗标表见 [docs/models.md](../docs/models.md)。

## 5. 其它零散取舍

- **`qi doctor` 也打印"分派模型"**:`routerProvider` / `routerModel` 解析仍在代码里(`config.resolve_router_model`),
  但 **auto 分派已取消**(core 是单 agent),所以这两个键是历史遗留:留着不报错,`qi doctor` 会显示,
  没有任何自动分派会用到它们。
- **`--offline`**:接受但什么都不做(启动期本来没有网络操作)。
- **`QI_AGENT_HOME` 指向目录本身**,不是父目录。
