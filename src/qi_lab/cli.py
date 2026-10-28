"""`qi-lab` 命令入口。

当前只是把参数交给 `serve`;单独一层是为了以后加子命令(doctor / extensions)时
不必改 `[project.scripts]`。
"""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    from .serve import main as serve_main

    return serve_main(argv)
