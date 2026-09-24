# qi 文档

qi 是**可扩展的编码 agent 框架**:裸 core 就是单 agent,跑在终端里;其余能力按需装扩展。
给它一个目标和一个工作目录,它会读文件、跑命令、改内容,并一步步把任务做完。

机器可读的导航清单是同目录的 [`docs.json`](docs.json)(与下面这些分类同源)。

## 开始用 qi

新装?跟着 [快速开始](quickstart.md) 装上、接一个模型、跑完第一个任务。

已经装好了,按你想做的事选:

- [在终端里用 qi](usage.md) —— 交互模式、消息队列、导出结果。
- [选一个模型](models.md) —— 接厂商、接本地模型或兼容端点。
- [接着或分叉一条会话](sessions.md) —— 恢复工作,或在不丢历史的前提下换条路。
- [配置 qi](configuration.md) —— 偏好、工作目录、指令与可复用资源放在哪。
- [理解 qi 怎么工作](how-qi-works.md) —— 工具、上下文、会话与 agent loop。

## 定制 qi

qi 能加载按需说明、加可执行集成、换配色,并把这些打包分发。

| 想做 | 看 |
| --- | --- |
| 改偏好(渲染模式、压缩、思考级别…) | [设置](settings.md)、[配置](configuration.md) |
| 加一份按需加载的能力说明 | [技能](skills.md) |
| 写扩展(工具 / 事件 / 命令 / 界面) | [写扩展](extensions.md)、[终端 UI 组件](tui.md) |
| 换配色 | [主题](themes.md) |
| 安装、声明、分发 | [扩展的安装与声明](packages.md) |

## 自动化与嵌入

- [命令行](cli.md):`qi -p "…"` 无头一次拿结果,进度走 stderr。
- [JSON 事件流](json.md):把一轮里的每个事件按行喂给上层程序。

qi 没有 RPC 模式与 SDK —— `--mode rpc` 会明确报未实现。

## 参考

查表用:[命令行](cli.md) · [斜杠命令](slash-commands.md) · [设置](settings.md) ·
[环境变量](environment-variables.md) · [键位](keybindings.md) · [供应商与凭证](providers.md) ·
[会话文件格式](session-format.md) · [上下文压缩](compaction.md) · [JSON 事件流](json.md)。

## 安全地使用

qi 的工具与扩展以 **qi 进程本身的权限**运行。信任门控只决定"要不要加载项目里的资源",**不是沙箱**。
动不认识的仓库、扩展或无人值守的自动化之前,先读 [安全地运行](security.md);要真隔离看
[隔离运行](containerization.md)。
