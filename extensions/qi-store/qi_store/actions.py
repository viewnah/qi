"""落盘动作。

安装不是"往目录里拷文件",而是**三种形状**的动作:

| 动作 | 用于 | 冲突判定的对象 |
| --- | --- | --- |
| `WriteText` / `WriteBytes` | 单文件 | 目标是不是**本账本**写过的 |
| `ExtractZip` | skill 包(目录) | 目标目录是不是本账本装的 |
| `PatchJson` | `mcp.json` / `a2a.json` 里的**一个键** | 那个键在不在、是不是本账本写的 |

第三种是关键:装一个 MCP server **不能覆盖别人的 server**,也不能在卸载时把整个
`mcp.json` 删掉。所以冲突与卸载都按"键"而不是"文件"算 —— 用户的其它配置必须原样活着。
"""

from __future__ import annotations

from typing import Any

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union


@dataclass(frozen=True)
class WriteText:
    target: Path
    text: str


@dataclass(frozen=True)
class WriteBytes:
    target: Path
    data: bytes


@dataclass(frozen=True)
class ExtractZip:
    """把 zip 解到一个目录。`strip_top=True` 时去掉上游的顶层目录(skill 包里是技能名)。"""

    target: Path
    data: bytes
    strip_top: bool = True


@dataclass(frozen=True)
class PatchJson:
    """只动 `{section: {key: value}}` 里的一个键。文件其余部分原样保留。"""

    target: Path
    section: str
    key: str
    value: dict


Action = Union[WriteText, WriteBytes, ExtractZip, PatchJson]


@dataclass
class Planned:
    """一个条目的安装计划。`item` 不写具体类型以免和 catalog 成环。

    只在这里定义一份:kinds 生成它、planner 标冲突、installer 执行它。
    """

    item: Any
    target: Path
    actions: list[Action]
    needs_config: list[str] = field(default_factory=list)
    note: str = ""
    #: 目标已存在(且不是本账本装的)—— `--force` 才继续
    conflict: str | None = None


def describe(action: Action) -> str:
    if isinstance(action, ExtractZip):
        return f"解压到 {action.target}"
    if isinstance(action, PatchJson):
        return f"{action.target} → {action.section}.{action.key}"
    return f"写入 {action.target}"


def patch_current(action: PatchJson) -> tuple[bool, object]:
    """`(键是否已存在, 当前值)`。文件不存在或不是 JSON 都当"不存在"处理。"""
    if not action.target.is_file():
        return False, None
    try:
        raw = json.loads(action.target.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False, None
    section = raw.get(action.section) if isinstance(raw, dict) else None
    if not isinstance(section, dict) or action.key not in section:
        return False, None
    return True, section[action.key]
