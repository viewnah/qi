# 安全地运行

qi 能执行任意命令、读写任意文件,而**装进来的扩展就是任意代码**。本文讲清**有哪些闸门、哪些地方没有闸门**,
以及边界应该画在哪。qi **不提供进程内沙箱** —— 真隔离见 [隔离运行](containerization.md)。

## 先分清什么会"执行"

| 种类 | 是什么 | 未信任项目时 |
| --- | --- | --- |
| **扩展**(extension) | **可执行代码**,与 qi 同进程、同一棵依赖树、全权限 | **挡住**(不扫项目目录) |
| **技能**(skill) | **文本指令**:描述 + 正文,模型按需读取并照做 | **不挡** |
| **项目上下文**(`AGENTS.md` 等) | **文本指令**,整篇进系统提示词 | **不挡** |
| **`bash` 工具** | 全权子进程(无命令级过滤) | 与信任无关(它就是个工具) |

界线画在"**可执行 vs 文本**"上。所以信任门控**不能**阻止一个恶意仓库通过 `AGENTS.md` 或
`.agents/skills/**/SKILL.md` 影响模型行为 —— 那一侧没有任何自动闸门,只能靠你自己看。

## 项目信任

项目级 `.qi/` 可能含可执行代码,所以默认**不信任**:

| 值 / 命令 | 含义 |
| --- | --- |
| `qi -a` / `--approve` | 信任本项目(**本次运行**),加载项目级扩展 |
| `qi -na` / `--no-approve` | 明确不信任;两个同给报错(退出码 2) |
| `settings.defaultProjectTrust` | `ask`(默认)/ `always` / `never`。**只从用户级读** —— 项目级写了会被忽略并提示 |

优先级:**CLI 显式表态 > `project_trust` 事件(用户级 / `-e` 扩展可投票)> `trust.json` 的按目录决定 >
`defaultProjectTrust`**。

- `ask` 在**没有界面**时(`qi -p`)**保守判不信任**:跳过项目级资源,并在 stderr 提示用 `-a`。CI 必须显式信任。
- **按目录记住决定**:TUI 里 `/trust`(连带上一层)、`/trust no`、`/trust forget`,存 `~/.qi/agent/trust.json`
  —— **写在用户 home,不碰仓库里任何文件**(仓库不能为自己背书)。写完当前会话不重载,重启后生效。
- 判定结果与理由会进启动提示(`qi doctor` 也显示),例如
  `未信任项目(defaultProjectTrust=ask(无 UI;用 -a 信任)):.qi/extensions/ 未加载`。

**未信任时不加载**:<项目>`/.qi/extensions/` 与项目级 `settings.json` 的 `extensions[]`(只挡项目那一份)。
**仍然生效**:项目级技能(`.agents/skills/`、`<cwd>/.qi/skills/`)、项目上下文(`AGENTS.md` 等)、
项目级 settings 的普通字段(`defaultTools`、`theme`…)。细目见 [configuration.md](configuration.md)。

## bash 的边界

**qi 没有命令级过滤**:不筛子命令、不拦重定向、不做只读白名单 —— `bash` 能跑任何东西。这是**刻意的**:
白名单挡不住 `&&` / `;` / `>` / 裸解释器这几种写法,所以它只会给一个“看起来有闸门”的错觉。

限制手段只有两档:

1. **工具级收窄**:`-t` 严格白名单 / `-xt` 排除 / `-nt` 全禁 / `-nbt` 只去内置;`settings.defaultTools` 只挑内置那档。
   想"连 bash 都不给"就别把 `bash` 列进白名单。见 [cli.md](cli.md)。
2. **把 qi 放进容器 / VM / 受限用户**(唯一真边界):见 [containerization.md](containerization.md)。

其它约定与现状:

- 文件工具的路径限制在**会话工作目录**内(`read` / `ls` / `find` / `grep` / `write` / `edit`),**bash 不受限**。
- `bash` / `powershell` 的子进程 stdin 是 `DEVNULL`(读不到 TUI 的按键);超时按**进程组**回收。
- TUI 里 `!` 手动命令不经任何过滤 —— 那是你亲手敲的,不算模型越权。
- **没有工具级审批弹窗**(那类 workflow 属于扩展或外部工具)。破坏性操作的交互前钩子(HITL)**未做**。

## 配置文件里的命令

`models.json` 的 `apiKey` 支持 `!command`(执行命令取 stdout)。它**不走 shell**(`shlex` 拆参 + `shell=False`),
只在你写的那份配置被解析时求值 —— 属于**你的配置信任域**,与 `auth.json` 同级;扩展或模型内容**无法**触发它。
三种写法见 [models.md](models.md)。

## 报漏洞 / 上报问题

qi 目前没有专门的安全上报渠道。**不做进程内沙箱是刻意的取舍**:半步隔离最容易被误当成安全边界 ——
要么按[隔离运行](containerization.md)那条路真隔离,要么就按“它有你进程的全部权限”来使用。
