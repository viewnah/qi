"""工作区的显示名覆盖(改过名的那几个目录)。

qi 的「项目」(左栏一级)不是实体,而是会话 `cwd` 的**派生**(见 `web/src/state/projects.ts`)。
只有一件事派生不出来,所以记在这里:**改过的显示名** —— 目录名未必是你想看到的项目名
(`~/src/dsh-client` 想显示成「dsh」)。

**这个文件不记录"工作区名单"**,因为名单就是"哪些目录下还有会话":某个目录的会话被删光,
它在左栏自然就消失了,不需要另记一笔"我删过它"。这也正是"删除工作区 = 连同会话一起删"
这条语义带来的简化 —— 早先那版照着 dsh 做"保留会话、落到未分组",于是必须再存一个
`ungrouped` 集合来压住"删过的目录又冒出来";现在没有这个需要了,那个集合连同它的
`regroup()` 一起删掉(留着的死概念比没有概念更坏)。

代价写清楚:**删除工作区是不可恢复的**(会话文件被 unlink)。确认弹层因此必须把条数
和后果写出来,而不是让人猜。目录本身不删 —— qi 不动用户的文件夹。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

#: 落在 `~/.qi/agent/` 下,与会话(按项目分组的来源)同一个挂载点。
FILE_NAME = "workspaces.json"


def normalize(cwd: str | Path) -> str:
    """目录 → 规范化键。

    必须与 `SessionStore.create()` 写进 header 的那一套同源(`expanduser().resolve()`),
    否则同一个目录会有两种写法(`/tmp/x` 与 `/private/tmp/x`),于是分成两组 ——
    macOS 上这不是假设,是必然。
    """
    return str(Path(cwd).expanduser().resolve())


@dataclass
class WorkspaceNames:
    """目录 → 改过的显示名。没有条目的目录就用目录名。"""

    names: dict[str, str] = field(default_factory=dict)

    def name_of(self, cwd: str | Path) -> str | None:
        """该目录的显示名覆盖;没有则 `None`(调用方回落到目录名)。"""
        return self.names.get(normalize(cwd))


class WorkspaceStore:
    """`workspaces.json` 的读写。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else paths.global_home() / FILE_NAME

    def load(self) -> WorkspaceNames:
        """读。

        文件损坏、字段缺失、类型不对一律**当空**处理:这是 UI 偏好,不是数据源。
        为它抛异常会让 `qi web` 整个起不来,而代价只是"分组回到目录名"。
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return WorkspaceNames()
        if not isinstance(raw, dict):
            return WorkspaceNames()
        raw_names = raw.get("names")
        names = {}
        if isinstance(raw_names, dict):
            for key, value in raw_names.items():
                if isinstance(key, str) and isinstance(value, str) and value.strip():
                    names[normalize(key)] = value.strip()
        return WorkspaceNames(names=names)

    def save(self, data: WorkspaceNames) -> None:
        """原子写:先写同目录临时文件再 `os.replace`,不会读到半截文件。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "names": dict(sorted(data.names.items()))}
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".workspaces-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def rename(self, cwd: str | Path, name: str) -> WorkspaceNames:
        """改显示名(**不碰目录**)。空名 = 恢复成目录名。"""
        data = self.load()
        key = normalize(cwd)
        label = name.strip()
        if label:
            data.names[key] = label
        else:
            data.names.pop(key, None)
        self.save(data)
        return data

    def forget(self, cwd: str | Path) -> WorkspaceNames:
        """忘掉该目录的显示名。

        只清名字,**不碰会话文件** —— 会话的删除是 API 层的事(`DELETE /api/workspaces`
        先删这个目录下的会话,再调这里)。分开的理由:这个文件是偏好,
        而"删哪些会话"要看会话存储,两者不该互相认识。
        """
        data = self.load()
        data.names.pop(normalize(cwd), None)
        self.save(data)
        return data
