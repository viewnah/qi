# qi 文档

qi 是**可扩展的编码 agent 框架**:裸 core 就是单 agent;角色、MCP、Web 都是扩展。
想读"为什么这样设计",看 [overview.md](../design/overview.md)(总设计)与 [design/](../design)(内部设计与决策记录)——
本文只负责**把手册排好序**。

机器可读的导航清单是同目录的 [`docs.json`](docs.json)(与下面这张表同源)。

## 开始

| 文档 | 讲什么 |
| --- | --- |
| [quickstart.md](quickstart.md) | 从零到第一次对话:装、配模型、跑起来 |
| [cli.md](cli.md) | 命令面与全部选项 |
| [tui.md](tui.md) | 交互界面:布局、`/` 命令、消息队列 |
| [settings.md](settings.md) | `settings.json`:分层、字段清单、资源路径与排除项 |
| [security.md](security.md) | 会执行什么、信任门控挡什么、`bash` 的边界 |

## 扩展与定制

| 文档 | 讲什么 |
| --- | --- |
| [extensions.md](extensions.md) | 扩展机制:API 面、事件与链语义、依赖契约、官方三件套 |
| [packages.md](packages.md) | 扩展的安装与声明:`settings.packages` 怎么写、两条装法的失效面、怎么读 `qi list` / `qi doctor` |
| [skills.md](skills.md) | 技能:六层来源、`SKILL.md` 格式、渐进披露 |
| [themes.md](themes.md) | 主题:dark/light、选择优先级、终端背景探测 |
| [system-prompt.md](system-prompt.md) | 系统提示词怎么拼:基座 → 项目上下文 → 技能 → 手册索引 → cwd |
| [qi-agents README](../extensions/qi-agents/README.md) | 角色:`agent.md` 字段、`subagent` 用法、角色私有技能(**角色手册归扩展自己**) |

## 参考

| 文档 | 讲什么 |
| --- | --- |
| [sessions.md](sessions.md) | 会话:文件在哪、命令行操作、用量口径、分支与 fork |
| [session-format.md](session-format.md) | **会话文件格式**(JSONL + 树):字段、entry 类型、落盘顺序不变量 |
| [compaction.md](compaction.md) | 上下文压缩与分支摘要:阈值、切点、摘要格式 |
| [model-config.md](model-config.md) | `models.json` 与 `qi init` 用法 |
| [providers.md](providers.md) | 供应商与凭证:解析顺序、`apiKey` 写法、环境变量 |
| [tools.md](tools.md) | 内置工具与工具集三态 |

## 程序化使用

| 文档 | 讲什么 |
| --- | --- |
| [json.md](json.md) | `--mode json` 事件流:事件类型、`data` 键、消费示例 |

## 读完还想改 qi 本身

| 去处 | 内容 |
| --- | --- |
| [PLAN.md](../design/PLAN.md) | 开发计划、阶段表、未决清单 |
| [overview.md](../design/overview.md) | 总设计:架构总览、既定决策索引、技术选型、目录布局、安全总原则 |
| [extensions-design.md](../design/extensions-design.md) | 扩展化的阶段拆解(§9)、未定清单(§11)、与 Claude Code 三方对照(§12)、E20→E25 论证(§13) |
| [web.md](../design/web.md) | Web 宿主的设计与 API 契约 |
| [bash-allowlist.md](../design/bash-allowlist.md) | bash 策略的变更记录(为什么删掉首词白名单) |
| [agent-config-design.md](../design/agent-config-design.md) | 角色系统的设计过程(含**已过时项对照表**) |
| [dispatcher.md](../design/dispatcher.md) | auto 分派(**整体作废**;未作废的部分保留) |
| [plugins.md](../design/plugins.md) | v1 插件机制(已被扩展机制取代) |
