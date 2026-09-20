"""PEP 723 的内联脚本元数据(`# /// script` 块)。

目录通道的扩展**没有 dist 元数据** —— 它不是装上去的,是仓库/用户目录里的一个脚本。所以它的
依赖声明走 PEP 723:在 `extension.py` 里写一个 `# /// script` 块。解析结果用于两件事:

- **宿主依赖检查**(E11:把 `qi-agent` 写进依赖要报出来)—— 与 pip 通道**对称**,不再只查装上的;
- `qi doctor` 展示声明(§5.5:声明仍然要做)。

**只解析,不安装**(安装是 `qi install` 的事,目标仍是**同一个解释器环境**,见 E13)。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

#: 块头/块尾的形状(`# /// <type>` 与 `# ///`)。
_OPEN = re.compile(r"^# /// ([a-zA-Z0-9-]+)\s*$")
_CLOSE = "# ///"

#: 目录通道的固定入口名(§E9:一个目录一个扩展)。
ENTRY_FILE = "extension.py"


def parse_inline_metadata(text: str) -> dict[str, dict]:
    """解析所有 `# /// <type>` 块 → `{type: TOML}`。

    **逐行扫描,而不是用规范里给的那个正则**:那个正则是"一个块"的形状,而它的内容组
    (`(^#(| .*)$\s)+`)在**相邻两个块**之间是贪婪的 —— 第二块的行也都以 `#` 开头,于是
    它会把两块合成一段去解析 TOML,失败后**整块被跳过**(现象是"文档里明明写了两块,
    解析结果却是空的")。逐行扫描没有这个问题,也更好读。

    **一块写坏只跳过那一块**:这层是辅助信息(依赖声明),不该因为一个 TOML 语法错误让
    整个扩展装载不了 —— 装载本身有它自己的报错路径。
    """
    out: dict[str, dict] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        opened = _OPEN.match(lines[index])
        if opened is None:
            index += 1
            continue
        kind = str(opened.group(1))
        body: list[str] = []
        index += 1
        while index < len(lines) and lines[index].strip() != _CLOSE:
            if lines[index].startswith("#"):
                body.append(lines[index][1:].lstrip(" "))   # 去掉一个 `#` 与紧随的空格
            index += 1
        index += 1                                          # 跳过结束标记
        try:
            out[kind] = tomllib.loads("\n".join(body))
        except tomllib.TOMLDecodeError:
            continue
    return out


def dependencies_of(script: Path | str) -> list[str]:
    """一个脚本(或扩展目录)声明的依赖 —— `script` 块里的 `dependencies`。

    收目录时自动找入口文件(`extension.py`)。没有块、文件读不到、字段不是列表 → `[]`。
    """
    path = Path(script)
    if path.is_dir():
        path = path / ENTRY_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    block = parse_inline_metadata(text).get("script") or {}
    raw = block.get("dependencies")
    return [str(item) for item in raw] if isinstance(raw, list) else []
