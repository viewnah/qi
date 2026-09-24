# qi 是怎么工作的

qi 把**模型请求、工具执行、上下文拼装、会话落盘**串成一条链。一次会话就是这条链留下的记录:
消息、工具调用与结果、模型切换、压缩摘要都在里面。

会话里的消息构成一棵**树**,树上每一条从根到某个节点的路径就是一条**分支**。当前节点所在的那条
就是**活动分支**,它提供下一次模型请求的历史。

## Agent loop

用户消息落进活动分支。qi 用**系统提示词 + 活动分支 + 可用工具 + 模型设置**拼出一次请求,
发给选定的 provider。

provider 流式返回助手回复(可能包含文本与工具调用)。qi 记录回复、执行每个工具调用、记录结果 ——
这就是**一轮(turn)**。如果工具结果或排队消息还需要再问一次模型,就开下一轮;否则本次运行结束。

- **steer**(回合中按 `Enter`):当前回合结束后立刻作为下一回合发出。
- **follow-up**(回合中按 `Alt+Enter`):排在所有 steer 之后。
- **中止**(`escape`):协作式中断 —— 未执行的工具补上"已中断"结果、半截回答照常落盘,排队的消息退回编辑器。

## 上下文

活动分支提供对话历史:qi 把会话 entry 转成模型能吃的 user / assistant / tool-result 消息。
系统提示词由**基座**加**动态块**拼成:

```text
基座      代码内默认,或被 <cwd>/.qi/SYSTEM.md / ~/.qi/agent/SYSTEM.md 整体替换
动态块    项目上下文(AGENTS.md…) → 技能清单 → 手册索引 → 当前工作目录
```

- **技能**只把名字、描述、位置放进提示词;正文在任务匹配时由模型自己 `read`(渐进披露)。见 [skills.md](skills.md)。
- 没有能读文件的工具(`read` / `bash`)时,技能清单与手册索引**整块不注入**。
- 什么时候压、压哪一段、摘要长什么样,见 [compaction.md](compaction.md)。
- 自定义基座要注意:它会**整体替换**基座,连带「可用工具 / 指南」两块一起带走(见 [configuration.md](configuration.md))。

## 会话

持久会话是 JSONL 文件,每条 entry 带 `id` 与 `parentId`;当前节点决定活动分支。
字段、entry 类型与落盘顺序见 [session-format.md](session-format.md)。

- **接着**旧节点继续提问 = 同一个文件里再开一条分支;`--fork` / `/fork` / `/clone` 则是**新文件**。
- 模型上下文**只从活动分支重建**;压缩会插入一条摘要 entry,在后续请求里替掉更早的消息 —— 原始 entry 仍留在树里。
- 会话默认**懒建**:`qi` / `/new` 先有对象、不写文件,第一条助手回答才落盘;`--no-session` 则是内存会话。见 [sessions.md](sessions.md)。

## 界面

| 形态 | 命令 | 输出 |
| --- | --- | --- |
| 交互 TUI | `qi` / `qi "问题"` | 界面(默认 `fullscreen`:qi 拥有视口) |
| 无头一次 | `qi -p "问题"` | **只有答案**(进度走 stderr,`--verbose` 才显示) |
| 事件流 | `qi --mode json "问题"` | 一行一个 JSON 事件 → [json.md](json.md) |

三者用的是**同一套** agent 与会话机制。qi 没有 RPC 模式与 SDK。

## 工具

工具是**代码**(全局注册进 `ToolCatalog`),技能与资产是**内容**。内置 9 个:

| 工具 | 作用 |
| --- | --- |
| `read` | 读文件(路径 + 行范围;超长截断并提示续读) |
| `ls` | 列目录 |
| `find` | 文件名搜索(名称 / glob) |
| `grep` | 文本搜索(正则 / 关键字 + 行号 + 上下文) |
| `write` | 整写文件(新建 / 覆盖) |
| `edit` | 精确替换(`old_text → new_text`;多处匹配报错) |
| `bash` | 执行命令(工作目录 = 会话 cwd;真正的 bash;输出截断 + timeout) |
| `powershell` | 执行 PowerShell(**仅 Windows**;非 Windows 调用时返回可照做的错误) |
| `clarify` | 向用户提一个澄清问题;没人可问时不挂住,返回"需要用户澄清"给模型 |

**工具集三态**(对齐 Claude Code):省略或 `["*"]` = 全部可用;显式名单 = allowlist 收窄;
名单里有未知名 → 启动报错。会话级有四个旗标:`-t` 严格白名单 / `-xt` 排除 / `-nt` 全禁 /
`-nbt` 只去内置;`settings.defaultTools` 只挑**内置**那一档。见 [cli.md](cli.md)。

`bash` / `powershell` 的子进程还会拿到一组**会话环境变量**(`QI_SESSION_ID` / `QI_MODEL` …),
这是 agent 自查"我现在跑的是什么模型"的唯一通道 —— 见 [environment-variables.md](environment-variables.md)。

`bash` **不做命令级过滤**;真边界只能来自容器 / VM / 受限用户,见 [security.md](security.md) 与
[containerization.md](containerization.md)。扩展可以注册自己的工具(收 `Tool`,也收 dict 形状),
见 [extensions.md](extensions.md)。

## 扩展与资源

扩展是加载进 qi 进程的 Python 模块,能注册工具、命令、快捷键、旗标、provider、事件处理器与渲染回调。
技能提供按需说明与附带文件;主题提供终端配色;扩展通过 pip 或本地目录分发(声明在 `settings.packages`)。

见 [extensions.md](extensions.md)、[skills.md](skills.md)、[themes.md](themes.md)、[packages.md](packages.md)。

## 信任与权限

项目级资源(`.qi/extensions/`、项目级 `settings.extensions[]`)要**先过信任门控**才加载;项目级**技能**与
`AGENTS.md` 等文本指令不受门控。工具与扩展都以 **qi 进程本身的系统权限**运行 —— 门控管的是"加载什么",
不是"能干什么"。见 [security.md](security.md)。
