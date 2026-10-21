"""计划:把条目变成"要写哪里、会不会撞、还要补什么"。

**计划先行的意义不只是好看**:它把"会不会覆盖别人的东西"和"装完还要手工补什么"
提前到**动手之前**。多件一起算,是因为冲突是相对的 —— 一次装两个同名条目,
单独看每个都不冲突。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .actions import PatchJson, Planned, patch_current
from .catalog import StoreItem
from .errors import UsageError
from .kinds import get_kind
from .kinds.base import PlanContext
from .ledger import Ledger

__all__ = ["Plan", "Planned", "build", "scope_root"]


@dataclass
class Plan:
    items: list[Planned] = field(default_factory=list)
    skipped: list[tuple[StoreItem, str]] = field(default_factory=list)   # (条目, 原因)

    @property
    def conflicts(self) -> list[Planned]:
        return [planned for planned in self.items if planned.conflict]

    @property
    def needs_config(self) -> list[str]:
        seen: list[str] = []
        for planned in self.items:
            for name in planned.needs_config:
                if name not in seen:
                    seen.append(name)
        return seen


def build(items: list[StoreItem], ctx: PlanContext, *, ledger: Ledger) -> Plan:
    """逐条生成计划并标注冲突。**不做任何写操作**(skill 的 zip 下载是读)。"""
    plan = Plan()
    for item in items:
        record = ledger.find(item.id, ctx.scope)
        if record is not None and record.version == item.version:
            plan.skipped.append((item, f"已装且版本相同({item.version})"))
            continue
        planned = get_kind(item.kind).plan(item, ctx)
        planned.conflict = _conflict(planned, ledger)
        # 多件一起装时,后一个可能撞前一个(同名)。前一个还没落盘,所以这里自己再看一遍。
        for earlier in plan.items:
            if earlier.conflict is None and _same_key(earlier, planned):
                planned.conflict = "与本批另一个条目同名"
        plan.items.append(planned)
    return plan


def _same_key(a: Planned, b: Planned) -> bool:
    keys = {action.key for action in a.actions if isinstance(action, PatchJson)}
    return any(isinstance(action, PatchJson) and action.key in keys for action in b.actions)


def _conflict(planned: Planned, ledger: Ledger) -> str | None:
    """目标已存在且**不是本账本**写的 → 冲突。本账本的同名条目是"升级",不是冲突。"""
    for action in planned.actions:
        if isinstance(action, PatchJson):
            exists, _current = patch_current(action)
            if exists and not ledger.owns_patch(action.target, action.section, action.key):
                return f"{action.section}.{action.key} 已存在于 {action.target}"
            continue
        target: Path | None = getattr(action, "target", None)
        if target is not None and target.exists() and not ledger.owns_target(target):
            return f"{target} 已存在"
    return None


def scope_root(scope: str, cwd: Path | None) -> Path:
    from . import paths

    if scope == "global":
        return paths.global_agent_dir()
    if scope == "project":
        return paths.project_dir(cwd)
    raise UsageError(f"未知的作用域:`{scope}`", hint="可用:global / project")
