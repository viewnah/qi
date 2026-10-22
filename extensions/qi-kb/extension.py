"""目录通道的入口（开发用）。

pip 通道走 `pyproject.toml` 的 entry point（`kb = "qi_kb:register"`）；这一份只给
**开发**用 —— 把本目录软链/复制到 `~/.qi/agent/extensions/qi-kb/`，然后 `qi kb …`
就能跑，不用先 `pip install`。

装载器会把本文件所在目录临时插进 `sys.path` 再 exec，所以 `import qi_kb` 在这里成立。
"""

from __future__ import annotations

from qi_kb import register

__all__ = ["register"]
