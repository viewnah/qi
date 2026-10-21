"""kind 适配器登记处。

一个 kind 只需三样:`fetch`(列)、`plan`(转成落盘动作)、可选 `download`(只取件)。
`agent` 是**留槽未实现**(上游蓝图与 qi 的角色目录格式尚未对齐) —— 单独给出理由,
因为"报个 P1"和"讲清为什么不装"对使用者是两件事。
"""

from __future__ import annotations

from types import ModuleType

from ..errors import UsageError
from . import a2a, mcp, skill

KINDS: dict[str, ModuleType] = {"skill": skill, "mcp": mcp, "a2a": a2a}

#: 认识但**故意不实现**的 kind
NOT_IMPLEMENTED = {
    "agent": (
        "agent 这一类暂不实现:hikqin 的模板蓝图(format_version 1,skill 文件 base64 内联、"
        "model/api_key 被剥离)与 qi 的角色目录(agent.md + 私有 mcp.json + 私有 skills/)"
        "格式尚未对齐,装下去也跑不起来"
    ),
}

ALL_KINDS = (*KINDS, *NOT_IMPLEMENTED)


def get_kind(name: str) -> ModuleType:
    if name in KINDS:
        return KINDS[name]
    if name in NOT_IMPLEMENTED:
        raise UsageError(NOT_IMPLEMENTED[name])
    known = "、".join(ALL_KINDS)
    raise UsageError(f"不认识的 kind:`{name}`", hint=f"可用:{known}")
