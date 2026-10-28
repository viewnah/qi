#!/usr/bin/env python3
"""构建前的一致性检查:qi-lab 与内置扩展必须使用同一个 bundle 版本。

```bash
python3 scripts/check_versions.py
TAG=v0.4.0 python3 scripts/check_versions.py    # 顺便核 tag 与版本一致
```

为什么不再要求 core 锁步:`qi-coding-agent` 已经独立发版,qi-lab 只声明兼容范围
(`qi-coding-agent>=0.4,<0.5`)。内置扩展随 qi-lab wheel 一起分发,它们的
`pyproject.toml` 只在未来提取发布时用到,所以 bundle 阶段保持同版本即可。
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LAB = REPO / "pyproject.toml"
EXTENSIONS = [
    REPO / "extensions" / name / "pyproject.toml"
    for name in ("qi-mcp", "qi-agents", "qi-a2a", "qi-store", "qi-kb", "qi-assistants")
]


def project_of(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)["project"]


def main() -> int:
    targets = [LAB, *EXTENSIONS]
    found: list[tuple[str, str, Path]] = []
    for path in targets:
        if not path.is_file():
            print(f"缺少 {path.relative_to(REPO)}", file=sys.stderr)
            return 1
        project = project_of(path)
        found.append((str(project["name"]), str(project["version"]), path))

    for name, version, path in found:
        print(f"{name:20} {version}  ({path.relative_to(REPO)})")

    versions = {version for _name, version, _path in found}
    if len(versions) != 1:
        print(f"\n版本不一致:{sorted(versions)} —— qi-lab 与内置扩展必须同版本", file=sys.stderr)
        return 1
    version = versions.pop()

    lab = project_of(LAB)
    deps = [str(dep) for dep in lab.get("dependencies", [])]
    if not any(dep.startswith("qi-coding-agent") for dep in deps):
        print("\nqi-lab 的 dependencies 里必须声明 qi-coding-agent 的兼容范围", file=sys.stderr)
        return 1

    tag = (os.environ.get("TAG") or "").strip()
    if tag:
        want = tag[1:] if tag.startswith("v") else tag
        if want != version:
            print(f"\ntag {tag} 与包版本不一致(应为 {want})", file=sys.stderr)
            return 1
        print(f"\ntag {tag} 与包版本一致 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
