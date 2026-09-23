#!/usr/bin/env python3
"""发布前的一致性检查:core 与三个官方扩展必须**锁步同版本**。

```bash
python3 scripts/check_versions.py              # 只比四个包的 version
TAG=v0.1.0 python3 scripts/check_versions.py   # 顺便核 tag 与版本一致
```

为什么要有这一步:`qi-agent`(发行名 `qi-coding-agent`)、`qi-mcp`、`qi-agents`、`qi-web`
是**四个独立 distribution**(PyPI 上四个名字),但仓库约定锁步发版 —— `qi-agents` 依赖
`qi-mcp`,版本错位时用户会装到一个自己都没试过的组合。发布 workflow 在构建前跑它。
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TARGETS = [
    REPO / "pyproject.toml",
    REPO / "extensions" / "qi-mcp" / "pyproject.toml",
    REPO / "extensions" / "qi-agents" / "pyproject.toml",
    REPO / "extensions" / "qi-web" / "pyproject.toml",
]


def project_of(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)["project"]


def main() -> int:
    found: list[tuple[str, str, Path]] = []
    for path in TARGETS:
        if not path.is_file():
            print(f"缺少 {path.relative_to(REPO)}", file=sys.stderr)
            return 1
        project = project_of(path)
        found.append((str(project["name"]), str(project["version"]), path))

    for name, version, path in found:
        print(f"{name:20} {version}  ({path.relative_to(REPO)})")

    versions = {version for _name, version, _path in found}
    if len(versions) != 1:
        print(f"\n版本不一致:{sorted(versions)} —— 四个包必须锁步同版本", file=sys.stderr)
        return 1

    tag = (os.environ.get("TAG") or "").strip()
    if tag:
        want = tag[1:] if tag.startswith("v") else tag
        if want != versions.pop():
            print(f"\ntag {tag} 与包版本不一致(应为 {want})", file=sys.stderr)
            return 1
        print(f"\ntag {tag} 与包版本一致 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
