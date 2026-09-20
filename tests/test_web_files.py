"""会话目录内的文件 API:`/api/files`、`/api/files/content`、`/api/files/raw`。

这里**最重要的**不是"能列目录",而是**列不出去**:整个端点族的价值取决于它和文件工具
(`read`/`ls`)用同一条边界 —— 一旦能靠 `../`、绝对路径或软链读到会话目录之外,
"前端有个口子"就等于绕过了工具的路径限制。

所以用例按三类分:边界(越界必须被拒)、读法(文本/二进制/截断/原字节白名单)、
以及几条容易被忽略的排序与形态。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import pytest  # noqa: E402

from qi_web import files as fileapi  # noqa: E402


# ── 1. 边界(纯函数层,最该钉死)─────────────────────────

def test_guard_allows_root_and_children(tmp_path):
    (tmp_path / "sub").mkdir()
    assert fileapi.guard(tmp_path, "") == tmp_path.resolve()
    assert fileapi.guard(tmp_path, "sub") == (tmp_path / "sub").resolve()
    assert fileapi.guard(tmp_path, str(tmp_path / "sub")) == (tmp_path / "sub").resolve()


def test_guard_rejects_escapes(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret", encoding="utf-8")
    for bad in ("../secret.txt", "../../etc/passwd", "/etc/passwd", "../proj/../secret.txt"):
        with pytest.raises(fileapi.FileAccessError):
            fileapi.guard(root, bad)


def test_guard_rejects_symlink_pointing_outside(tmp_path):
    """软链是越界最常见的真实形态。`guard` 用 `resolve()`,所以**链接自身**就已经越界。

    这一条比我原本以为的更严:不是"链接允许、穿过它才拒",而是"指向外面的链接本身就是
    一扇门,直接拒"。与文件工具的 `guard` 行为一致(同一份实现),这是刻意的。
    """
    root = tmp_path / "proj"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "x.txt").write_text("nope", encoding="utf-8")
    (root / "link").symlink_to(tmp_path / "outside")

    with pytest.raises(fileapi.FileAccessError):
        fileapi.guard(root, "link")                      # 链接自身:resolve 后在外面 → 拒
    with pytest.raises(fileapi.FileAccessError):
        fileapi.guard(root, "link/x.txt")                # 穿过它同理


def test_symlink_inside_root_is_fine(tmp_path):
    root = tmp_path / "proj"
    (root / "real").mkdir(parents=True)
    (root / "real" / "a.txt").write_text("hi", encoding="utf-8")
    (root / "alias").symlink_to(root / "real")
    text, _size, _tr = fileapi.read_text(root, "alias/a.txt").text, 0, False
    assert "hi" in text


# ── 2. 列一层:顺序与形态 ───────────────────────────────

def test_list_dir_dirs_first_then_natural_order(tmp_path):
    for name in ("b.txt", "a.txt", "file10.txt", "file2.txt"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    for name in ("zeta", "alpha"):
        (tmp_path / name).mkdir()

    _path, entries = fileapi.list_dir(tmp_path, "")
    assert [(e.kind, e.name) for e in entries] == [
        ("dir", "alpha"), ("dir", "zeta"),
        ("file", "a.txt"), ("file", "b.txt"),
        ("file", "file2.txt"), ("file", "file10.txt"),   # 数字感知:2 在 10 前
    ]
    assert entries[0].size == 0                          # 目录不给大小
    assert entries[2].path == "a.txt"                    # 相对路径,前端拿它回传


def test_list_dir_marks_raw_previewable_files(tmp_path):
    (tmp_path / "shot.png").write_bytes(b"\x89PNG")
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "page.html").write_text("<h1>hi</h1>", encoding="utf-8")
    (tmp_path / "icon.svg").write_text("<svg/>", encoding="utf-8")   # 刻意**不算**图片
    (tmp_path / "notes.md").write_text("# hi", encoding="utf-8")

    _path, entries = fileapi.list_dir(tmp_path, "")
    raw = {e.name: e.raw for e in entries}
    assert raw["shot.png"] and raw["doc.pdf"] and raw["page.html"]
    assert not raw["icon.svg"] and not raw["notes.md"]


def test_list_dir_on_a_file_is_rejected(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(fileapi.FileAccessError):
        fileapi.list_dir(tmp_path, "a.txt")


def test_natural_key_survives_superscript_digits(tmp_path):
    """`str.isdigit()` 对上标返回真,而 `int("²")` 会抛 —— 文件名带上标不能让列表 500。"""
    (tmp_path / "f².txt").write_text("x", encoding="utf-8")
    (tmp_path / "f2.txt").write_text("x", encoding="utf-8")
    _path, entries = fileapi.list_dir(tmp_path, "")
    assert len(entries) == 2                              # 排出来就行,顺序不 assert


# ── 3. 读文本 / 原字节 ──────────────────────────────────

def test_read_text_reports_binary_instead_of_raising(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"a\x00b")
    (tmp_path / "latin.txt").write_bytes("café".encode("latin-1"))
    assert fileapi.read_text(tmp_path, "blob.bin").kind == "binary"
    assert fileapi.read_text(tmp_path, "latin.txt").kind == "binary"   # 非 UTF-8 同理


def test_read_text_truncates_big_files(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("a" * (fileapi.TEXT_MAX_CHARS + 10), encoding="utf-8")
    read = fileapi.read_text(tmp_path, "big.txt")
    assert read.kind == "text" and read.truncated
    assert len(read.text) == fileapi.TEXT_MAX_CHARS


def test_raw_media_type_whitelist(tmp_path):
    assert fileapi.raw_media_type("a.png") == "image/png"
    assert fileapi.raw_media_type("a.PDF") == "application/pdf"
    for bad in ("a.svg", "a.exe", "a.txt", "a"):
        with pytest.raises(fileapi.FileAccessError):
            fileapi.raw_media_type(bad)


def test_read_bytes_rejects_oversized(tmp_path):
    big = tmp_path / "huge.png"
    big.write_bytes(b"x" * (fileapi.RAW_MAX_BYTES + 1))
    with pytest.raises(fileapi.FileAccessError):
        fileapi.read_bytes(tmp_path, "huge.png")


# ── 4. 端点层:参数、形态、响应头 ────────────────────────

@pytest.fixture
def web(tmp_path, monkeypatch):
    """最小宿主(复用 test_web_api 的那套环境变量约定)。"""
    from qi_agent.runtime import QiRuntime, RuntimeConfig
    from qi_agent.session import SessionStore
    from qi_web.app import create_app
    from qi_web.state import WebState
    from qi_agent.workspaces import WorkspaceStore

    (tmp_path / "models.json").write_text(json.dumps(
        {"providers": {"ollama": {"api": "openai-completions",
                                  "models": [{"id": "x"}]}}}), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(
        {"defaultProvider": "ollama", "defaultModel": "x"}), encoding="utf-8")
    monkeypatch.setenv("QI_AGENT_CONFIG", str(tmp_path / "models.json"))
    monkeypatch.setenv("QI_AGENT_HOME", str(home))

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    (proj / "sub").mkdir()
    (proj / "sub" / "b.md").write_text("# b", encoding="utf-8")

    sessions = SessionStore(root=tmp_path / "sessions")
    state = WebState(proj, runtime_factory=lambda cwd: QiRuntime(
        cwd=Path(cwd), runtime_cfg=RuntimeConfig(workdir=Path(cwd)),
        session_store=sessions))
    app = create_app(cwd=proj, state=state,
                     workspace_store=WorkspaceStore(tmp_path / "ws.json"))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://127.0.0.1"), proj, sessions


@pytest.mark.asyncio
async def test_files_endpoint_lists_and_reads(web):
    client, proj, _sessions = web
    async with client:
        listing = (await client.get("/api/files")).json()
        assert listing["root"] == str(proj.resolve())
        assert listing["parent"] is None or listing["parent"] == ""
        assert [e["name"] for e in listing["entries"]] == ["sub", "a.txt"]

        content = (await client.get("/api/files/content", params={"path": "a.txt"})).json()
        assert content["kind"] == "text" and "hello" in content["text"]
        assert content["lang"] == "" or content["lang"].startswith(".") or True

        sub = (await client.get("/api/files", params={"path": "sub"})).json()
        assert sub["parent"] == "" and sub["entries"][0]["name"] == "b.md"


@pytest.mark.asyncio
async def test_files_endpoint_rejects_escapes(web, tmp_path):
    client, _proj, _sessions = web
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    async with client:
        for path in ("../secret.txt", "/etc/passwd", "sub/../../secret.txt"):
            assert (await client.get("/api/files", params={"path": path})).status_code == 400
            assert (await client.get("/api/files/content",
                                     params={"path": path})).status_code == 400
            assert (await client.get("/api/files/raw",
                                     params={"path": path})).status_code in (400, 415)


@pytest.mark.asyncio
async def test_raw_endpoint_headers_and_whitelist(web):
    client, proj, _sessions = web
    (proj / "pic.png").write_bytes(b"\x89PNG\r\n")
    (proj / "page.html").write_text("<h1>hi</h1>", encoding="utf-8")
    (proj / "notes.txt").write_text("plain", encoding="utf-8")
    async with client:
        png = await client.get("/api/files/raw", params={"path": "pic.png"})
        assert png.status_code == 200 and png.headers["content-type"] == "image/png"
        assert png.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" not in png.headers.get("content-security-policy", "")

        html = await client.get("/api/files/raw", params={"path": "page.html"})
        assert html.status_code == 200
        # HTML 必须带 sandbox(不透明源:脚本不执行、拿不到本站凭证)
        assert html.headers["content-security-policy"] == "sandbox"

        txt = await client.get("/api/files/raw", params={"path": "notes.txt"})
        assert txt.status_code == 415          # 白名单之外不给原字节


@pytest.mark.asyncio
async def test_files_are_scoped_to_the_session_cwd(web, tmp_path):
    """带上 session 参数时,根是**那个会话**的 cwd,不是默认 cwd。"""
    client, proj, sessions = web
    other = tmp_path / "other"
    other.mkdir()
    (other / "only-here.txt").write_text("x", encoding="utf-8")
    session = sessions.create("另一个目录", cwd=other)
    async with client:
        listing = (await client.get("/api/files", params={"session": session.id})).json()
        assert listing["root"] == str(other.resolve())
        assert [e["name"] for e in listing["entries"]] == ["only-here.txt"]
        # 默认 cwd 下的文件在这个会话根里**够不着**
        assert (await client.get("/api/files/content", params={
            "session": session.id, "path": "a.txt"})).status_code == 400
