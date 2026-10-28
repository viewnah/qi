"""取货流水线:目录 → 计划 → 落盘 → 账本 → 卸载。

用 `httpx.MockTransport` 假上游(形状照 `tests/fixtures/` 里的真实响应),断言落在**磁盘**上:
装出来的 `SKILL.md` / `mcp.json` 的键 / `a2a.json` 的键,以及账本里有没有记。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from qi_store import cli, config, ledger as ledger_mod, paths, planner
from qi_store.catalog import Catalog, load as load_catalog
from qi_store.client import StoreClient
from qi_store.errors import EXIT_CONFLICT, EXIT_OK, EXIT_USAGE
from qi_store.kinds import mcp as mcp_kind
from qi_store.kinds.base import PlanContext, env_name
from qi_store.ledger import Ledger

from qstore_testkit import (   # noqa: E402  (pytest 会把 tests/ 放进 sys.path)
    A2A,
    FIXTURES,
    MCP_SERVER,
    SKILL_MD,
    SKILL_ZIP,
    SKILLS,
    factory,
    jwt,
    make_zip,
    upstream,
)


@pytest.fixture
def logged_in(store_home, project):
    """一个已登录的用户级配置(令牌是假 JWT,只为让白名单/续期路径走通)。"""
    config.save_auth("hikqin", {"login": "admin", "role": "admin", "access": jwt()},
                     base_url="http://h:6091")
    config.save_default("hikqin")
    return project


# ── 目录解析 ────────────────────────────────────────────


def _catalog(transport) -> Catalog:
    source = config.Source(name="hikqin", base_url="http://h:6091", auth={"access": jwt()})
    client = StoreClient(source, http=httpx.Client(transport=transport))
    return load_catalog(client, store_name="hikqin", allow_cache=False)


def test_catalog_projects_all_four_kinds():
    catalog = _catalog(upstream())
    kinds = {item.kind for item in catalog.items}
    assert kinds == {"agent", "skill", "mcp", "a2a"}
    skill = catalog.find("skill:4")
    assert skill.name == "ppt-generator"
    assert skill.safety["scan_status"] == "safe"


def test_bare_number_must_carry_kind():
    catalog = _catalog(upstream())
    with pytest.raises(Exception) as excinfo:
        catalog.find("4")
    assert "kind 前缀" in str(excinfo.value)


def test_ambiguous_name_reports_candidates():
    catalog = _catalog(upstream())
    with pytest.raises(Exception) as excinfo:
        catalog.find("ppt")           # 命中 ppt-generator 与 "我的ppt 制作大师"
    assert "歧义" in str(excinfo.value) or "多个" in str(excinfo.value)


def test_a2a_and_skill_use_different_keys():
    catalog = _catalog(upstream())
    a2a = [item for item in catalog.items if item.kind == "a2a"]
    assert a2a[0].id.startswith("a2a:agent-")
    assert a2a[0].meta["tenant"] == "agent-4c4af5bf"


# ── skill ──────────────────────────────────────────────


def test_install_skill_writes_directory_and_ledger(logged_in, capsys):
    code = cli.run(["install", "skill:4", "-y"], cwd=logged_in,
                   http_factory=factory(upstream()))
    assert code == EXIT_OK
    target = paths.global_agent_dir() / "skills" / "ppt-generator"
    assert (target / "SKILL.md").is_file()
    assert (target / "scripts" / "gen.py").is_file()

    record = Ledger.load().find("skill:4")
    assert record is not None
    assert record.scope == "global"
    assert record.target == str(target)
    assert str(target / "SKILL.md") in record.files


def test_install_skill_then_uninstall_cleans_up(logged_in, capsys):
    cli.run(["install", "skill:4", "-y"], cwd=logged_in, http_factory=factory(upstream()))
    target = paths.global_agent_dir() / "skills" / "ppt-generator"
    assert target.is_dir()

    assert cli.run(["uninstall", "skill:4", "-y"], cwd=logged_in,
                   http_factory=factory(upstream())) == EXIT_OK
    assert not target.exists()
    assert Ledger.load().find("skill:4") is None


def test_skill_without_description_fails_before_writing(logged_in, capsys):
    zip_without_desc = make_zip({"x/SKILL.md": "---\nname: x\n---\n\n正文\n"})
    code = cli.run(["install", "skill:4", "-y"], cwd=logged_in,
                   http_factory=factory(upstream(skill_zip=zip_without_desc)))
    assert code == EXIT_USAGE
    assert "description" in capsys.readouterr().err
    assert not (paths.global_agent_dir() / "skills" / "ppt-generator").exists()


def test_skill_with_unsupported_layout_fails(logged_in, capsys):
    # SKILL.md 在 skills/x/ 下,但根上还有别的文件 → 解不出一个能用的技能目录
    bad = make_zip({"skills/x/SKILL.md": SKILL_MD, "README.md": "hi\n"})
    code = cli.run(["install", "skill:4", "-y"], cwd=logged_in,
                   http_factory=factory(upstream(skill_zip=bad)))
    assert code == EXIT_USAGE
    assert "结构" in capsys.readouterr().err


def test_download_skill_does_not_touch_config_or_ledger(tmp_path, logged_in):
    out = tmp_path / "review"
    assert cli.run(["download", "skill:4", "-o", str(out)], cwd=logged_in,
                   http_factory=factory(upstream())) == EXIT_OK
    assert (out / "ppt-generator" / "SKILL.md").is_file()
    assert not (paths.global_agent_dir() / "skills").exists()
    assert Ledger.load().items == []


def test_download_raw_keeps_the_zip(tmp_path, logged_in):
    out = tmp_path / "review"
    cli.run(["download", "skill:4", "-o", str(out), "--raw"], cwd=logged_in,
            http_factory=factory(upstream()))
    assert (out / "ppt-generator.zip").is_file()


# ── mcp ────────────────────────────────────────────────


def test_install_mcp_writes_only_its_key(logged_in):
    mcp_file = paths.global_agent_dir() / "mcp.json"
    mcp_file.write_text(json.dumps({"mcpServers": {"mine": {"command": "x"}}}), encoding="utf-8")
    assert cli.run(["install", "mcp:3", "-y"], cwd=logged_in,
                   http_factory=factory(upstream())) == EXIT_OK
    data = json.loads(mcp_file.read_text(encoding="utf-8"))
    assert data["mcpServers"]["mine"] == {"command": "x"}       # 别人的原样活着
    entry = data["mcpServers"]["ppt-tools"]
    assert entry["type"] == "streamable-http"
    assert entry["url"] == "https://mcp.example.com/mcp"
    assert entry["headers"] == {"X-Tenant": "demo"}             # 上游给的 headers 原样抄
    # auth_config 上游没给 → 留环境变量引用,不伪造
    assert entry["auth"] == "bearer"
    assert entry["bearerTokenEnv"] == "PPT_TOOLS_TOKEN"


def test_mcp_headers_are_copied_verbatim_even_if_they_look_secret():
    """用户明确要求:上游给明文就写明文(不改写成变量、不猜)。"""
    server = dict(MCP_SERVER, headers={"Authorization": "Bearer literal-token"})
    entry, needs = mcp_kind.build_entry(server)
    assert entry["headers"] == {"Authorization": "Bearer literal-token"}
    assert needs == []                    # 已经有凭据了,不需要用户再补


def test_env_name_is_exportable():
    assert env_name("ppt-tools", "TOKEN") == "PPT_TOOLS_TOKEN"
    assert env_name("123 server", "TOKEN") == "MCP_123_SERVER_TOKEN"


def test_install_mcp_without_auth_needs_no_config(logged_in):
    server = {k: v for k, v in MCP_SERVER.items() if k != "headers"} | {"auth_type": "none"}
    assert cli.run(["install", "mcp:3", "-y"], cwd=logged_in,
                   http_factory=factory(upstream(mcp_servers=server))) == EXIT_OK
    data = json.loads((paths.global_agent_dir() / "mcp.json").read_text(encoding="utf-8"))
    record = Ledger.load().find("mcp:3")
    assert record is not None and record.needs_config == []
    assert "auth" not in data["mcpServers"]["ppt-tools"]


def test_mcp_conflict_needs_force_and_backs_up(logged_in, capsys):
    mcp_file = paths.global_agent_dir() / "mcp.json"
    mcp_file.write_text(json.dumps({"mcpServers": {"ppt-tools": {"url": "old"}}}),
                        encoding="utf-8")
    code = cli.run(["install", "mcp:3", "-y"], cwd=logged_in, http_factory=factory(upstream()))
    assert code == EXIT_CONFLICT
    assert json.loads(mcp_file.read_text(encoding="utf-8"))["mcpServers"]["ppt-tools"] == {
        "url": "old"}

    assert cli.run(["install", "mcp:3", "-y", "--force"], cwd=logged_in,
                   http_factory=factory(upstream())) == EXIT_OK
    backups = list(mcp_file.parent.glob("mcp.json.bak-*"))
    assert backups and json.loads(backups[0].read_text(encoding="utf-8"))[
        "mcpServers"]["ppt-tools"] == {"url": "old"}


def test_uninstall_mcp_removes_key_and_prunes_empty_section(logged_in):
    cli.run(["install", "mcp:3", "-y"], cwd=logged_in, http_factory=factory(upstream()))
    mcp_file = paths.global_agent_dir() / "mcp.json"
    assert mcp_file.is_file()
    assert cli.run(["uninstall", "mcp:3", "-y"], cwd=logged_in,
                   http_factory=factory(upstream())) == EXIT_OK
    # 整个文件空了(本扩展建的)就删掉,不在配置目录留一个 `{}`
    assert not mcp_file.exists()


# ── a2a ────────────────────────────────────────────────


def test_install_a2a_needs_no_login_and_uses_base_url_plus_tenant(store_home):
    """a2a 是公开端点:没登录也能列/装。落 `baseUrl + tenant`(卡片自己给的两半,不猜)。"""
    config.save_auth("hikqin", None, base_url="http://h:6091")   # 只配源,不登录
    assert cli.run(["install", "a2a:agent-4c4af5bf", "-y"],
                   http_factory=factory(upstream())) == EXIT_OK
    data = json.loads((paths.global_agent_dir() / "a2a.json").read_text(encoding="utf-8"))
    entry = data["a2aServers"]["技能调试"]
    assert entry == {"baseUrl": "http://10.1.43.6:6091", "tenant": "agent-4c4af5bf"}


# ── 计划 / 冲突 / 离线 ──────────────────────────────────


def test_same_batch_duplicate_key_is_a_conflict(logged_in):
    catalog = _catalog(upstream(mcp_servers=[MCP_SERVER, dict(MCP_SERVER, id=4)]))
    items = [item for item in catalog.items if item.kind == "mcp"]
    ctx = PlanContext(root=paths.global_agent_dir(), scope="global", store_name="hikqin",
                      base_url="http://h:6091", client=None)
    plan = planner.build(items, ctx, ledger=Ledger.load())
    assert plan.items[1].conflict == "与本批另一个条目同名"


def test_dry_run_writes_nothing(logged_in, capsys):
    assert cli.run(["install", "skill:4", "--dry-run"], cwd=logged_in,
                   http_factory=factory(upstream())) == EXIT_OK
    assert not (paths.global_agent_dir() / "skills").exists()
    assert "--dry-run" in capsys.readouterr().out


def test_search_offline_uses_cache(logged_in, capsys):
    assert cli.run(["search"], cwd=logged_in, http_factory=factory(upstream())) == EXIT_OK
    assert paths.cache_file("hikqin").is_file()
    capsys.readouterr()                            # 丢掉在线那次的表格

    def no_network(request):                       # pragma: no cover
        raise AssertionError("--offline 不该联网")

    # 用 --json 断言内容:表格会按终端宽度截断,在 CI 里不可靠
    assert cli.run(["search", "--offline", "--json"], cwd=logged_in,
                   http_factory=factory(httpx.MockTransport(no_network))) == EXIT_OK
    names = {item["name"] for item in json.loads(capsys.readouterr().out)}
    assert "ppt-generator" in names


def test_offline_install_refuses(logged_in, capsys):
    assert cli.run(["install", "skill:4", "--offline", "-y"], cwd=logged_in,
                   http_factory=factory(upstream())) == EXIT_USAGE
    assert "离线" in capsys.readouterr().err


def test_search_without_login_still_lists_a2a(store_home, capsys):
    config.save_auth("hikqin", None, base_url="http://h:6091")   # 配了源但没登录
    assert cli.run(["search"], http_factory=factory(upstream())) == EXIT_OK
    out = capsys.readouterr()
    assert "技能调试" in out.out                  # a2a 不需要登录
    assert "未登录" in out.err


def test_missing_kind_is_reported_not_silent(logged_in, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a2a/agents":
            return httpx.Response(200, json=A2A)
        return httpx.Response(401, json={"code": 401, "message": "无效的认证凭据"})

    cli.run(["search"], cwd=logged_in, http_factory=factory(httpx.MockTransport(handler)))
    captured = capsys.readouterr()
    assert "没查成" in captured.err               # 一个 kind 失败不能变成"商店是空的"
    assert "技能调试" in captured.out


def test_list_reads_ledger_offline(logged_in, capsys):
    cli.run(["install", "skill:4", "-y"], cwd=logged_in, http_factory=factory(upstream()))
    capsys.readouterr()
    assert cli.run(["list", "--json"], cwd=logged_in,
                   http_factory=factory(upstream())) == EXIT_OK
    records = json.loads(capsys.readouterr().out)
    assert records[0]["itemId"] == "skill:4" and records[0]["name"] == "ppt-generator"


def test_uninstall_unknown_id_is_not_found(logged_in, capsys):
    assert cli.run(["uninstall", "skill:999", "-y"], cwd=logged_in,
                   http_factory=factory(upstream())) == 4
    assert "账本里没有" in capsys.readouterr().err


def test_picker_refuses_non_tty(logged_in, monkeypatch, capsys):
    import io as _io
    import sys as _sys

    monkeypatch.setattr(_sys, "stdin", _io.StringIO(""))
    assert cli.run(["install", "skill"], cwd=logged_in,
                   http_factory=factory(upstream())) == EXIT_USAGE
    assert "非交互" in capsys.readouterr().err


def test_parse_indexes_handles_ranges_and_dedup():
    assert cli._parse_indexes("1,3", 5) == [1, 3]
    assert cli._parse_indexes("2-4", 5) == [2, 3, 4]
    assert cli._parse_indexes("1,1,2", 5) == [1, 2]
    assert cli._parse_indexes("4-2", 5) == [2, 3, 4]
    with pytest.raises(ValueError):
        cli._parse_indexes("9", 5)


# ── 账本 / 计划 单元 ────────────────────────────────────


def test_ledger_roundtrip(store_home):
    book = Ledger.load()
    book.upsert(ledger_mod.Record(itemId="skill:1", store="hikqin", kind="skill",
                                  name="x", version="sig:1", scope="global"))
    book.save()
    reloaded = Ledger.load()
    assert reloaded.find("skill:1") is not None
    assert reloaded.owns_target(Path("/nope")) is False


def test_project_scope_requires_confirmation(logged_in, monkeypatch, capsys):
    import io as _io
    import sys as _sys

    monkeypatch.setattr(_sys, "stdin", _io.StringIO(""))      # 非 tty
    code = cli.run(["install", "skill:4", "-l"], cwd=logged_in,
                   http_factory=factory(upstream()))
    assert code == EXIT_USAGE
    assert "项目目录" in capsys.readouterr().err


def test_install_unknown_name_suggests_search(logged_in, capsys):
    assert cli.run(["install", "nope", "-y"], cwd=logged_in,
                   http_factory=factory(upstream())) == 4
    assert "search" in capsys.readouterr().err


def test_usage_errors_and_help_return_codes(logged_in):
    """argparse 的用法错误也走同一个退出码来源(2),--help 是 0。"""
    assert cli.run(["search", "-k", "nosuch"], cwd=logged_in,
                   http_factory=factory(upstream())) == EXIT_USAGE
    assert cli.run(["install", "--nope"], cwd=logged_in,
                   http_factory=factory(upstream())) == EXIT_USAGE
    assert cli.run(["--help"], cwd=logged_in, http_factory=factory(upstream())) == EXIT_OK


def test_install_json_plan_still_guards_conflicts(logged_in, capsys):
    """`--json` 只改输出,不能绕过冲突闸门。"""
    mcp_file = paths.global_agent_dir() / "mcp.json"
    mcp_file.write_text(json.dumps({"mcpServers": {"ppt-tools": {"url": "old"}}}),
                        encoding="utf-8")
    assert cli.run(["install", "mcp:3", "--json", "-y"], cwd=logged_in,
                   http_factory=factory(upstream())) == EXIT_CONFLICT
    assert json.loads(mcp_file.read_text(encoding="utf-8"))["mcpServers"]["ppt-tools"] == {
        "url": "old"}


def test_reinstall_identical_version_is_a_noop(logged_in, capsys):
    cli.run(["install", "skill:4", "-y"], cwd=logged_in, http_factory=factory(upstream()))
    capsys.readouterr()
    assert cli.run(["install", "skill:4", "-y"], cwd=logged_in,
                   http_factory=factory(upstream())) == EXIT_OK
    assert "跳过" in capsys.readouterr().out


def test_project_scope_lands_under_the_given_cwd(logged_in, capsys):
    """`-l` 必须落到 `run(cwd=…)` 指的那个目录 —— 不是进程工作目录。

    这条以前是 bug:`_plan_ctx` 收到的是 `None` → `Path.cwd()`,于是测试里看着对、
    真实调用时把项目级技能装到了你当前所在目录。
    """
    assert cli.run(["install", "skill:4", "-l", "-y"], cwd=logged_in,
                   http_factory=factory(upstream())) == EXIT_OK
    assert (logged_in / ".qi" / "skills" / "ppt-generator" / "SKILL.md").is_file()
    record = Ledger.load().find("skill:4")
    assert record is not None and record.scope == "project"
    # 全局那一层不能被顺带写一份
    assert not (paths.global_agent_dir() / "skills" / "ppt-generator").exists()


# ── 账本 vs 磁盘:别处删掉的要如实说 ──────────────────────


def test_deleted_out_of_band_shows_as_not_installed(logged_in, capsys):
    """装完在**别处**删掉(设置面 A2A/MCP 那两节、手改 json、rm -rf 目录)。

    账本只记"商店装过什么",所以必须拿磁盘复核 —— 否则商店页面永远标"已安装"。
    """
    assert cli.run(["install", "a2a:agent-4c4af5bf", "-y"], cwd=logged_in,
                   http_factory=factory(upstream())) == EXIT_OK
    a2a_file = paths.global_agent_dir() / "a2a.json"

    # 模拟"用户在 A2A 那一节把它删了":只删键,不动账本
    data = json.loads(a2a_file.read_text(encoding="utf-8"))
    data["a2aServers"].pop("技能调试")
    a2a_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    capsys.readouterr()
    assert cli.run(["list"], cwd=logged_in, http_factory=factory(upstream())) == EXIT_OK
    assert "技能调试" in capsys.readouterr().out       # 账本里还在

    # 用 --json 断言:表格会按终端宽度换行,长状态串会被拆开,在 CI 里不可靠
    capsys.readouterr()
    assert cli.run(["check", "--json"], cwd=logged_in,
                   http_factory=factory(upstream())) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    row = payload["installed"][0]
    assert row["status"].startswith("本地已被移除")
    assert "a2aServers.技能调试" in row["status"]


def test_missing_record_can_be_cleaned_with_uninstall(logged_in, capsys):
    """「清理账本」就是对一个已经不存在的记录跑 uninstall —— 不碰别人,只划掉这笔记账。"""
    cli.run(["install", "skill:4", "-y"], cwd=logged_in, http_factory=factory(upstream()))
    target = paths.global_agent_dir() / "skills" / "ppt-generator"
    import shutil

    shutil.rmtree(target)                              # 用户手工删了目录

    assert cli.run(["uninstall", "skill:4", "-y"], cwd=logged_in,
                   http_factory=factory(upstream())) == EXIT_OK
    assert Ledger.load().find("skill:4") is None


def test_env_token_is_used_by_every_command(store_home, project, monkeypatch, capsys):
    """`QI_STORE_TOKEN` 是 CI 通道,文档说"优先于文件" —— 那就不该只有 `whoami` 认它。

    只配置源(不登录)+ 环境变量令牌 → `search` / `install` 都要能用。
    """
    config.save_auth("hikqin", None, base_url="http://h:6091")
    monkeypatch.setenv("QI_STORE_TOKEN", jwt())
    capsys.readouterr()
    assert cli.run(["search", "-k", "skill"], cwd=project,
                   http_factory=factory(upstream())) == EXIT_OK
    out = capsys.readouterr().out
    assert "ppt-generator" in out
    assert "未登录" not in out          # 有 env 令牌就不该报未登录
