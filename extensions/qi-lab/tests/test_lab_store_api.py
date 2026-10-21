"""商店面板的 API(`/api/store/*`)。

三条要钉住的东西:

1. **qi-store 没装不是 500** —— `available=false` + `error`(与 `/api/agents` 的
   `unavailable` 同一套口径:少装一个扩展不该让整页报错);
2. **写操作过 `X-Qi-Confirm`** —— 与 `/api/mcp`、`/api/a2a` 同一条闸;
3. **登录不经网页** —— 没有 `/api/store/login`,凭证只在终端写(statu 只读)。

上游全部用 `httpx.MockTransport` 假装(`qi_store.ops.open_client` 是注入点),
所以这些用例不碰网络、也不依赖那台实例活着。
"""

from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402
import pytest  # noqa: E402

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_lab.app import create_app  # noqa: E402

FIXTURES = REPO / "extensions" / "qi-store" / "tests" / "fixtures"
YES = {"X-Qi-Confirm": "yes"}


def _store_paths():
    """store 的文件位置来自 qi_store(不是 qi_agent.paths) —— 两套名字别混。"""
    from qi_store import paths as store_paths

    return store_paths



def jwt(exp: int | None = None) -> str:
    import base64

    payload = {"username": "admin", "role": "admin", "exp": exp or int(time.time()) + 3600}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{raw}.sig"


SKILL_MD = "---\nname: ppt-generator\ndescription: 做 PPT\n---\n\n正文\n"


def skill_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ppt-generator/SKILL.md", SKILL_MD)
    return buffer.getvalue()


def upstream() -> httpx.MockTransport:
    skills = json.loads((FIXTURES / "skills.json").read_text(encoding="utf-8"))
    a2a = json.loads((FIXTURES / "a2a_agents.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/skills":
            return httpx.Response(200, json=skills)
        if path == "/api/skills/4/download":
            return httpx.Response(200, content=skill_zip())
        if path == "/api/skills/4/files":
            return httpx.Response(200, json={"skill_id": 4, "skill_name": "ppt-generator",
                                             "files": [{"name": "SKILL.md",
                                                        "is_directory": False}]})
        if path == "/api/mcp/servers":
            return httpx.Response(200, json={"total": 0, "page": 1, "page_size": 100,
                                             "items": []})
        if path == "/a2a/agents":
            return httpx.Response(200, json=a2a)
        return httpx.Response(404, json={"code": 404, "message": path})

    return httpx.MockTransport(handler)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    (path / ".git").mkdir(parents=True)
    return path


@pytest.fixture
def fake_upstream(monkeypatch):
    """把 qi_store 的客户端换成 MockTransport —— 生产代码一行不改。"""
    qi_store = pytest.importorskip("qi_store")
    from qi_store import ops
    from qi_store.client import StoreClient

    def fake_open(source, http_factory=None, **kwargs):
        return StoreClient(source, http=httpx.Client(transport=upstream()), **kwargs)

    monkeypatch.setattr(ops, "open_client", fake_open)
    return qi_store


def _seed_store() -> None:
    """用户级 store.json:一个源 + 一张假凭证。"""
    from qi_store import config

    config.save_auth("hikqin", {"login": "admin", "role": "admin", "access": jwt()},
                     base_url="http://h:6091")
    config.save_default("hikqin")


def _client(project: Path) -> TestClient:
    app = create_app(cwd=project, password=None, allowed_hosts=[], bind_host="127.0.0.1")
    return TestClient(app, base_url="http://127.0.0.1")


# ── status ─────────────────────────────────────────────


def test_status_reports_source_and_login_without_network(project, fake_upstream, monkeypatch):
    _seed_store()

    def no_network(*_a, **_k):                     # pragma: no cover
        raise AssertionError("status 不该联网")

    monkeypatch.setattr(fake_upstream.ops, "open_client", no_network)
    with _client(project) as client:
        body = client.get("/api/store/status").json()

    assert body["available"] is True
    assert body["sources"][0]["name"] == "hikqin"
    assert body["sources"][0]["loggedIn"] is True
    assert body["sources"][0]["login"] == "admin"
    assert body["defaultScope"] == "global"


def test_status_says_unavailable_without_qi_store(project, monkeypatch):
    monkeypatch.setitem(sys.modules, "qi_store", None)      # import 会失败

    with _client(project) as client:
        response = client.get("/api/store/status")
    assert response.status_code == 200                       # 不是 500
    body = response.json()
    assert body["available"] is False and body["error"]


def test_login_requires_the_confirm_header(project, fake_upstream):
    """写凭证是写操作 → 与其它写端点同一条确认闸。"""
    with _client(project) as client:
        response = client.post("/api/store/login",
                               json={"login": "admin", "password": "pw",
                                     "baseUrl": "http://h:6091"})
    assert response.status_code == 428
    assert not _store_paths().user_config_file().exists()


def test_login_writes_credentials_but_never_echoes_them(project, fake_upstream):
    """密码过一次就丢;响应只有登录态 —— **令牌一个字节都不回浏览器**。"""
    from qi_store import config

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"access_token": jwt(4102444800),
                                             "refresh_token": "refresh-secret",
                                             "user": {"username": "admin", "role": "admin"}})
        if request.url.path == "/api/auth/me":
            return httpx.Response(200, json={"username": "admin", "role": "admin"})
        return httpx.Response(404, json={"code": 404, "message": request.url.path})

    from qi_store.client import StoreClient

    def fake_open(source, http_factory=None, on_auth_change=None, **kwargs):
        # **必须转发 on_auth_change**:它才是"令牌变了就写回用户级配置"的那条路;
        # 丢了它,登录看着成功、磁盘上什么都没有。
        return StoreClient(source, http=httpx.Client(transport=httpx.MockTransport(handler)),
                           on_auth_change=on_auth_change, **kwargs)

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(fake_upstream.ops, "open_client", fake_open)
        with _client(project) as client:
            response = client.post("/api/store/login", headers=YES,
                                   json={"login": "admin", "password": "pw",
                                         "baseUrl": "http://h:6091"})
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["sources"][0]["loggedIn"] is True
    assert body["sources"][0]["login"] == "admin"
    # 响应里没有令牌,也没有密码
    for secret in ("refresh-secret", "pw", jwt(4102444800)):
        assert secret not in response.text

    # 但磁盘上是**真的**写了(用户级 store.json)
    stored = json.loads(_store_paths().user_config_file().read_text(encoding="utf-8"))
    assert stored["stores"]["hikqin"]["auth"]["access"] == jwt(4102444800)
    assert config.DEFAULT_STORE_NAME == "hikqin"


