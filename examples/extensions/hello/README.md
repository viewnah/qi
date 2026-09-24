# hello —— 最小扩展示例

一份 `extension.py` 里把**主要的面各走一遍**:工具 / 斜杠命令 / CLI 子命令 / 旗标 / 事件。
代码里的注释说明各自是什么、`ctx` 上有什么。完整 API 与设计取舍见
[`docs/extensions.md`](../../../docs/extensions.md)。

## 跑起来

```bash
# 用户级(跨项目)
mkdir -p ~/.qi/agent/extensions/hello
cp extension.py ~/.qi/agent/extensions/hello/

# 或项目级(跟仓库走;项目未信任时不装载)
mkdir -p .qi/extensions/hello && cp extension.py .qi/extensions/hello/
```

然后:

| 试什么 | 怎么试 |
| --- | --- |
| 工具 | 让模型"打个招呼"(它会调 `greet`) |
| 斜杠命令 | TUI 里输入 `/hello 你好呀` |
| CLI 子命令 | `qi hello-cli --any 参数`(参数原样交给扩展) |
| 旗标 | `qi --ext greet=张三 "打个招呼"` |
| 事件 | 起一个会话,启动提示里会出现一行"hello 扩展已装载" |
| 诊断 | `qi doctor` —— 会列出 hello 与它注册了什么 |

## 这个目录长什么样

```
hello/
└── extension.py     ← 唯一必需的文件(入口名固定)
```

一个扩展就是一个目录 + 一个 `extension.py`(里面必须有 `register(api)`)。
包形态(要发到 PyPI 那种)见 `extensions/` 下的三个扩展:它们用
`pyproject.toml` 的 `qi.extensions` entry point 声明入口,目录里放一个真正的 Python 包。

## 想抄更完整的

- **工具 + 事件 + 委派**:`extensions/qi-agents/`(角色发现 + `subagent` 工具)
- **能力交接 + 外部协议**:`extensions/qi-mcp/`(MCP 客户端 + 按值注入)
- **HTTP 宿主 + 前端**:`extensions/qi-web/`(还演示了 `registerCliCommand` 的真实用法)
