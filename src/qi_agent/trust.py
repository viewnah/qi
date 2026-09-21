"""按**目录**记住信任决定(`~/.qi/agent/trust.json`)—— 对齐 pi。

两个要点(都是 pi 文档里的原话):

1. **存在用户 home,不在仓库里。** 仓库不能为仓库自己背书 —— 否则一行
   `.qi/settings.json` 就能让自己的任意代码跑起来(那个漏洞真存在过,见
   `tests/test_trust_scope.py`)。
2. **沿祖先链查,最近的那条先生效**:pi 的 "the closest saved decision on the current or
   parent path applies before the global default"。也就是说只轮到 `defaultProjectTrust`
   是在**没有任何已存决定**的时候。

`/trust` 连带记住**上一层目录**(pi 的 `/trust` 明确这么做:同一条路径下的平级项目都会被覆盖),
并且**当前会话不重载** —— 写完要重启才生效(pi 同样如此,见 `docs/security.md`)。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

TRUST_FILE_NAME = "trust.json"


def trust_file(path: Path | None = None) -> Path:
    """信任库的位置:`~/.qi/agent/trust.json`(可用 `QI_AGENT_HOME` 覆盖那一层)。"""
    if path is not None:
        return path
    from .paths import global_home

    return global_home() / TRUST_FILE_NAME


def _canonical(path: Path | str) -> str:
    """规范目录 —— 决定按它存(pi 用的也是 canonical directory)。"""
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(Path(path).expanduser())


class TrustStore:
    """`~/.qi/agent/trust.json`:`{规范目录: true|false}`。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = trust_file(path)

    # ── 读 ────────────────────────────────────────────────────────────
    def load(self) -> dict[str, bool]:
        """读整表。坏文件当"没有决定"—— 启动路径上不能因此崩(它只影响信任,不影响别的)。"""
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): bool(value) for key, value in data.items() if isinstance(value, bool)}

    def get(self, cwd: Path | str) -> tuple[bool, str] | None:
        """当前目录或**父目录**上最近的决定 → `(trusted, 命中的那个目录)`;没有则 None。"""
        table = self.load()
        here = Path(_canonical(cwd))
        for candidate in (here, *here.parents):
            found = table.get(str(candidate))
            if found is not None:
                return found, str(candidate)
        return None

    def decisions(self) -> dict[str, bool]:
        """全部决定(给 `/trust` 或诊断用),按路径排序。"""
        return dict(sorted(self.load().items()))

    # ── 写 ────────────────────────────────────────────────────────────
    def set(self, cwd: Path | str, trusted: bool, *, parent: bool = False) -> Path:
        """记住某个目录的决定;`parent=True` 时**连带上一层**(pi 的 `/trust` 行为)。

        写盘用「临时文件 + `os.replace`」并置 0600:这是个安全相关的文件,
        半个写坏的信任库比没有更糟(读的时候会被当成"没有决定",于是静默失效)。
        """
        table = self.load()
        target = Path(_canonical(cwd))
        table[str(target)] = bool(trusted)
        if parent and target.parent != target:
            table[str(target.parent)] = bool(trusted)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(table, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        return self.path

    def forget(self, cwd: Path | str) -> bool:
        """删掉某个目录的决定(注意:上层若还有决定,它照样生效)。"""
        table = self.load()
        if table.pop(_canonical(cwd), None) is None:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(table, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        return True
