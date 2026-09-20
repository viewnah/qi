"""qi-mcp:qi 的 MCP 支持(形状照搬 `pi-mcp-adapter`,见 docs/extensions.md E25)。

分四个切片做,当前进度:

| 切片 | 内容 | 状态 |
| --- | --- | --- |
| 1 | **两层声明表 + `/mcp` 面板** | ✅ 本切片 |
| 2 | `mcp` 代理工具(search / describe / call)+ lazy 连接 | ⏳ |
| 3 | `directTools` 直连 + `includeTools`/`excludeTools`/`toolPrefix` | ⏳ |
| 4 | 给 qi-agents 的**按值注入** API(它读角色目录的 mcp.json 交给这里) | ⏳ |

为什么默认是**一个代理工具**而不是每 server 注册 N 个工具:`pi-mcp-adapter` 的实测理由是
**上下文** —— 一个 server 的工具定义轻松 10k+ token,连几个 server 就在对话开始前烧掉半个
上下文窗口。代理工具约 200 token,模型按需 `search` 发现、再 `call`。
"""

from __future__ import annotations

from typing import Any

from .config import (  # noqa: F401  (对外导出)
    MCP_FILE,
    ConfigError,
    McpScope,
    ServerSpec,
    active,
    load_layers,
    resolve,
    scope_files,
    unknown_fields,
)

__all__ = ["register", "resolve", "active", "load_layers", "scope_files",
           "ServerSpec", "ConfigError", "MCP_FILE", "McpScope"]


def register(api: Any) -> None:
    """装载 qi-mcp。

    本切片只接**配置层**:`/mcp` 列出两层声明表。**故意不调用 `provides_config("mcp_servers")`**
    —— 按 E25,"agent 目录里的 mcp.json"由 qi-agents 读,不是本扩展管,声明了反而是假的。
    """

    def on_command(args: str, ctx: Any) -> None:
        cwd = getattr(ctx, "cwd", None)
        specs = resolve(cwd)
        lines = [f"MCP 声明(共 {len(specs)} 个;层:全局 → 项目,同名项目覆盖)"]
        if not specs:
            lines.append(f"  (无)把一份 {MCP_FILE} 放到 ~/.qi/agent/ 或 <git 根>/.qi/")
        for spec in sorted(specs.values(), key=lambda s: s.name):
            flags = []
            if spec.disabled:
                flags.append("disabled")
            if spec.direct_tools:
                flags.append("directTools")
            unknown = unknown_fields(spec)
            if unknown:
                flags.append("未识别字段:" + ",".join(unknown))
            mark = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(f"  {spec.name}  ({spec.scope})  {spec.summary()}{mark}")
        lines.append("本切片尚未连接 server:`mcp` 代理工具与工具注册是切片 2/3")
        ctx.ui.notify("\n".join(lines))

    api.registerCommand("mcp", on_command, description="列出 MCP 声明表(全局/项目两层)")