def test_login_bad_credentials_is_401_and_writes_nothing(project, fake_upstream):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401, "message": "用户名或密码错误"})

    from qi_store.client import StoreClient

    def fake_open(source, http_factory=None, on_auth_change=None, **kwargs):
        # **必须转发 on_auth_change**:它才是"令牌变了就写回用户级配置"的那条路;
        # 丢了它,登录看着成功、磁盘上什么都没有。
        return StoreClient(source, http=httpx.Client(transport=httpx.MockTransport(handler)),
                           on_auth_change=on_auth_change, **kwargs)

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(fake_upstream.ops, "open_client", fake_open)
        with _client(project) as client:
            response = client.post("/api/store/login", headers=YES,
                                   json={"login": "admin", "password": "bad",
                                         "baseUrl": "http://h:6091"})
    assert response.status_code == 401
    assert not _store_paths().user_config_file().exists()


def test_login_without_a_url_is_400(project, fake_upstream):
    with _client(project) as client:
        response = client.post("/api/store/login", headers=YES,
                               json={"login": "admin", "password": "pw"})
    assert response.status_code == 400
    assert "地址" in response.json()["detail"]


def test_logout_removes_credentials(project, fake_upstream):
    _seed_store()
    with _client(project) as client:
        body = client.post("/api/store/logout", headers=YES, json={}).json()
        assert body["sources"][0]["loggedIn"] is False
    stored = json.loads(_store_paths().user_config_file().read_text(encoding="utf-8"))
    assert "auth" not in stored["stores"]["hikqin"]


# ── search ─────────────────────────────────────────────


def test_search_lists_items_with_install_state(project, fake_upstream):
    _seed_store()
    with _client(project) as client:
        body = client.get("/api/store/search").json()

    assert body["available"] is True and body["total"] >= 4
    by_kind = {item["kind"] for item in body["items"]}
    assert by_kind == {"skill", "a2a"}          # 上游 mcp 为空(真实情况)
    first = next(item for item in body["items"] if item["id"] == "skill:4")
    assert first["installed"] is False
    assert first["safety"]["scan_status"] == "safe"


