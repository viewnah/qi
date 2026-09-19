# qi-agents

qi 的**角色系统**扩展:角色发现 + 角色选择 + `subagent` 委派。

它原本住在 core 里(P-E4c 移出,见 `docs/extensions.md` §E14/E15)。现在 core 只认识运行单元
`{name, prompt, tools}`,「角色」这个概念由本扩展提供 —— 只用 qi 的公开扩展面,core 一行不用改。

## 装

```bash
pip install qi-agents            # 或 -e ./extensions/qi-agents(开发)
```

装完 `qi` 下次启动就会自动发现它(entry point `qi.extensions`)。

## 写一个角色

一个目录一份 `agent.md`,frontmatter 声明元信息,**正文就是那个角色的系统提示词**:

```markdown
---
name: reviewer
description: 只读审查员,专挑缺陷;不改代码
tools: read, grep, glob
model: anthropic/claude-sonnet-4
---

你是一名严格的代码审查员。只读代码,指出具体缺陷与风险,给最小修法。
不要重写整个文件,不要改动任何东西。
```

放哪儿:

| 位置 | 作用域 | 说明 |
| --- | --- | --- |
| `~/.qi/agent/agents/<名>/agent.md` | `user` | 跨项目,你自己的常用角色 |
| `<项目>/.qi/agents/<名>/agent.md` | `project` | 跟仓库走;**同名覆盖**用户级 |

`tools` 支持 `read, grep`(逗号)或 `[read, grep]`(列表);省略 = **继承父**(不是"全部工具"——
工具省略若解释成"全部",一个角色就能悄悄拿到比父更多的权限,那是一条提权路径)。
`description` 必填:模型靠它挑角色。

## 用

```bash
qi --ext agent=reviewer "审一下 src/ 里的错误处理"     # 以此角色运行主会话
```

```bash
qi "看看这个模块"                                       # 用 subagent 工具把活派出去
> 你: 让 scout 查一下这个函数的调用方
```

`subagent` 工具三种模式:

```jsonc
{"agent": "scout", "task": "查调用方"}                       // single
{"tasks": [{"agent": "scout", "task": "A"}, …]}              // parallel(并发 4,最多 8 个)
{"chain": [{"agent": "scout", "task": "先查"},
           {"agent": "writer", "task": "基于 {previous} 写文档"}]}   // chain:上一棒输出代入 {previous}
```

`agentScope` 参数决定看哪层角色(`user` 默认 / `project` / `both`)。
**项目级角色是仓库控制的提示词**,所以走项目层时要显式指定,且项目未被信任时会先问一句。

TUI 里 `/agents` 列角色(带 `both` 看项目级)。

## 为什么子 agent 是**进程内**的

`api.runAgent` 在本进程里跑受管子运行,不是起子进程。Python 每进程首次调用 LLM 要付
litellm 的导入成本(实测 ~6.8s 冷热稳定),子进程等于 6.8s × N(8 个并行 ≈ 54s);
Node 起一次只要 0.04s,所以 pi 的子进程模型不能平移到这里。代价是子运行与主会话共享进程
(异常/内存不隔离),收益是并行委派真的能用。

**递归防护是结构性的**:子运行的工具清单里去掉 `subagent` 本身,所以子角色不可能再起子角色。

## 已知限制

- 子运行的中间过程不回流(`on_event` 未接):界面只看到"子 agent 在跑"和最终结果,
  看不到它内部每次工具调用。要做得先定渲染契约。
- 角色的 `model` 走 `provider/model` 字符串;没有按角色的温度/最大轮数等更细的设置。
- `/agents` 是只读列表,没有交互式挑选(pi 有 `/agents` 面板)。
