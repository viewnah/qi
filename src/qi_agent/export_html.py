"""会话导出成**单文件 HTML**(pi 的 `/export [file]`:按扩展名选 HTML 或 JSONL)。

三件事刻意这么做:

* **只导当前分支** —— 用户看到的、能接着跑的就是那一条(与会话回放同一口径);整个文件
  (含其它分支)仍然可以用 `.jsonl` 导出,那条路没变;
* **自包含**:CSS 内联、不引任何外部资源 —— 这样一个文件就能分享(将来 `/share` 上传的
  正是它),离线打开也不掉样式;
* **一切内容都转义**:这是**可分享的产物**,而会话里装的是用户与模型的自由文本 ——
  模型输出里的 `<script>` 在别人的浏览器里执行,是这类导出最典型的坑。
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

_CSS = """body { margin: 0 auto; max-width: 52rem; padding: 2rem 1rem;
  font: 15px/1.6 -apple-system, "Segoe UI", "Noto Sans CJK SC", sans-serif;
  background: #faf9f7; color: #23201c; }
h1 { font-size: 1.1rem; margin: 0 0 .25rem; }
.meta { color: #6b635a; font-size: .8rem; margin-bottom: 1.5rem; }
.turn { border-left: 3px solid #d8d2c8; padding: .1rem 0 .1rem .8rem; margin: 1rem 0;
  white-space: pre-wrap; overflow-wrap: anywhere; }
.user { border-color: #8a7f6d; }
.assistant { border-color: #6b8ca8; }
.tool { border-color: #b3a894; color: #554e46; font-family: ui-monospace, monospace;
  font-size: .85rem; }
.note { color: #6b635a; font-style: italic; }
.role { display: block; font-size: .7rem; letter-spacing: .06em; text-transform: uppercase;
  color: #8a7f6d; margin-bottom: .2rem; }"""

_ROLE_LABEL = {"user": "你", "assistant": "助手", "tool": "工具"}


def _text_of(content: Any) -> str:
    """`content` → 纯文本。字符串直接用;分段列表取其文本;其它形状退 `str()`。

    (会话 entry 的 `content` 多数是字符串,但工具结果与多段消息可能是列表 ——
    宁可退化成可读的字符串,也不要在这里抛。)
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return "" if content is None else str(content)


def render_session_html(session: Any, *, title: str | None = None) -> str:
    """把会话的**当前分支**渲染成一个自包含 HTML 字符串。"""
    heading = title or getattr(session, "title", "") or "会话"
    created = str(getattr(session, "created_at", "") or "")
    sid = str(getattr(session, "id", "") or "")
    where = str(getattr(session, "cwd", "") or "")
    rows: list[str] = []
    for entry in session.branch():
        kind = str(entry.get("type") or "")
        if kind == "message":
            role = str(entry.get("role") or "assistant")
            body = _text_of(entry.get("content"))
            if not body.strip():
                continue
            label = _ROLE_LABEL.get(role, role)
            rows.append(f'<div class="turn {html.escape(role)}">'
                        f'<span class="role">{html.escape(label)}</span>'
                        f'{html.escape(body)}</div>')
        elif kind in ("compaction", "branch_summary"):
            summary = _text_of(entry.get("summary"))
            who = "上下文压缩" if kind == "compaction" else "分支摘要"
            rows.append(f'<div class="turn note">{html.escape(who)}:{html.escape(summary)}</div>')
    meta = " · ".join(part for part in (sid, created, where) if part)
    return ("<!doctype html>\n<html lang=\"zh\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<title>{html.escape(heading)}</title>\n<style>\n{_CSS}\n</style>\n</head>\n<body>\n"
            f"<h1>{html.escape(heading)}</h1>\n"
            f'<p class="meta">{html.escape(meta)}</p>\n'
            + "\n".join(rows) + "\n</body>\n</html>\n")


def write_session_html(session: Any, out: Path, *, title: str | None = None) -> Path:
    """写文件(目录不存在就建)。返回写到的路径。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_session_html(session, title=title), encoding="utf-8")
    return out


def html_wanted(path: Path) -> bool:
    """`.html` / `.htm` → 导 HTML;其余(含 `.jsonl`)→ 导原始 JSONL(pi 同口径)。"""
    return path.suffix.lower() in (".html", ".htm")