def test_search_reports_missing_kind_instead_of_silence(project, fake_upstream, monkeypatch):
    _seed_store()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a2a/agents":
            return httpx.Response(200, json=[])
        return httpx.Response(401, json={"code": 401, "message": "无效的认证凭据"})

    from qi_store.client import StoreClient

    def fake_open(source, http_factory=None, on_auth_change=None, **kwargs):
        # **必须转发 on_auth_change**:它才是"令牌变了就写回用户级配置"的那条路;
        # 丢了它,登录看着成功、磁盘上什么都没有。
        return StoreClient(source, http=httpx.Client(transport=httpx.MockTransport(handler)),
                           on_auth_change=on_auth_change, **kwargs)

    monkeypatch.setattr(fake_upstream.ops, "open_client", fake_open)
    with _client(project) as client:
        body = client.get("/api/store/search").json()
    assert body["items"] == []
    assert {m["kind"] for m in body["missing"]} == {"skill", "mcp"}


# ── plan / install / uninstall ─────────────────────────


def test_plan_is_read_only(project, fake_upstream):
    _seed_store()
    with _client(project) as client:
        body = client.post("/api/store/plan", json={"ids": ["skill:4"]}).json()

    assert body["available"] is True
    item = body["items"][0]
    assert item["id"] == "skill:4"
    assert item["targets"] and item["targets"][0].replace("\\", "/").endswith("skills/ppt-generator")
    assert body["blocked"] is False
    assert not (_store_paths().global_agent_dir() / "skills").exists()      # 一个文件都没写


def test_install_requires_the_confirm_header(project, fake_upstream):
    _seed_store()
    with _client(project) as client:
        response = client.post("/api/store/install", json={"ids": ["skill:4"]})
    assert response.status_code == 428
    assert not (_store_paths().global_agent_dir() / "skills").exists()


def test_install_then_uninstall(project, fake_upstream):
    _seed_store()
    with _client(project) as client:
        installed = client.post("/api/store/install", headers=YES,
                                json={"ids": ["skill:4"]}).json()
        assert installed["available"] is True
        assert installed["installed"][0]["id"] == "skill:4"
        target = _store_paths().global_agent_dir() / "skills" / "ppt-generator"
        assert (target / "SKILL.md").is_file()

        listed = client.get("/api/store/installed").json()
        assert listed["items"][0]["id"] == "skill:4"
        assert listed["items"][0]["scope"] == "global"

        gone = client.post("/api/store/uninstall", headers=YES,
                           json={"id": "skill:4"}).json()
        assert gone["available"] is True
        assert not target.exists()


def test_install_rejects_empty_ids(project, fake_upstream):
    _seed_store()
    with _client(project) as client:
        assert client.post("/api/store/install", headers=YES, json={"ids": []}).status_code == 400
        assert client.post("/api/store/plan", json={"ids": []}).status_code == 400


def test_install_unknown_id_is_404(project, fake_upstream):
    _seed_store()
    with _client(project) as client:
        response = client.post("/api/store/install", headers=YES,
                               json={"ids": ["skill:999"]})
    assert response.status_code == 404


def test_install_conflict_is_409_until_force(project, fake_upstream):
    _seed_store()
    mcp_file = _store_paths().global_agent_dir() / "mcp.json"
    mcp_file.parent.mkdir(parents=True, exist_ok=True)
    mcp_file.write_text(json.dumps({"mcpServers": {"ppt-tools": {"url": "old"}}}),
                        encoding="utf-8")
    # 用一个 mcp 条目制造冲突:上游 mcp 列表为空,所以先让它出现
    from qi_store import ops
    from qi_store.client import StoreClient

    synthetic = json.loads((FIXTURES / "mcp_server_synthetic.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/mcp/servers":
            return httpx.Response(200, json={"total": 1, "page": 1, "page_size": 100,
                                             "items": [synthetic]})
        if request.url.path == "/a2a/agents":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"total": 0, "items": []})

    def fake_open(source, http_factory=None, on_auth_change=None, **kwargs):
        # **必须转发 on_auth_change**:它才是"令牌变了就写回用户级配置"的那条路;
        # 丢了它,登录看着成功、磁盘上什么都没有。
        return StoreClient(source, http=httpx.Client(transport=httpx.MockTransport(handler)),
                           on_auth_change=on_auth_change, **kwargs)

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(ops, "open_client", fake_open)
        with _client(project) as client:
            response = client.post("/api/store/install", headers=YES, json={"ids": ["mcp:3"]})
            assert response.status_code == 409
            assert json.loads(mcp_file.read_text(encoding="utf-8"))["mcpServers"] == {
                "ppt-tools": {"url": "old"}}

            forced = client.post("/api/store/install", headers=YES,
                                 json={"ids": ["mcp:3"], "force": True})
            assert forced.status_code == 200
            assert json.loads(mcp_file.read_text(encoding="utf-8"))[
                "mcpServers"]["ppt-tools"]["type"] == "streamable-http"


