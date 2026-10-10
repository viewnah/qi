"""会话目录内的**文件浏览与预览**(右侧文件面板用)。

## 边界:与文件工具**同一条**

路径必须落在会话的 cwd 内。这里的 `guard()` 是从 `tools/__init__.py` 的
`ToolContext.guard` 抄来的,连细节都一致(`expanduser` → `resolve(strict=False)` →
必须等于 root 或在 root 之下)。**同一份数据不该有两个边界** —— 工具读不到的东西,
界面也不该读到;否则"前端有个口子"就会变成绕过 `tools` 限制读全盘。

`resolve()` 会跟随软链,所以**指向外面的软链连它自己都被拒**(不是"穿过它才拒"):
那扇门本身就通向外面。这条比字符串前缀比较强得多,而且与文件工具的 `guard` 完全一致
(同一份实现)。

## 三种读法,各有上限

| 端点用途 | 函数 | 上限 | 说明 |
| --- | --- | --- | --- |
| 列一层目录 | `list_dir` | — | 只列**一层**(前端懒展开);目录在前、名字用数字感知排序 |
| 文本预览 | `read_text` | 512 KB | 超过就截断并标记;含 NUL 或非 UTF-8 视为二进制,不硬解 |
| 原字节预览 | `read_bytes` | 8 MB | 只给**白名单**里的类型(图片 / PDF / HTML);HTML 由调用方加沙箱响应头 |

白名单是安全设计的一部分,不是顺手:内容是**用户自己的文件**,但浏览器渲染它时
会把它当成本站的文档 —— 上限越窄,能借它做的事越少。`svg` 刻意**不在**图片白名单里
(它是可执行文档),它走文本预览(当代码看),既安全又更有用。
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

#: 文本预览上限(字符)。超过就只回前一段并标 `truncated`。
TEXT_MAX_CHARS = 512_000
#: 原字节预览上限。图片/PDF 通常远小于它;超了宁可回"太大"也不让浏览器去吞。
RAW_MAX_BYTES = 8 * 1024 * 1024

#: 可以**原字节**预览的类型。刻意收窄(见模块注释)。
RAW_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".avif": "image/avif",
    ".pdf": "application/pdf",
    ".html": "text/html",
    ".htm": "text/html",
}

#: 走文本预览时用来标语言的扩展名(只影响前端高亮/标题,不影响安全)。
_LANG_BY_SUFFIX = {
    ".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript",
    ".jsx": "jsx", ".json": "json", ".md": "markdown", ".toml": "toml",
    ".yaml": "yaml", ".yml": "yaml", ".sh": "bash", ".css": "css",
    ".html": "html", ".htm": "html", ".sql": "sql", ".rs": "rust",
    ".go": "go", ".java": "java", ".c": "c", ".h": "c", ".cpp": "cpp",
    ".svg": "xml", ".xml": "xml",
}


#: 文件树里一行的类型(字面量:调用方据此穷尽分支)
FileKind = Literal["dir", "file"]

#: 文本预览的结果类型。"binary" 不是错误 —— 它是"这个文件能看见但不预览内容"。
TextKind = Literal["text", "binary"]


class FileAccessError(Exception):
    """路径越界 / 不存在 / 不是预期类型。调用方转成 4xx。"""


@dataclass
class TextRead:
    """文本预览的结果。

    **二进制与超限都不抛异常**:它们是"这个文件存在、但不适合当文本看"这一**正常结果**,
    该由返回值表达。先前这里是抛异常 + 调用方按错误**文案**里有没有"二进制"来判断 ——
    那种耦合一改文案就静默失效,单测也钉不住。
    """

    kind: TextKind
    text: str = ""
    size: int = 0
    truncated: bool = False


@dataclass
class Entry:
    name: str
    #: **相对**会话目录的 POSIX 路径(前端拿它当 key,也拿它回传)
    path: str
    kind: FileKind
    size: int
    mtime: float
    #: 文件是否能原字节预览(图片/PDF/HTML)—— 前端据此决定预览方式
    raw: bool


def guard(root: Path, p: str | Path) -> Path:
    """把路径钉在 `root` 内;越界就抛 `FileAccessError`。规则同 `ToolContext.guard`。"""
    raw = Path(str(p)).expanduser()
    resolved = (raw if raw.is_absolute() else root / raw).resolve(strict=False)
    base = root.resolve(strict=False)
    if resolved != base and base not in resolved.parents:
        raise FileAccessError(f"路径越界(仅允许会话目录内): {p}")
    return resolved


def _rel(root: Path, path: Path) -> str:
    """绝对路径 → 相对会话目录的 POSIX 路径(root 自己 → "")。"""
    rel = path.relative_to(root.resolve(strict=False))
    return rel.as_posix() if str(rel) != "." else ""


def list_dir(root: Path, rel: str = "") -> tuple[str, list[Entry]]:
    """列**一层**目录。返回 (该层目录的绝对路径, 条目)。

    目录在前、文件在后,各自按名字做**数字感知**排序(`file2` 在 `file10` 前,
    与 dsh 的 `Intl.Collator(..., {numeric: true})` 同一条)。这个顺序在前端也做不了:
    服务端给的是 listing 事实,排序是读者的事 —— 但只有这里知道"哪个是目录"。
    """
    target = guard(root, rel)
    if not target.is_dir():
        raise FileAccessError(f"不是一个目录: {rel or '.'}")
    entries: list[Entry] = []
    with os.scandir(target) as it:
        for item in it:
            try:
                st = item.stat()
            except OSError:
                continue                      # 竞态:列到一半被删了,跳过而不是整个失败
            is_dir = item.is_dir()            # 跟随软链:指向目录的软链算目录
            if not is_dir and not item.is_file():
                continue                      # 套接字/设备文件:不给看
            suffix = Path(item.name).suffix.lower()
            entries.append(Entry(
                name=item.name,
                path=(Path(rel) / item.name).as_posix() if rel else item.name,
                kind="dir" if is_dir else "file",
                size=0 if is_dir else st.st_size,
                mtime=st.st_mtime,
                raw=not is_dir and suffix in RAW_TYPES,
            ))
    entries.sort(key=lambda e: (e.kind != "dir", _natural_key(e.name)))
    return str(target), entries


#: 只认 ASCII 数字。**不能用 `str.isdigit()`**:它对上标也返回真(`"²".isdigit()` 是 True),
#: 而 `int("²")` 抛 ValueError —— 文件名里带上标就会把整个列表端点打成 500(实测发现)。
_ASCII_DIGITS = "0123456789"


def _natural_key(name: str) -> tuple:
    """数字感知排序键:把名字拆成"文本 / 数字 / 文本…",数字段按**数值**比。

    数字段不用 `int()` 比,而是比 `(位数, 数字串)` —— 位数多的更大,位数相同才比字典序。
    这样既精确(任意长度都对:`2` < `10` < `100…0`),又不会碰两件事:
      · `str.isdigit()` 对上标返回真、而 `int("²")` 抛(上面那条注释);
      · CPython 对 `int()` 有 4300 位数字的转换上限。
    """
    # 两段同形状的 `(标记, 位数, 文本)`:文本段位数恒为 0。同形状是为了让元素类型只有一种
    # (排序键本来就该是齐次的),也省掉一个联合类型注解。
    parts: list[tuple[int, int, str]] = []
    digits = ""
    for ch in name.lower():
        if ch in _ASCII_DIGITS:
            digits += ch
            continue
        if digits:
            parts.append((1, len(digits), digits))
            digits = ""
        parts.append((0, 0, ch))
    if digits:
        parts.append((1, len(digits), digits))
    return tuple(parts)


def read_text(root: Path, rel: str) -> TextRead:
    """读文本预览。

    含 NUL 字节 → 当二进制(这是"不是文本"的可靠信号,比按扩展名猜准);非 UTF-8 同理。
    两者都**回 `kind="binary"` 而不是抛** —— 见 `TextRead` 的注释。超大则截断 + 标记。
    """
    target = guard(root, rel)
    if not target.is_file():
        raise FileAccessError(f"不是一个文件: {rel}")
    size = target.stat().st_size
    with target.open("rb") as fh:
        blob = fh.read(TEXT_MAX_CHARS * 4)          # 多读一些,免得 UTF-8 边界截半个字符
    if b"\x00" in blob:
        return TextRead(kind="binary", size=size)
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return TextRead(kind="binary", size=size)
    truncated = len(text) > TEXT_MAX_CHARS or size > len(blob)
    return TextRead(kind="text", text=text[:TEXT_MAX_CHARS], size=size,
                    truncated=truncated)


def raw_media_type(rel: str) -> str:
    """原字节预览的 MIME;不在白名单里 → 抛错(调用方转 415)。"""
    suffix = Path(rel).suffix.lower()
    media = RAW_TYPES.get(suffix)
    if media is None:
        raise FileAccessError(f"不支持原样预览的类型: {suffix or '(无扩展名)'}")
    return media


def read_bytes(root: Path, rel: str) -> tuple[bytes, str]:
    """读原字节(图片/PDF/HTML)。返回 (字节, MIME)。"""
    media = raw_media_type(rel)
    target = guard(root, rel)
    if not target.is_file():
        raise FileAccessError(f"不是一个文件: {rel}")
    size = target.stat().st_size
    if size > RAW_MAX_BYTES:
        raise FileAccessError(f"文件太大({size // 1024} KB),不预览")
    return target.read_bytes(), media


def lang_of(rel: str) -> str:
    """给前端一个语言提示(只用于标题/高亮,不参与安全判断)。"""
    suffix = Path(rel).suffix.lower()
    return _LANG_BY_SUFFIX.get(suffix, mimetypes.guess_extension(suffix or "") or "")
