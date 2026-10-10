"""技能的写端点:添加 / 删除 / 导入(`POST|DELETE /api/skills` + `POST /api/skills/import`)。

接口面要钉住的东西比 MCP 多一层:**技能是会进提示词的代码**。所以除了确认闸、
状态码、只改该改的那层之外,还有三条由 `qi_lab.skills` 定义的不变量:

1. 没有 description 的技能 = 不存在(扫描器直接跳过)→ 写一个看不见的技能不如 400;
2. 显示名 == 目录名(写出去的),删的时候按显示名扫 → 手改过 frontmatter 的也删得掉;
3. 同层不重名(409)—— 同层重复会让 `load_top_level_skills` 抛 LoadError → 宿主 503。

外加**一条运行时不变量**:runtime 按 cwd 缓存 `top_skills`,写完不重扫的话会出现
"GET 说它在、模型却用不上"。`test_create_is_visible_to_the_next_read` 钉的就是它
(GET 先把缓存建起来,再写,再 GET —— 没有 `refresh_skills()` 就红)。
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.workspaces import WorkspaceStore  # noqa: E402
from qi_lab.app import create_app  # noqa: E402

# starlette 的 `TestClient.delete()` 不吃 `json=`(与 `/api/mcp` 的 DELETE 用例同一个坑),
# 所以带请求体的 DELETE 一律走通用的 `client.request()`。
YES = {"X-Qi-Confirm": "yes"}


# ── 夹具 ────────────────────────────────────────────────────
#
# 隔离由 tests/conftest.py 的 `_isolate_user_state`(QI_CONFIG_DIR → tmp)与
# `minimal_config`(models.json + settings.json)兜住 —— 没有后者,`QiRuntime`
# 会因"缺默认模型"在**第一次 GET /api/skills** 时抛 ConfigError → 503。


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


def _global_root() -> Path:
    return paths.global_home() / "skills"


def _project_root(project: Path) -> Path:
    return project / ".qi" / "skills"


def _listed(client: TestClient, source: str) -> set[str]:
    return {s["name"] for s in client.get("/api/skills").json()["skills"]
            if s["source"] == source}


def _frontmatter(path: Path) -> tuple[dict, str]:
    from qi_agent.loader import split_frontmatter
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    return meta, body


# ── 确认闸与能力位 ──────────────────────────────────────────


def test_skill_writes_need_the_confirm_header(client, project):
    """三端点一律 428 —— 与写凭证、写 MCP 同一道闸。"""
    create = {"scope": "project", "name": "x", "description": "d"}
    assert client.post("/api/skills", json=create).status_code == 428
    assert client.request("DELETE", "/api/skills",
                         json={"scope": "project", "name": "x"}).status_code == 428
    assert client.post("/api/skills/import",
                       json={"scope": "project", "source": "/tmp"}).status_code == 428
    # 一个字节都没落盘
    assert not _project_root(project).exists()


def test_meta_advertises_skill_write(client):
    caps = client.get("/api/meta").json()["capabilities"]
    assert caps["skill_write"] is True
    assert caps["mcp_write"] is True          # 老前端按能力位降级,两个互不干扰


# ── 添加 ────────────────────────────────────────────────────


def test_create_lands_in_the_two_roots(client, project, minimal_config):
    """两个 scope 各落到 qi 自己的那两层(`~/.qi/agent/skills` 与 `<cwd>/.qi/skills`)。"""
    r = client.post("/api/skills", headers=YES,
                    json={"scope": "project", "name": "brave-search",
                          "description": "搜网页", "body": "先搜再答"})
    assert r.status_code == 201
    meta, body = _frontmatter(_project_root(project) / "brave-search" / "SKILL.md")
    assert meta["name"] == "brave-search" and meta["description"] == "搜网页"
    assert body == "先搜再答"

    r = client.post("/api/skills", headers=YES,
                    json={"scope": "global", "name": "mine", "description": "我的"})
    assert r.status_code == 201
    assert (_global_root() / "mine" / "SKILL.md").is_file()


def test_create_response_is_the_updated_list(client, project):
    r = client.post("/api/skills", headers=YES,
                    json={"scope": "project", "name": "a", "description": "甲"})
    assert r.status_code == 201
    assert "a" in {s["name"] for s in r.json()["skills"]}
    # 写完不留残骸(临时目录改名走了)
    assert {p.name for p in _project_root(project).iterdir()} == {"a"}


def test_create_is_visible_to_the_next_read(client, project):
    """**runtime 缓存必须被重扫**:GET 先把 runtime 建起来(缓存 `top_skills`),
    再写,再 GET —— 少了 `WebState.refresh_skills()` 这一条就是旧清单。"""
    assert "fresh" not in _listed(client, "qi-project")
    r = client.post("/api/skills", headers=YES,
                    json={"scope": "project", "name": "fresh", "description": "新"})
    assert r.status_code == 201
    assert "fresh" in _listed(client, "qi-project")


def test_same_layer_duplicate_is_409(client, project):
    """同层重名会让 `load_top_level_skills` 抛 LoadError → 宿主 503(整页挂掉)。"""
    body = {"scope": "project", "name": "dup", "description": "甲"}
    assert client.post("/api/skills", headers=YES, json=body).status_code == 201
    assert client.post("/api/skills", headers=YES, json=body).status_code == 409
    # 占了路径但不是技能(没有 SKILL.md)也一样 —— 不往人家目录里塞文件
    (_project_root(project) / "taken").mkdir(parents=True)
    r = client.post("/api/skills", headers=YES,
                    json={"scope": "project", "name": "taken", "description": "乙"})
    assert r.status_code == 409


def test_cross_layer_duplicate_is_allowed(client):
    """跨层同名是**合法覆盖**(高优先级盖低优先级),不是冲突。"""
    assert client.post("/api/skills", headers=YES,
                       json={"scope": "global", "name": "twice", "description": "g"}
                       ).status_code == 201
    assert client.post("/api/skills", headers=YES,
                       json={"scope": "project", "name": "twice", "description": "p"}
                       ).status_code == 201


@pytest.mark.parametrize("name", ["", " ", " lead", "trail ", "../evil", "a/b", "a\\b",
                                  ".", "..", ".hidden", "x\x01y", "x" * 101])
def test_bad_names_are_400_not_a_filesystem_surprise(client, name):
    r = client.post("/api/skills", headers=YES,
                    json={"scope": "project", "name": name, "description": "d"})
    assert r.status_code == 400, name


def test_missing_description_is_400(client, project):
    """没有 description 的技能扫描器**直接跳过** —— 写出来等于没写。"""
    r = client.post("/api/skills", headers=YES,
                    json={"scope": "project", "name": "invisible", "description": "  "})
    assert r.status_code == 400
    assert not (_project_root(project) / "invisible").exists()


def test_odd_descriptions_survive_a_round_trip(client, project):
    """描述里的冒号/引号/`#` 不少见 —— frontmatter 必须走 `yaml.safe_dump`,
    自己拼模板会拼出一份解析不回来的 YAML(这条就红)。名字也允许中文。"""
    desc = '含: 冒号 与 "引号" 与 #井号 与 --- 分隔符'
    r = client.post("/api/skills", headers=YES,
                    json={"scope": "project", "name": "中文技能", "description": desc,
                          "body": "正文里也有 --- 和 :"})
    assert r.status_code == 201, "中文名应该是合法的"
    meta, body = _frontmatter(_project_root(project) / "中文技能" / "SKILL.md")
    assert meta["description"] == desc
    assert body == "正文里也有 --- 和 :"


def test_unknown_scope_is_422(client):
    r = client.post("/api/skills", headers=YES,
                    json={"scope": "agents", "name": "x", "description": "d"})
    assert r.status_code == 422


# ── 删除 ────────────────────────────────────────────────────


def test_delete_removes_the_whole_skill_dir(client, project):
    root = _project_root(project)
    client.post("/api/skills", headers=YES,
                json={"scope": "project", "name": "big", "description": "带附件"})
    (root / "big" / "references").mkdir(parents=True)
    (root / "big" / "references" / "x.md").write_text("参考", encoding="utf-8")

    r = client.request("DELETE", "/api/skills", headers=YES, json={"scope": "project", "name": "big"})
    assert r.status_code == 200
    assert not (root / "big").exists()
    assert "big" not in {s["name"] for s in r.json()["skills"]}


def test_delete_finds_by_display_name_not_directory_name(client, project):
    """手改过 frontmatter 的技能:界面上显示 `renamed`,目录叫 `olddir` ——
    按显示名才删得掉,而且删的是**目录**(连同附件)。"""
    target = _project_root(project) / "olddir"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("---\nname: renamed\ndescription: 改过名\n---\n",
                                     encoding="utf-8")
    r = client.request("DELETE", "/api/skills", headers=YES, json={"scope": "project", "name": "renamed"})
    assert r.status_code == 200
    assert not target.exists()


def test_delete_404_when_not_in_that_layer(client, project):
    """`.agents/skills` 那一层**删不掉**:它不是 qi 建的(跨工具共享的家)。"""
    other = project / ".agents" / "skills" / "cross"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("---\nname: cross-skill\ndescription: 别人家的\n---\n",
                                    encoding="utf-8")
    r = client.request("DELETE", "/api/skills", headers=YES,
                      json={"scope": "project", "name": "cross-skill"})
    assert r.status_code == 404
    assert (other / "SKILL.md").is_file()

    assert client.request("DELETE", "/api/skills", headers=YES,
                         json={"scope": "project", "name": "nope"}).status_code == 404


def test_delete_then_recreate_works(client, project):
    """删掉之后同层再建不算撞名(409 只对**还在的**那一份)。"""
    body = {"scope": "project", "name": "again", "description": "d"}
    assert client.post("/api/skills", headers=YES, json=body).status_code == 201
    assert client.request("DELETE", "/api/skills", headers=YES,
                         json={"scope": "project", "name": "again"}).status_code == 200
    assert client.post("/api/skills", headers=YES, json=body).status_code == 201


# ── 导入 ────────────────────────────────────────────────────


def _skill_dir(path: Path, *, name: str, description: str = "被导进来的") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n正文\n", encoding="utf-8")
    return path


def test_import_copies_a_directory_wholesale(client, project, tmp_path):
    """整目录复制:`references/` 这类附件跟着走(技能常常带脚本/参考文件)。"""
    src = _skill_dir(tmp_path / "src", name="src-name")
    (src / "references").mkdir()
    (src / "references" / "x.md").write_text("参考", encoding="utf-8")
    source_before = (src / "SKILL.md").read_bytes()

    r = client.post("/api/skills/import", headers=YES,
                    json={"scope": "project", "source": str(src)})
    assert r.status_code == 201
    target = _project_root(project) / "src-name"
    assert (target / "references" / "x.md").read_text(encoding="utf-8") == "参考"
    assert (src / "SKILL.md").read_bytes() == source_before   # 源一个字节没动
    assert "src-name" in _listed(client, "qi-project")


def test_import_name_override_rewrites_only_the_name(client, project, tmp_path):
    """落点名与源里的 name 不一致时只改 `name` —— 否则“按显示名删”会失灵。"""
    src = _skill_dir(tmp_path / "odd-dir", name="odd")
    (src / "SKILL.md").write_text(
        "---\nname: odd\ndescription: 保持这句\n---\n\n保持这句正文\n", encoding="utf-8")

    r = client.post("/api/skills/import", headers=YES,
                    json={"scope": "project", "source": str(src), "name": "renamed"})
    assert r.status_code == 201
    meta, body = _frontmatter(_project_root(project) / "renamed" / "SKILL.md")
    assert meta == {"name": "renamed", "description": "保持这句"}
    assert body == "保持这句正文"
    # 名字一致了,所以按显示名删得掉
    assert client.request("DELETE", "/api/skills", headers=YES,
                         json={"scope": "project", "name": "renamed"}).status_code == 200


def test_import_a_single_md_wraps_it_into_a_skill_dir(client, project, tmp_path):
    src = tmp_path / "solo.md"
    src.write_text("---\nname: solo\ndescription: 单份\n---\n\n单份正文", encoding="utf-8")
    r = client.post("/api/skills/import", headers=YES,
                    json={"scope": "project", "source": str(src)})
    assert r.status_code == 201
    assert (_project_root(project) / "solo" / "SKILL.md").is_file()


def test_import_a_zip(client, project, tmp_path):
    """zip 解到临时目录后照常定位;落点名 **frontmatter 的 name 优先**,
    源里没写 name 才用 zip 自己的名字(临时目录名没有信息量)。"""
    named = tmp_path / "named.zip"
    with zipfile.ZipFile(named, "w") as zf:
        zf.writestr("SKILL.md", "---\nname: zip-name\ndescription: zip 来的\n---\n\nz\n")
        zf.writestr("references/a.md", "附件")
    r = client.post("/api/skills/import", headers=YES,
                    json={"scope": "project", "source": str(named)})
    assert r.status_code == 201
    target = _project_root(project) / "zip-name"
    assert (target / "SKILL.md").is_file()
    assert (target / "references" / "a.md").is_file()
    assert "zip-name" in _listed(client, "qi-project")

    bare = tmp_path / "pack.zip"          # 源里没有 name 字段 → 用 zip 名
    with zipfile.ZipFile(bare, "w") as zf:
        zf.writestr("SKILL.md", "---\ndescription: 没写 name\n---\n\nz\n")
    r = client.post("/api/skills/import", headers=YES,
                    json={"scope": "project", "source": str(bare)})
    assert r.status_code == 201
    assert (_project_root(project) / "pack" / "SKILL.md").is_file()


def test_import_zip_slip_is_refused(client, project, tmp_path):
    """成员名带 `..` 的包不能解出来(`extractall` 会写到目标目录外面)。"""
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../evil.md", "---\ndescription: x\n---\n")
    r = client.post("/api/skills/import", headers=YES,
                    json={"scope": "project", "source": str(evil)})
    assert r.status_code == 400
    assert "越界" in r.json()["detail"]
    assert not (project / ".qi" / "evil.md").exists()
    assert not (tmp_path / "evil.md").exists()


def test_import_without_description_is_400(client, project, tmp_path):
    """导一个看不见的技能不如拒绝:扫描器按 description 索引。"""
    src = tmp_path / "nodesc.md"
    src.write_text("---\nname: nodesc\n---\n\n正文", encoding="utf-8")
    r = client.post("/api/skills/import", headers=YES,
                    json={"scope": "project", "source": str(src)})
    assert r.status_code == 400
    assert "description" in r.json()["detail"]
    assert not (_project_root(project) / "nodesc").exists()


def test_import_shapes_that_do_not_contain_one_skill(client, project, tmp_path):
    """定位不到**唯一**技能就报 400 —— 不猜。"""
    missing = client.post("/api/skills/import", headers=YES,
                          json={"scope": "project", "source": str(tmp_path / "nope")})
    assert missing.status_code == 404

    multi = tmp_path / "multi"
    _skill_dir(multi / "a", name="a")
    _skill_dir(multi / "b", name="b")
    r = client.post("/api/skills/import", headers=YES,
                    json={"scope": "project", "source": str(multi)})
    assert r.status_code == 400
    assert "2 个技能" in r.json()["detail"]

    empty = tmp_path / "empty"; empty.mkdir()
    r = client.post("/api/skills/import", headers=YES,
                    json={"scope": "project", "source": str(empty)})
    assert r.status_code == 400
    assert "SKILL.md" in r.json()["detail"]

    binary = tmp_path / "not-a-skill.txt"; binary.write_text("x", encoding="utf-8")
    r = client.post("/api/skills/import", headers=YES,
                    json={"scope": "project", "source": str(binary)})
    assert r.status_code == 400


def test_import_into_an_existing_skill_is_409(client, project, tmp_path):
    """不覆盖:先删再导是**两步可见**的操作,静默覆盖会吞掉别人手改过的版本。"""
    src = _skill_dir(tmp_path / "s", name="s")
    assert client.post("/api/skills/import", headers=YES,
                       json={"scope": "project", "source": str(src)}).status_code == 201
    r = client.post("/api/skills/import", headers=YES,
                    json={"scope": "project", "source": str(src)})
    assert r.status_code == 409


def test_import_is_428_without_the_confirm_header(client, tmp_path):
    src = _skill_dir(tmp_path / "s2", name="s2")
    assert client.post("/api/skills/import",
                       json={"scope": "project", "source": str(src)}).status_code == 428