# ── check ──────────────────────────────────────────────


def test_check_reports_statuses_and_wanted_drift(project, fake_upstream):
    _seed_store()
    with _client(project) as client:
        client.post("/api/store/install", headers=YES, json={"ids": ["skill:4", "a2a:agent-4c4af5bf"]})

    # 声明里加一个没装的
    store = _store_paths().user_config_file()
    raw = json.loads(store.read_text(encoding="utf-8"))
    raw["wanted"] = ["skill:3"]
    store.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with _client(project) as client:
        body = client.get("/api/store/check").json()

    assert body["available"] is True
    statuses = {item["id"]: item["status"] for item in body["items"]}
    assert statuses["skill:4"] == "up-to-date"
    assert body["wantedMissing"][0]["spec"] == "skill:3"
    assert body["wantedMissing"][0]["reason"] == "声明了但没装"


# ── 安全:值不出宿主 ────────────────────────────────────


def test_store_endpoints_never_echo_tokens(project, fake_upstream):
    _seed_store()
    from qi_store import paths as store_paths

    token = json.loads(store_paths.user_config_file().read_text(encoding="utf-8"))[
        "stores"]["hikqin"]["auth"]["access"]
    with _client(project) as client:
        for path in ("/api/store/status", "/api/store/search", "/api/store/installed",
                     "/api/store/check"):
            assert token not in client.get(path).text


def test_search_for_an_unimplemented_kind_explains_instead_of_400(project, fake_upstream):
    """`agent` 这一类没实现 —— 面板要把理由显示在标签里,而不是弹一个错误框。"""
    _seed_store()
    with _client(project) as client:
        response = client.get("/api/store/search", params={"kind": "agent"})
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["missing"][0]["kind"] == "agent"
    assert "格式" in body["missing"][0]["reason"]


def test_install_skill_is_visible_to_the_next_skills_read(project, fake_upstream, minimal_config):
    """**装完必须能在「技能」那一节看到** —— 这条是用户报的那个 bug 的回归。

    根因:技能落盘只解决一半。`/api/skills` 与提示词读的都是
    `runtime.top_skills`,而它按 cwd 缓存;不在 store 安装后调
    `WebState.refresh_skills()`,就会出现"GET 说它在、模型却用不上"与
    "商店里装了、技能列表里没有"。
    """
    _seed_store()
    with _client(project) as client:
        # 先把 runtime 建起来(GET /api/skills 会缓存 top_skills)
        before = [s["name"] for s in client.get("/api/skills").json()["skills"]]
        assert "ppt-generator" not in before

        assert client.post("/api/store/install", headers=YES,
                           json={"ids": ["skill:4"]}).status_code == 200

        after = [s["name"] for s in client.get("/api/skills").json()["skills"]]
        assert "ppt-generator" in after


def test_uninstall_skill_disappears_from_the_skills_read(project, fake_upstream, minimal_config):
    _seed_store()
    with _client(project) as client:
        client.post("/api/store/install", headers=YES, json={"ids": ["skill:4"]})
        assert "ppt-generator" in [s["name"] for s in client.get("/api/skills").json()["skills"]]

        assert client.post("/api/store/uninstall", headers=YES,
                           json={"id": "skill:4"}).status_code == 200
        assert "ppt-generator" not in [s["name"] for s in client.get("/api/skills").json()["skills"]]


def test_install_a2a_is_visible_to_the_a2a_section(project, fake_upstream):
    """装完 A2A 条目 → 设置面「A2A」那一节立刻能看到。

    这一节**没有后端缓存**(`/api/a2a` 每次重读 `a2a.json`),所以这条用例钉的是
    "写对了地方 + 格式被 qi-a2a 认";前端那半(进这一节重读)由组件自己保证 ——
    之前它就是挂载时拉一次,于是"装完了这节也看不到"。
    """
    _seed_store()
    with _client(project) as client:
        assert client.get("/api/a2a").json()["resolved"] == []

        assert client.post("/api/store/install", headers=YES,
                           json={"ids": ["a2a:agent-4c4af5bf"]}).status_code == 200

        body = client.get("/api/a2a").json()
    names = [entry["name"] for entry in body["resolved"]]
    assert "技能调试" in names
    entry = next(e for e in body["resolved"] if e["name"] == "技能调试")
    assert entry["disabled"] is False
    assert entry["tenant"] == "agent-4c4af5bf"
    assert entry["base_url"] == "http://10.1.43.6:6091"
    assert entry["source"] == "global"


