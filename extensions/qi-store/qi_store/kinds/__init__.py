"""kind 适配器登记处。

一个 kind 只需三样:`fetch`(列)、`plan`(转成落盘动作)、可选 `download`(只取件)。
四类都实现了:`skill` / `mcp` / `a2a` / `agent`。

`NOT_IMPLEMENTED` 留在这里是给**下一个** kind 的位置(比如上游 PEP 503 索引上的
`extension`)。"报个 P1"和"讲清为什么不装"对使用者是两件事,所以这张表里每一条都带理由。
"""

from __future__ import annotations

from types import ModuleType

from ..errors import UsageError
from . import a2a, agent, mcp, skill

KINDS: dict[str, ModuleType] = {"agent": agent, "skill": skill, "mcp": mcp, "a2a": a2a}

#: 认识但**故意不实现**的 kind(每条都要写清"为什么不做",界面会原样显示)
NOT_IMPLEMENTED: dict[str, str] = {}

ALL_KINDS = (*KINDS, *NOT_IMPLEMENTED)


def get_kind(name: str) -> ModuleType:
    if name in KINDS:
        return KINDS[name]
    if name in NOT_IMPLEMENTED:
        raise UsageError(NOT_IMPLEMENTED[name])
    known = "、".join(ALL_KINDS)
    raise UsageError(f"不认识的 kind:`{name}`", hint=f"可用:{known}")
