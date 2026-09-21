"""会话导出成 HTML(pi 的 `/export [file]` 支持 HTML 或 JSONL,按扩展名选)。

三条它必须守住的:

* **只导当前分支** —— 整个文件(含其它分支)仍然走 `.jsonl`;
* **自包含**(CSS 内联、不引外部资源)—— 这样一个文件就能分享,也正是将来 `/share` 上传的东西;
* **一切内容转义** —— 这是可分享的产物,而模型输出里的 `<script>` 在别人浏览器里执行是
  这类导出最典型的坑。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qi_agent.export_html import (  # noqa: E402
    html_wanted,
    render_session_html,
    write_session_html,
)


def _session(entries: list[dict], *, title: str = "我的会话") -> SimpleNamespace:
    return SimpleNamespace(title=title, id="abc123", created_at="2026-09-20",
                           cwd="/tmp/proj", branch=lambda: list(entries))


def _msg(role: str, content: str) -> dict:
    return {"type": "message", "role": role, "content": content, "agent_id": "core"}


def test_escapes_everything():
    """**安全**:内容里的标签只当文字,不能成为可执行的 HTML。"""
    html = render_session_html(_session([_msg("assistant", "<script>alert(1)</script>")]))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_renders_roles_and_notes():
    html = render_session_html(_session([
        _msg("user", "你好"),
        _msg("assistant", "在的"),
        {"type": "compaction", "summary": "压过一次"},
        {"type": "branch_summary", "summary": "切过分支"},
    ]))
    assert 'class="turn user"' in html and "你好" in html
    assert 'class="turn assistant"' in html and "在的" in html
    assert "上下文压缩" in html and "分支摘要" in html
    assert "abc123" in html and "2026-09-20" in html and "我的会话" in html


def test_is_self_contained():
    """不引外部资源:没有 http(s) 链接、没有 src=。"""
    html = render_session_html(_session([_msg("user", "hi")]))
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html.lower()


def test_only_the_current_branch():
    """`branch()` 给什么就渲染什么 —— 其它分支不在其中(那是 `.jsonl` 导出的事)。"""
    html = render_session_html(_session([_msg("user", "只在当前分支里")]))
    assert "只在当前分支里" in html


def test_list_content_is_joined():
    """工具结果/多段消息的 `content` 可能是列表 —— 取文本,不抛。"""
    html = render_session_html(_session([
        {"type": "message", "role": "tool",
         "content": [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}]}]))
    assert "第一段" in html and "第二段" in html


def test_html_wanted_by_suffix():
    assert html_wanted(Path("x.html")) and html_wanted(Path("X.HTM"))
    assert not html_wanted(Path("x.jsonl")) and not html_wanted(Path("x.txt"))


def test_write_creates_parent_dirs(tmp_path):
    out = tmp_path / "deep" / "dir" / "s.html"
    write_session_html(_session([_msg("user", "hi")]), out)
    assert out.is_file() and out.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_cli_export_dispatches_by_extension(tmp_path, monkeypatch):
    """端到端:`--export out.html` 出 HTML,`--export out.jsonl` 仍抄整个文件。"""
    import json

    from qi_agent import paths
    from qi_agent.session import SessionStore
    from qi_agent.cli import app
    from typer.testing import CliRunner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    store = SessionStore()
    session = store.create("t", cwd=tmp_path)
    store.append(session, _msg("user", "导出我"))

    html_out = tmp_path / "s.html"
    json_out = tmp_path / "s.jsonl"
    runner = CliRunner()
    assert runner.invoke(app, ["--export", str(html_out)]).exit_code == 0
    assert runner.invoke(app, ["--export", str(json_out)]).exit_code == 0

    assert html_out.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert "导出我" in html_out.read_text(encoding="utf-8")
    # JSONL 那条路没变:仍是整个会话文件(带 header)
    assert json.loads(json_out.read_text(encoding="utf-8").splitlines()[0])["type"] == "session"
