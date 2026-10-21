"""账本条目 vs 现实:**这条东西现在还在不在**。

为什么需要它:账本记的是"商店装过什么",而用户可以从**别处**把东西删掉 ——
设置面 A2A / MCP 两节自己的删除、手工编辑 `a2a.json`、`rm -rf` 一个技能目录、
甚至另一个工具改了同一个文件。那些路径都不经过 store,账本因此会撒谎:
商店页面还标着"已安装",`check` 还说"最新"。

**磁盘是事实来源,账本是记录。** 对不上就如实说"本地已被移除",而不是把账本当真。
"""

from __future__ import annotations

import json
from pathlib import Path

from .ledger import Record


def presence(record: Record) -> tuple[bool, str]:
    """`(还在不在, 不在了的原因)`。原因是一句人话,会直接出现在界面上。"""

    if record.files:
        paths = [Path(entry) for entry in record.files]
        missing = [path for path in paths if not path.exists()]
        if len(missing) == len(paths):
            return False, "文件都不在了"
        if missing:
            return False, f"缺 {len(missing)}/{len(paths)} 个文件"

    for patch in record.patches:
        file = Path(patch.file)
        if not file.is_file():
            return False, f"{file.name} 不存在"
        try:
            raw = json.loads(file.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False, f"{file.name} 读不了"
        section = raw.get(patch.section) if isinstance(raw, dict) else None
        if not isinstance(section, dict) or patch.key not in section:
            return False, f"{patch.section}.{patch.key} 已不在 {file.name}"

    # 既没有 files 也没有 patches 的记录只可能是手写的脏数据
    if not record.files and not record.patches:
        return False, "账本条目没有落点信息"
    return True, ""
