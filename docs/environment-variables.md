# 环境变量

分三类:**qi 自己的配置变量**、**qi 注入给 shell 工具子进程的会话变量**,以及**各 provider 的凭证变量**
(后者在 [providers.md](providers.md))。

## qi 的配置变量

| 变量 | 作用 |
| --- | --- |
| `QI_AGENT_HOME` | **agent 目录本身**(默认 `~/.qi/agent`)。全部用户级状态的挂载点:`settings.json` / `models.json` / `auth.json` / `sessions/` / `skills/` / `extensions/` / `trust.json` |
| `QI_CONFIG_DIR` | 名字空间根(默认 `~/.qi`)。仅用于推导默认路径与旧布局迁移 |
| `QI_AGENT_CONFIG` | **直接指定 `models.json` 文件**(优先于项目 / 全局路径) |
| `QI_THEME` | `dark` / `light` / `auto`,覆盖 `settings.theme` → [themes.md](themes.md) |

> `QI_AGENT_HOME` 指向的是**目录本身**,不是它的父目录 —— 拿它去拼 `agent/settings.json` 会得到
> `…/agent/agent/settings.json`。这是这个变量最常见的误用。

`models.json` 的完整查找顺序是 `QI_AGENT_CONFIG` → `<项目>/.qi/models.json` → `~/.qi/agent/models.json`,
见 [models.md](models.md)。

## 注入给 shell 工具的会话变量

`bash` 与 `powershell` 的**子进程**(以及它们拉起的命令)会拿到这五个:

| 变量 | 值 |
| --- | --- |
| `QI_SESSION_ID` | 当前会话 id |
| `QI_SESSION_FILE` | 会话 JSONL 的绝对路径 |
| `QI_PROVIDER` / `QI_MODEL` | 当前模型(子运行里是**子运行自己的**模型) |
| `QI_REASONING_LEVEL` | 当前思考级别 |

**为什么需要**:换模型是**界面状态**,不作为消息进对话 —— 所以 agent 无从"感知"切换动作,
`env | grep QI_` 是它唯一能**自证**"我现在跑的是什么"的通道(同一条也写进了 bash 的
prompt guidelines)。

两条约定:

- 注入前**先删掉**这五个键再填 —— 否则子运行里跑的 bash 会继承父的值。
- 取不到的字段**不设**,而不是设成空串(空串会被读成"设过了,值是空的")。

它们**只注入子进程**,不改变 qi 自己进程的环境。

## provider 凭证变量

`DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 这类**约定变量**由 provider 名推导,
是凭证解析的第 2 顺位(优先于 `models.json` 的 `apiKey`,低于 auth store)。完整表与排查口径见
[providers.md](providers.md)。
