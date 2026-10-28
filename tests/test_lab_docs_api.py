"""文档防漂:`docs/api/` 必须与 `create_app()` 实际注册的路由**双向对齐**。

接口文档写在和代码同仓的目录里,漂移只有两种,而且**都不会有人报警**:

  · 代码删了端点、文档还写着 —— 照着文档调 → 404/405;
  · 代码加了端点、文档没写 —— 没人知道它存在,于是重复造一个。

所以让测试去数路由。断言的是"**方法 + 路径**这个字面串出现在某份文档里",
不校对措辞:文档要能自由写作,只钉"有没有提到这条"。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]       # 仓库根
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

from qi_lab.app import create_app  # noqa: E402

DOCS = REPO / "docs" / "api"

#: 文档里**故意**提到的别的项目的端点(借形状说明用),不算 qi-lab 自己的路由。
OTHER_PROJECTS = {"GET /api/cwd/browse"}   # pi-web 的目录浏览(形状照它抄)


def _routes() -> set[str]:
    """`create_app()` 注册的每一条 `/api` 路由 → `"GET /api/sessions"` 这种字面串。"""
    app = create_app(cwd=REPO)
    out: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = {m for m in (getattr(route, "methods", None) or []) if m != "HEAD"}
        if path.startswith("/api") and methods:
            out.update(f"{method} {path}" for method in methods)
    return out


def _corpus() -> str:
    files = sorted(DOCS.glob("*.md"))
    assert files, f"{DOCS} 下没有 .md —— 接口文档目录空了"
    return "\n".join(p.read_text(encoding="utf-8") for p in files)


def test_docs_dir_exists():
    assert DOCS.is_dir(), f"接口文档目录不见了: {DOCS}"


def test_every_registered_route_is_documented():
    """**代码有、文档没有** → 每条注册的 `/api` 路由都要在文档里出现一次。"""
    corpus = _corpus()
    missing = sorted(r for r in _routes() if r not in corpus)
    assert not missing, "这些端点没写进 docs/api/:\n  " + "\n  ".join(missing)


def test_docs_do_not_invent_routes():
    """**文档有、代码没有** → 文档里出现的 `METHOD /api/…` 必须真的注册过。

    这一条防的是"文档说有、代码已删"(照着调就是 404/405)。
    """
    corpus = _corpus()
    mentioned = set(re.findall(r"(?:GET|POST|PATCH|PUT|DELETE)\s+/api[^\s`\"',)）」】]*", corpus))
    real = _routes() | OTHER_PROJECTS
    stale = sorted(mentioned - real)
    assert not stale, "文档里写了不存在的端点:\n  " + "\n  ".join(stale)


def test_readme_indexes_every_doc_file():
    """`README.md` 要点到每一份分册 —— 否则那份文档等于没有。"""
    readme = (DOCS / "README.md").read_text(encoding="utf-8")
    stale = sorted(p.name for p in DOCS.glob("*.md")
                   if p.name != "README.md" and f"({p.name})" not in readme)
    assert not stale, "README.md 没链接到: " + ", ".join(stale)


def test_every_capability_bit_is_documented(tmp_path):
    """**代码有、文档没有** → `/api/meta` 的每个能力位都要在文档里点过名。

    路由对齐只管“端点存不存在”,管不住能力位:少写一个 `a2a_write` /
    `store_write` / `kb_write`,前端就不知道该怎么降级 —— 而那不会报错。
    """
    import asyncio
    import httpx

    from qi_lab.app import create_app

    app = create_app(cwd=tmp_path)
    transport = httpx.ASGITransport(app=app)

    async def _caps() -> dict:
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://127.0.0.1") as client:
            response = await client.get("/api/meta")
        assert response.status_code == 200
        return response.json()["capabilities"]

    capabilities = asyncio.run(_caps())
    corpus = _corpus()
    missing = sorted(name for name in capabilities if f"`{name}`" not in corpus)
    assert not missing, "这些能力位没写进 docs/api/:\n  " + "\n  ".join(missing)
