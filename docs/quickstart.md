# 快速开始

从零到第一次对话:装 → 配模型 → 跑。

## 1. 装

qi 需要 **Python ≥ 3.12**。

**目前以从源码安装为主**——仓库里没有发布配置(无 CI、无 PyPI 上传),所以不要假设
`pip install qi-agent` 能装到东西(那个名字是[设计里写的目标形态](../design/PLAN.md),不是现状)。

```bash
git clone https://github.com/viewnah/qi.git
cd qi
uv sync                 # 或用你自己习惯的方式装进某个 venv:
                        #   python -m venv .venv && .venv/bin/pip install -e .
```

装完 `qi` 命令可用(`[project.scripts]` 里的入口):

```bash
uv run qi --version     # 或激活 venv 后直接 `qi --version`
```

首次运行会创建 `~/.qi/agent/`(用户级状态都挂在这里:配置、会话、技能、扩展)。

## 2. 配一个模型

qi 不内置任何 provider 就必须先配一个能用的模型。交互式引导:

```bash
qi init
```

它会走三步(样式复刻 QwenPaw 的 `init`):**选 provider**(已有 / 新建 → Base URL → API 类型 → API Key)
→ **Add Models**(`Add a model?` 循环,含 reasoning / contextWindow / maxTokens)
→ **选一个默认模型**。产物是三个文件:`models.json`(provider 与模型清单)、
`settings.json`(默认模型)、`auth.json`(凭证,0600)。

CI / 脚本里不想交互就一行:

```bash
qi init -y --provider deepseek --model deepseek-chat \
    --base-url https://api.deepseek.com/v1 --api openai-completions --api-key sk-xxx
```

> 更细的字段与手工写法见 [model-config.md](model-config.md);凭证的解析顺序、`apiKey` 的
> `!command` / `$ENV` 写法见 [providers.md](providers.md)。

## 3. 验一下

```bash
qi doctor
```

它逐项报告:读到了哪些配置文件、装了哪些扩展、每个 provider 的**凭证状态**(`OK` / `缺密钥`,
以及来源)、默认模型。缺东西时它会给出可复制的修复命令。

这一步别跳过——"配了但没生效"几乎都能在这里一眼看出来(尤其是凭证:显示 `缺失(env:OPENAI_API_KEY(未设置))`
这种带括号说明的,就是"找到了来源但值为空")。

## 4. 跑起来

三种形态,按用途选:

```bash
qi                          # 交互界面(TUI)。裸 `qi` 就是这个
qi "分析这个仓库"            # 进 TUI,并把这句话作为首条消息发出(对齐 pi 的 `pi "问题"`)
qi -p "分析这个仓库"         # 无头一次执行,只输出答案(脚本用)
qi --mode json "分析这个仓库" # 事件流(一行一个 JSON 对象),给管道/上游服务用
```

两者区别:`-p` 的 stdout **只有答案**(进度走 stderr,`--verbose` 才显示);`--mode json` 输出全部
事件,见 [json.md](json.md)。

想让它问一句答一句、边看边改 → 用 TUI;想拿结果喂给别的程序 → 用 `-p` 或 `--mode json`。

## 5. 接着往下

| 想做 | 去哪 |
| --- | --- |
| 下次接着这条会话 | `qi -c`(最近一条)或 `qi --session <id>` —— [sessions.md](sessions.md) |
| 换模型 / 换思考级别 | TUI 里换,或 `--thinking <级别>` —— [tui.md](tui.md) |
| 让它按某个角色干活 | 装 **qi-agents** 扩展 —— [agent-config.md](agent-config.md) |
| 加自己的技能 | 写 `SKILL.md` 放进 `~/.qi/agent/skills/` —— [skills.md](skills.md) |
| 改系统提示词 | 写 `.qi/SYSTEM.md`(**整体替换**默认基座,注意副作用) —— [system-prompt.md](system-prompt.md) |
| 写扩展 | [extensions.md](extensions.md) |
| 换个配色 | `qi config --set theme=dark` 或 `QI_THEME=light qi` —— [themes.md](themes.md) |

## 6. 零配置也能跑什么

裸 core **不带角色**:它就是一个 agent,一份**代码内默认基座提示词**,加上内置工具
(read / ls / find / grep / write / edit / bash / powershell)。所以不装任何扩展也能正常干活 ——
角色、MCP、Web UI 都是**可选扩展**(`qi-mcp` / `qi-agents` / `qi-web`)。

装上扩展后 `qi doctor` 的"扩展"一节会列出它们;`qi list` 会把**已装**与 `settings.packages` 里
**声明**的扩展做双向比对(声明了没装 / 装了没声明),见 [cli.md](cli.md) §4。
