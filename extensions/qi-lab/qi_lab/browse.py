"""服务器目录浏览(给「添加工作区」的目录选择器用)。

形状照 **pi-web** 的 `GET /api/cwd/browse`(实测自
`@agegr/pi-web/.next/server/app/api/cwd/browse/route.js`):回
`{path, parentPath, directories, drives}`,而不是"回一棵树"或"回文件列表"。

三条刻意的取舍:

1. **只列目录,不列文件。** 选择器的用途是"在哪建一条会话",文件名只会是噪音;
   而且少回一类信息,少一类泄露面。pi-web 的文件列表在另一个端点(`/api/files`),
   qi 没有,也不需要。
2. **路径先 `expanduser` + `realpath`。** `~` 与 `~/x` 展开到 home,`..`/软链归一到真实路径 ——
   否则同一个目录会被点出两种写法,而 qi 的"项目"是按 `cwd` 字符串分组的(见
   `web/src/state/projects.ts`),两种写法就是两个项目。
3. **符号链接指向目录也算目录。** 开发者常把源码放在软链后面;`os.scandir` 的
   `is_dir()` 会跟随软链(而 `is_dir(follow_symlinks=False)` 不会),这里要前者。

安全性:这个端点**只读、只列目录**,不读文件内容、不写、不删。权限面比已有的 `bash` 工具
(能跑任意命令)小得多,所以它沿用同一道 `guard`(回环 + 可选口令)即可,不需要额外的信任模型。
"""

from __future__ import annotations

import os
import string
from pathlib import Path

def home_dir() -> Path:
    """`~` 与 `~/x` 的展开基准。抽成函数是为了测试能替换它(不依赖真实 home)。"""
    return Path.home()


def resolve_dir(raw: str | None) -> Path:
    """把用户输入的路径解析成绝对真实路径。

    `raw` 为空 → home(前端首屏就是这样:不给 `path`,直接落在主目录)。
    """
    text = (raw or "").strip()
    if text == "~":
        target = home_dir()
    elif text.startswith("~/"):
        target = home_dir() / text[2:]
    else:
        target = Path(text) if text else home_dir()
    # `realpath` 而不是 `resolve()`:前者不要求路径存在,后者在旧 Python 上语义同样成立,
    # 但 `realpath` 对"存在但没权限"的中间目录更宽容(它只做字符串级归一 + 软链展开)。
    return Path(os.path.realpath(target.expanduser()))


def windows_drives() -> list[dict[str, str]]:
    """Windows 盘符列表(NT 之外回空 —— pi-web 也是只在 win32 上给 `drives`)。"""
    if os.name != "nt":
        return []
    out = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.isdir(root):
            out.append({"name": f"{letter}:", "path": root})
    return out


def parent_of(path: Path) -> str | None:
    """上一级;已经在根上时回 `None`(前端据此禁用"上级"按钮)。"""
    parent = path.parent
    return None if parent == path else str(parent)


def list_directories(raw: str | None) -> list[dict[str, str]]:
    """列 `raw` 下的直接子目录,按名字排序(与 pi-web 的 `localeCompare` 同义)。"""
    root = resolve_dir(raw)
    # 不做 `is_dir()` 预判:让 `os.scandir` 自然抛错(文件 → NotADirectoryError、
    # 不存在 → FileNotFoundError、没权限 → PermissionError),端点据此回
    # 400 / 404 / 403 —— pi-web 也是这么分的"不存在"与"不是目录"两种情况。
    entries: list[dict[str, str]] = []
    with os.scandir(root) as scan:
        for item in scan:
            # `is_dir()` 跟随软链:指向目录的软链算目录(开发者常这么放源码)。
            try:
                if not item.is_dir():
                    continue
            except OSError:
                continue  # 断掉的软链 / 权限不足:跳过这一条,不让整次列举失败
            entries.append({"name": item.name, "path": str(root / item.name)})
    entries.sort(key=lambda entry: entry["name"].lower())
    return entries
