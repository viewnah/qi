"""角色的删除端点:`DELETE /api/agents`。

角色由 **qi-agents** 定义与发现(`agent.md` 的 frontmatter + 正文),这个宿主只做
删除。要钉住的东西是那三条边界(见 `qi_lab.agents` 的模块说明):

1. **只删这一层** —— 项目层同名覆盖用户层;删用户层那份,项目层的要*露出来*;
2. **我管的我才删** —— 只有 `~/.qi/agent/agents/` 与 `<cwd>/.qi/agents/` 两层,
   scope 的词汇是 qi-agents 的 `user` / `project`(不是技能的 `global`,那个 422);
3. **名字是查找键,不拼路径** —— 目标由 `discover()` 给(与 `GET /api/agents`
   同一条代码路径):`name` 里带 `../` 只会查不到,手改过 frontmatter 的角色按
   **显示名**删得掉,角色目录是指向根外的符号链接时拒绝(400,不 `rmtree` 出去)。

外加两条宿主面的:确认闸(428,一个字节都不动)与能力位 `agent_write`(老前端据此
把 Agent 那一节当只读)。**读模型不回提示词正文** —— 角色的正文从来没出过宿主。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.workspaces import WorkspaceStore  # noqa: E402
from qi_lab.app import create_app  # noqa: E402

# starlette 的 `TestClient.delete()` 不吃 `json=`(与 `/api/mcp`、`/api/skills` 的
# DELETE 用例同一个坑),带请求体的 DELETE 一律走通用的 `client.request()`。
YES = {"X-Qi-Confirm": "yes"}


# ── 夹具与造角色 ────────────────────────────────────────────
#
# 隔离由 tests/conftest.py 的 `_isolate_user_state`(QI_CONFIG_DIR → tmp)兜住:
# `scope=user` 的根 `paths.global_home()/"agents"` 因此落在临时目录,不会碰到开发机
# 真实的 `~/.qi/agent/agents/`。


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / ".git").mkdir(parents=True)
    return p


@pytest.fixture
def client(project: Path, tmp_path: Path, minimal_config) -> TestClient:
    app = create_app(cwd=project, password=None, allowed_hosts=[], bind_host="127.0.0.1",
                     workspace_store=WorkspaceStore(tmp_path / "workspaces.json"))
    return TestClient(app, base_url="http://127.0.0.1")


def _user_root() -> Path:
    return paths.global_home() / "agents"


def _project_root(project: Path) -> Path:
    return project / ".qi" / "agents"


def _role(root: Path, dirname: str, name: str, description: str = "一个测试角色",
          opening: str = "") -> Path:
    """造一个角色目录:`<root>/<dirname>/agent.md`(frontmatter 缺 description 就不成立)。"""
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{opening}---\n\n你是 {name}。\n",
        encoding="utf-8")
    return d


def _listed(client: TestClient) -> dict[str, dict]:
    """读模型:`{名字: 那条记录}`(同名只显示胜出的那层)。"""
    rows = client.get("/api/agents").json()["agents"]
    return {row["name"]: row for row in rows}


def _delete(client: TestClient, scope: str, name: str):
    return client.request("DELETE", "/api/agents", headers=YES,
                          json={"scope": scope, "name": name})


# ── 确认闸与能力位 ──────────────────────────────────────────


def test_delete_needs_the_confirm_header(client, project):
    """428,而且一个字节都没动 —— 与写凭证、写 MCP、写技能同一道闸。"""
    role = _role(_user_root(), "keeper", "keeper")
    r = client.request("DELETE", "/api/agents", json={"scope": "user", "name": "keeper"})
    assert r.status_code == 428
    assert (role / "agent.md").is_file()


def test_agents_expose_opening_and_suggestions(client):
    """选择期快捷提问跟着 `/api/agents` 回来，不需要先跑一轮 AG-UI。"""
    _role(_user_root(), "scout", "scout",
          opening='opening:\n  message: "我是侦察员"\n  suggestions:\n    - "查结构"\n    - "找报错"\n')
    _role(_user_root(), "training", "training",
          opening=('opening:\n  message: "我是数据助手"\n  suggestions:\n'
                   '    - "通用问题"\n'
                   '    - title: 联合岛屿作战\n      description: 可跳过\n      items:\n'
                   '        - 查数据产品\n    - title: 联合海上机动作战\n      items:\n'
                   '        - 查参演单位\n'))
    _role(_user_root(), "plain", "plain")

    rows = _listed(client)
    assert rows["scout"]["opening"] == {
        "message": "我是侦察员", "suggestions": ["查结构", "找报错"], "groups": []}
    assert rows["training"]["opening"]["suggestions"] == ["通用问题", "查数据产品", "查参演单位"]
    assert rows["training"]["opening"]["groups"] == [
        {"title": "联合岛屿作战", "description": "可跳过", "items": ["查数据产品"]},
        {"title": "联合海上机动作战", "description": "", "items": ["查参演单位"]},
        {"title": "", "description": "", "items": ["通用问题"]},
    ]
    assert rows["plain"]["opening"] is None


def test_meta_advertises_agent_write(client):
    caps = client.get("/api/meta").json()["capabilities"]
    assert caps["agent_write"] is True
    # 别的能力位不因为新增这一位而掉(前端按位降级,少一位 = 多一节只读)
    assert caps["skill_write"] is True and caps["mcp_write"] is True


# ── 两层各自的删除 ──────────────────────────────────────────


def test_delete_user_layer_role(client, project):
    """`scope=user` → `~/.qi/agent/agents/<目录>`(隔离 home 里的那个)。"""
    role = _role(_user_root(), "scout", "scout")
    _role(_user_root(), "keeper", "keeper")            # 邻居要在

    r = _delete(client, "user", "scout")
    assert r.status_code == 200
    assert "scout" not in {a["name"] for a in r.json()["agents"]}
    assert not role.exists()
    assert (_user_root() / "keeper").is_dir()          # 只删这一个


def test_delete_project_layer_role(client, project):
    role = _role(_project_root(project), "repo-helper", "repo-helper")

    assert _delete(client, "project", "repo-helper").status_code == 200
    assert not role.exists()
    assert "repo-helper" not in _listed(client)


def test_delete_removes_the_whole_role_dir(client, project):
    """角色目录里还有属于它的私有声明(`mcp.json` 等)—— 只删 agent.md 会留下孤儿。"""
    role = _role(_user_root(), "full", "full")
    (role / "mcp.json").write_text('{"mcpServers": {"x": {"command": "x"}}}', encoding="utf-8")
    (role / "notes.md").write_text("杂记", encoding="utf-8")

    assert _delete(client, "user", "full").status_code == 200
    assert not role.exists()
    assert not (role / "mcp.json").exists()
    assert not (role / "notes.md").exists()


def test_delete_only_that_layer(client, project):
    """跨层同名是覆盖关系:删 project 那份,**user 的要露出来**而不是被一起带走。"""
    user_role = _role(_user_root(), "shared", "shared")
    _role(_project_root(project), "shared", "shared")
    assert _listed(client)["shared"]["source"] == "project"     # 项目盖住用户

    assert _delete(client, "project", "shared").status_code == 200
    assert user_role.is_dir()                                   # 用户层那份完好
    assert _listed(client)["shared"]["source"] == "user"        # 露出来了


def test_deleting_a_role_that_only_exists_elsewhere_is_404(client, project):
    """只在 user 层时按 project 删 → 404,而且那一层不许被顺手创建/删除。"""
    user_role = _role(_user_root(), "only-user", "only-user")
    assert _delete(client, "project", "only-user").status_code == 404
    assert user_role.is_dir()
    assert not _project_root(project).exists()


# ── 名字是查找键,不拼路径 ──────────────────────────────────


def test_delete_by_frontmatter_name_not_dirname(client, project):
    """界面显示的是 frontmatter 的 `name`,删就得按它删(目录名可以是别的东西)。"""
    role = _role(_user_root(), "weird-dir", "nice-name")
    assert _delete(client, "user", "weird-dir").status_code == 404     # 目录名删不到
    assert role.is_dir()
    assert _delete(client, "user", "nice-name").status_code == 200     # 显示名删得到
    assert not role.exists()


def test_path_like_name_cannot_reach_outside(client, project):
    """`name` 不进路径拼接 —— `../` 之类只会在"该层没有这个角色"上撞成 404。"""
    outside = project / "victim"
    _role(outside, "innocent", "innocent")
    before = sorted(p.name for p in outside.iterdir())

    for evil in ("../victim/innocent", "..", "../../etc", "/etc/passwd", "a/../../b"):
        assert _delete(client, "user", evil).status_code == 404
        assert _delete(client, "project", evil).status_code == 404
    assert sorted(p.name for p in outside.iterdir()) == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 符号链接")
def test_symlinked_role_dir_is_refused(client, project, tmp_path):
    """角色目录本身是指向根外的符号链接 → 400,而且**理由要说得出来**。"""
    outside = tmp_path / "outside-role"
    _role(tmp_path, "outside-role", "link-target")      # outside/agent.md
    root = _user_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        (root / "evil").symlink_to(outside, target_is_directory=True)
    except OSError as exc:                               # 沙箱/权限不给建就跳过
        pytest.skip(f"建不了符号链接: {exc}")

    r = _delete(client, "user", "link-target")
    assert r.status_code == 400
    # 拒绝的**理由**得由 `_within()` 给(中文、说清楚是哪两个路径);`rmtree` 自己
    # 也会拒绝(等价的行为),但它只会甩一句 `Cannot call rmtree on a symbolic link`,
    # 既没有根也没有目标。断言这句话 = 挡住"去掉包含性检查"这个变异。
    assert "符号链接" in r.json()["detail"]
    assert (outside / "agent.md").is_file()


def test_builtin_role_is_not_deletable_through_a_writable_layer(client):
    """内置角色随包发布,不属于 user/project 任何一个可写层 —— 按两层删都 404。"""
    assert _delete(client, "user", "训练数据").status_code == 404
    assert _delete(client, "project", "训练数据").status_code == 404
    assert "训练数据" in _listed(client)


def test_unknown_scope_is_422(client, project):
    """技能那套词汇在这里不认:`scope` 是 qi-agents 的 user / project。"""
    assert _delete(client, "global", "x").status_code == 422
    assert _delete(client, "role", "x").status_code == 422
    assert _delete(client, "", "x").status_code == 422


def test_missing_name_is_404_not_500(client, project):
    """`name` 缺省为空串 → 找不到 → 404,不炸(读模型里没有这个名字)。"""
    assert _delete(client, "user", "").status_code == 404


# ── 读写同源:写完下一次读就少一条 ─────────────────────────


def test_delete_is_visible_to_the_next_read(client, project):
    """先把读模型建起来(任何将来的缓存都会在这里留下痕迹),再删,再读。"""
    _role(_user_root(), "gone", "gone")
    assert "gone" in _listed(client)                     # GET 先看到
    assert _delete(client, "user", "gone").status_code == 200
    assert "gone" not in _listed(client)                 # 下一次 GET 就没了


def test_response_is_the_updated_agent_list(client, project):
    _role(_project_root(project), "a", "a")
    _role(_project_root(project), "b", "b")
    r = _delete(client, "project", "a")
    assert r.status_code == 200
    payload = r.json()
    assert set(payload) == {"agents", "unavailable"}     # 形状 = 读端点的形状
    assert payload["unavailable"] is False
    assert {row["name"] for row in payload["agents"]
            if row["source"] != "builtin"} == {"b"}


# ── qi-agents 没装 ──────────────────────────────────────────


def test_delete_without_qi_agents_is_503(client, monkeypatch):
    """读可以降级成 `unavailable=True`,删没有可删的对象 —— 503,不装作删成功。"""
    monkeypatch.setitem(sys.modules, "qi_agents", None)   # 让 `import qi_agents` 报 ImportError
    monkeypatch.setitem(sys.modules, "qi_agents.discovery", None)
    r = _delete(client, "user", "whatever")
    assert r.status_code == 503
    assert "qi-agents" in r.json()["detail"]
    assert client.get("/api/agents").json()["unavailable"] is True