def test_uninstall_a2a_removes_only_its_entry(project, fake_upstream):
    """只删它那一个键 —— 同文件里别人配的条目必须原样活着。"""
    from qi_store import paths as store_paths

    a2a_file = store_paths.global_agent_dir() / "a2a.json"
    a2a_file.parent.mkdir(parents=True, exist_ok=True)
    a2a_file.write_text(json.dumps({"a2aServers": {
        "mine": {"cardUrl": "http://x/a2a/agents/a/card.json"}}}), encoding="utf-8")

    _seed_store()
    with _client(project) as client:
        client.post("/api/store/install", headers=YES, json={"ids": ["a2a:agent-4c4af5bf"]})
        assert client.post("/api/store/uninstall", headers=YES,
                           json={"id": "a2a:agent-4c4af5bf"}).status_code == 200

    stored = json.loads(a2a_file.read_text(encoding="utf-8"))
    assert set(stored["a2aServers"]) == {"mine"}


# ── 别处删掉之后:商店必须自己跟上 ────────────────────────
#
# 用户报的现象:在设置面「A2A」那一节删掉条目后,商店页还标着「已安装」。
# 根因是账本只记"商店装过什么",而 A2A/MCP 那两节的删除**不经过商店**。
# 修法:**磁盘是事实来源** —— \`installed\` / \`search\` / \`check\` 都去验一遍。


def test_entry_deleted_via_the_a2a_section_shows_as_not_installed(project, fake_upstream):
    """宿主自己的删除端点(A2A 节)→ 商店立刻按"未安装"算,并列进 check 的 missing。"""
    _seed_store()
    with _client(project) as client:
        assert client.post("/api/store/install", headers=YES,
                           json={"ids": ["a2a:agent-4c4af5bf"]}).status_code == 200

        # 用户在「A2A」那一节点删除(只删 a2a.json 的键,不动账本)
        assert client.request("DELETE", "/api/a2a", headers=YES,
                              json={"scope": "global", "name": "技能调试"}).status_code == 200

        installed = client.get("/api/store/installed").json()
        assert installed["items"][0]["present"] is False
        assert "技能调试" in installed["items"][0]["missingReason"]

        items = client.get("/api/store/search", params={"kind": "a2a"}).json()["items"]
        target = next(i for i in items if i["id"] == "a2a:agent-4c4af5bf")
        assert target["installed"] is False           # 按钮该是「安装」而不是「已装」
        assert target["updateAvailable"] is False

        check = client.get("/api/store/check").json()
        row = next(r for r in check["items"] if r["id"] == "a2a:agent-4c4af5bf")
        assert row["status"] == "missing"
        assert "a2aServers.技能调试" in row["detail"]


def test_cleaning_a_missing_record_only_drops_the_ledger_row(project, fake_upstream):
    """「清理账本」= 对一条已经不存在的记录跑 uninstall:不碰别人的配置,只划掉这笔账。"""
    from qi_store import paths as store_paths

    a2a_file = store_paths.global_agent_dir() / "a2a.json"
    a2a_file.parent.mkdir(parents=True, exist_ok=True)
    a2a_file.write_text(json.dumps({"a2aServers": {
        "mine": {"cardUrl": "http://x/a2a/agents/a/card.json"}}}), encoding="utf-8")
    _seed_store()
    with _client(project) as client:
        client.post("/api/store/install", headers=YES, json={"ids": ["a2a:agent-4c4af5bf"]})
        client.request("DELETE", "/api/a2a", headers=YES,
                       json={"scope": "global", "name": "技能调试"})

        assert client.post("/api/store/uninstall", headers=YES,
                           json={"id": "a2a:agent-4c4af5bf"}).status_code == 200
        assert client.get("/api/store/installed").json()["items"] == []

    # 别人配的条目还在
    stored = json.loads(a2a_file.read_text(encoding="utf-8"))
    assert set(stored["a2aServers"]) == {"mine"}
