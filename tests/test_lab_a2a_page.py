"""`GET /a2a` —— 手写页面的托管、以及它与前端产物的边界。

这一页刻意**不走 Vite 构建**:`qi_lab/static/` 是 `emptyOutDir: true` 的 outDir,
下一次 `npm run build` 会把不属于它的文件一起清掉。所以页面住 `qi_lab/pages/`,
改完刷新就生效,不需要 `npm install`。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

QILAB = REPO / "src" / "qi_lab"
PAGE = QILAB / "pages" / "a2a.html"


def _client(cwd: Path) -> TestClient:
    from qi_lab.app import create_app

    app = create_app(cwd=cwd, password=None, allowed_hosts=[], bind_host="127.0.0.1")
    return TestClient(app, base_url="http://127.0.0.1")


def test_page_is_served_without_a_build(tmp_path):
    response = _client(tmp_path).get("/a2a")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    # 一页能自洽:调的是自己的接口,不是外链
    assert "/api/a2a" in body
    assert "/api/a2a/probe" in body


def test_page_lives_outside_the_vite_out_dir():
    """放 `static/` 里会被下一次 `npm run build` 清掉 —— 这条钉住位置。"""
    assert PAGE.is_file()
    assert not (QILAB / "static" / "a2a.html").exists()


def test_page_needs_no_npm_and_no_bare_module_imports():
    """原生 JS:没有 `import` 语句、没有打包器才能解析的说明符。"""
    body = PAGE.read_text(encoding="utf-8")
    assert "<script" in body
    # 页面自己带的脚本里不该出现 ES module 的裸导入(那需要打包器解析)
    assert "from \"react\"" not in body
    assert 'type="module"' not in body


def test_page_never_embeds_a_password(tmp_path):
    """口令由用户当次输入,不写进页面、不写进 URL。"""
    body = _client(tmp_path).get("/a2a").text
    assert "password" in body          # 有个输入框
    assert "localStorage" not in body  # 不把口令落盘
    assert "sessionStorage" in body    # 只放当次会话


def test_inline_script_parses(tmp_path):
    """手写页面没有打包器兜底:一个少掉的 `}` 会让整页停在“加载中…”。

    这条曾经真的抓到过(`${esc(…}` 少一个右花括号)。
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:                    # 没装 node 就跳过,不要假装验过
        pytest.skip("没有 node,跳过内联脚本的语法检查")

    body = _client(tmp_path).get("/a2a").text
    match = re.search(r"<script[^>]*>(.*?)</script>", body, re.S)
    assert match, "页面里没有内联脚本"
    script = tmp_path / "page.js"
    script.write_text(match.group(1), encoding="utf-8")

    proc = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, f"内联脚本语法错误:\n{proc.stderr}"


def test_page_is_not_listed_as_an_api_route():
    """`/a2a` 是页面,不是 `/api` 路由 —— 文档双向对齐只管 `/api`。"""
    app = _client(Path.cwd()).app
    paths = {getattr(route, "path", "") for route in getattr(app, "routes", [])}
    assert "/a2a" in paths
    assert not any(p.startswith("/api/a2a.html") for p in paths)
