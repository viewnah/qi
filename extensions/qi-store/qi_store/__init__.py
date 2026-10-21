"""qi-store:把 hikqin 当成应用商店。

P0 = 登录链路(`login` / `logout` / `whoami` / `sources`)。取货(`install` / `download` / …)
在 P1 —— 命令名已经占好位置,调用时明确报"P1 未实现"。

装载入口只有一句:**只注册 CLI 子命令**,别的什么都不做。`register` 要薄
([docs/extensions.md](../../../docs/extensions.md) §2:装载失败是整体中断),
工具与事件等 P1 真的需要时再加。
"""

from __future__ import annotations

from typing import Any

from .cli import main

__all__ = ["register", "main"]


def register(api: Any) -> None:
    api.register_cli_command(
        "store",
        main,
        description="从 hikqin 应用商店安装 MCP / Skill / A2A(dev qi-store)",
    )
