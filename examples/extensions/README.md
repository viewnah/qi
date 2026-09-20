# 扩展示例

这里放**能直接抄**的最小示例。每个目录都是一个可以整份复制走的扩展。

| 目录 | 演示什么 |
| --- | --- |
| [`hello/`](hello/) | 最小形状:一个 `extension.py`,把工具 / 斜杠命令 / CLI 子命令 / 旗标 / 事件各走一遍 |
| [`../agents/`](../agents/) | 角色(`agent.md`)长什么样 —— 那是 **qi-agents** 的数据,不是 core 的概念 |

## 三种扩展形态

| 形态 | 入口 | 适合 |
| --- | --- | --- |
| **目录形态** | `<项目>/.qi/extensions/<名>/extension.py` 或 `~/.qi/agent/extensions/<名>/extension.py` | 自己用、快速试;**项目级要在信任的项目里才装载** |
| **包形态** | `pyproject.toml` 里声明 `qi.extensions` entry point,`pip install` 之后自动发现 | 要发布/共享(三个官方扩展都是这种) |
| **临时形态** | `settings.json` 的 `extensions: ["/路径"]` | 开发调试(不写进扩展目录) |

## 相关文档

- `docs/extensions.md` —— 完整 API、中间件链语义、信任门控、决策记录(E1–E25)
- `docs/settings.md` —— `settings.json` 里的 `extensions[]` / `defaultProjectTrust`
- `qi doctor` —— 装了哪些扩展、各自注册了什么、有没有依赖警告
