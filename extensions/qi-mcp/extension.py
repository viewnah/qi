"""目录通道入口:把本目录软链/复制到 `~/.qi/agent/extensions/qi-mcp/` 也能加载。"""

from qi_mcp import register

__all__ = ["register"]
