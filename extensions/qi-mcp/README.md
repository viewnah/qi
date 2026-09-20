# qi-mcp

qi 的 MCP 支持。形状**照搬 [`pi-mcp-adapter`](https://pi.dev/packages/pi-mcp-adapter)**
(见 `docs/extensions.md` E25)。

## 为什么默认只给一个代理工具

一个 MCP server 的工具定义轻松 **10k+ token**,连几个 server,对话还没开始就烧掉半个上下文
窗口 —— 而多数工具这次对话根本用不到。所以默认注册**一个约 200 token 的 `mcp` 代理工具**:
模型先 `search` 发现有什么,再 `call` 调。server 默认 **lazy**,真调用才启动。

## 装

```bash
pip install qi-mcp        # 或 -e ./extensions/qi-mcp(开发)
```

装完 `qi` 下次启动自动发现它(entry point `qi.extensions`)。

## 写声明表(qi 的两层)

格式与 v1 一致,只有一份真相:

```json
{
  "mcpServers": {
    "chrome-devtools": {
      "command": "npx",
      "args": ["-y", "chrome-devtools-mcp"]
    },
    "docs": { "url": "https://mcp.example.com/mcp" }
  }
}
```

| 层 | 路径 | 说明 |
| --- | --- | --- |
| `global` | `~/.qi/agent/mcp.json` | 用户级基建,跨项目 |
| `project` | `<git 根>/.qi/mcp.json` | 跟仓库走,可提交共享 → **同名覆盖全局** |

**第三层(角色私有 `<角色目录>/mcp.json`)由 qi-agents 读** —— 它把 server 定义按值交给
qi-mcp(E25:agent 目录是 qi-agents 的自包含包,"包的主人认识包成员")。所以这里只认两层。

TUI 里 `/mcp` 列出现有声明(带来源层与 disabled/directTools 标记)。

## 状态

| 切片 | 内容 | 状态 |
| --- | --- | --- |
| 1 | 两层声明表 + `/mcp` 面板 | ✅ |
| 2 | `mcp` 代理工具(search/describe/call)+ lazy 连接 | ⏳ |
| 3 | `directTools` 直连 + `includeTools`/`excludeTools` 通配 + `toolPrefix` | ⏳ |
| 4 | 给 qi-agents 的按值注入 API | ⏳ |

**尚未实现,别按它配置**:`lifecycle` / `idleTimeout` / `oauth` / `caFile` / 状态快照事件。
字段会被原样保留并在 `/mcp` 里标成"未识别字段",不会被判死(生态字段还在长)。

## 与 v1 的一处差别(待定)

v1 的模型更严:全局/项目两层只是**声明表**,哪个角色真能看到由 `agent.md` 的 `mcp_servers`
显式点名(凭证敏感 → 默认无)。E25 的模型是:角色的 server 集合 = 它自己的 `mcp.json` +
qi 两层,能不能调由 `tools:` 里有没有 `mcp` 决定 —— **不再要求逐 server 点名**。
要不要保留 v1 那道更严的闸,等切片 4 接线时再定。
