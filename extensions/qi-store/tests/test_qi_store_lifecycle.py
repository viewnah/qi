"""P2:生命周期 —— `check` / `upgrade` / `sync`。

重点不是"能跑",而是三件**语义**:

* 版本是**内容指纹**:上游改了包(大小/时间变了)才叫"可升级";
* `upgrade` **保持原来的落盘名与 key**(否则一升级就多出一份);
* `sync` 只**装缺的**,不替你卸多的(声明是"应该有",不是"只该有")。
"""

from __future__ import annotations

import copy
import json

import httpx

from qi_store import cli, config, paths
from qi_store.errors import EXIT_CONFLICT, EXIT_OK, EXIT_USAGE
from qi_store.ledger import Ledger

from qstore_testkit import SKILLS, factory, jwt, upstream


def bumped_skills() -> dict:
    """把 skill:4 的 `file_size` 改一下 → 内容指纹变 → 上游算"新版本"。"""
    payload = copy.deepcopy(SKILLS)
    for item in payload["items"]:
        if item["id"] == 4:
            item["file_size"] = int(item["file_size"]) + 1
    return payload


def installed(project, transport=None) -> None:
    assert cli.run(["install", "skill:4", "-y"], cwd=project,
                   http_factory=factory(transport or upstream())) == EXIT_OK


def seed_login() -> None:
    config.save_auth("hikqin", {"login": "admin", "role": "admin", "access": jwt()},
                     base_url="http://h:6091")
    config.save_default("hikqin")


# ── check ──────────────────────────────────────────────


def test_check_reports_updates_gone_and_current(store_home, project, capsys):
    seed_login()
    installed(project)
    # 再装一个 a2a(它在下面的"上游"里仍然存在,应该报最新)
    cli.run(["install", "a2a:agent-4c4af5bf", "-y"], cwd=project,
            http_factory=factory(upstream()))

    capsys.readouterr()
    code = cli.run(["check"], cwd=project,
                   http_factory=factory(upstream(skills=bumped_skills())))
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "可升级" in out and "最新" in out


def test_check_reports_vanished_upstream(store_home, project, capsys):
    seed_login()
    installed(project)
    gone = copy.deepcopy(SKILLS)
    gone["items"] = [item for item in gone["items"] if item["id"] != 4]

    capsys.readouterr()
    assert cli.run(["check"], cwd=project,
                   http_factory=factory(upstream(skills=gone))) == EXIT_OK
    assert "上游不可见" in capsys.readouterr().out


def test_check_reports_declared_but_missing(store_home, project, capsys):
    seed_login()
    (project / ".qi").mkdir(parents=True, exist_ok=True)
    (project / ".qi" / "store.json").write_text(json.dumps({
        "wanted": ["skill:3", {"name": "skill:4"}],
    }), encoding="utf-8")
    installed(project)

    capsys.readouterr()
    assert cli.run(["check"], cwd=project, http_factory=factory(upstream())) == EXIT_OK
    out = capsys.readouterr().out
    assert "声明了但没装" in out
    assert "skill:3" in out


def test_check_json_shape(store_home, project, capsys):
    seed_login()
    installed(project)
    capsys.readouterr()
    assert cli.run(["check", "--json"], cwd=project,
                   http_factory=factory(upstream(skills=bumped_skills()))) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["installed"][0]["itemId"] == "skill:4"
    assert payload["installed"][0]["status"].startswith("可升级")


# ── upgrade ────────────────────────────────────────────


def test_upgrade_moves_to_the_new_version_and_keeps_the_target(store_home, project, capsys):
    seed_login()
    installed(project)
    target = paths.global_agent_dir() / "skills" / "ppt-generator"
    before = Ledger.load().find("skill:4")
    assert before is not None
    assert before is not None

    assert cli.run(["upgrade", "skill:4", "-y"], cwd=project,
                   http_factory=factory(upstream(skills=bumped_skills()))) == EXIT_OK
    after = Ledger.load().find("skill:4")
    assert after is not None
    assert after.version != before.version
    assert after.target == before.target == str(target)      # 落点不变
    assert (target / "SKILL.md").is_file()
    # 旧版被备份,不是被默默扔掉
    assert list(target.parent.glob("ppt-generator.bak-*"))


def test_upgrade_dry_run_changes_nothing(store_home, project, capsys):
    seed_login()
    installed(project)
    before = Ledger.load().find("skill:4")
    assert before is not None
    capsys.readouterr()
    assert cli.run(["upgrade", "--dry-run"], cwd=project,
                   http_factory=factory(upstream(skills=bumped_skills()))) == EXIT_OK
    assert "dry-run" in capsys.readouterr().out
    record = Ledger.load().find("skill:4")
    assert record is not None and record.version == before.version


def test_upgrade_with_nothing_to_do_says_so(store_home, project, capsys):
    seed_login()
    installed(project)
    capsys.readouterr()
    assert cli.run(["upgrade", "-y"], cwd=project, http_factory=factory(upstream())) == EXIT_OK
    assert "没有可升级" in capsys.readouterr().out


def test_upgrade_refuses_offline(store_home, project, capsys):
    seed_login()
    installed(project)
    assert cli.run(["upgrade", "-y", "--offline"], cwd=project,
                   http_factory=factory(upstream())) == EXIT_USAGE
    assert "离线" in capsys.readouterr().err


# ── sync ───────────────────────────────────────────────


def test_sync_installs_only_what_is_declared(store_home, project, capsys):
    seed_login()
    (project / ".qi").mkdir(parents=True, exist_ok=True)
    (project / ".qi" / "store.json").write_text(json.dumps({
        "wanted": ["skill:4"],
    }), encoding="utf-8")
    assert cli.run(["sync", "-y"], cwd=project, http_factory=factory(upstream())) == EXIT_OK
    assert Ledger.load().find("skill:4") is not None


def test_sync_does_not_uninstall_extras(store_home, project, capsys):
    """声明是"应该有",不是"只该有" —— 多出来的只提示,不删。"""
    seed_login()
    cli.run(["install", "a2a:agent-4c4af5bf", "-y"], cwd=project,
            http_factory=factory(upstream()))
    (project / ".qi").mkdir(parents=True, exist_ok=True)
    (project / ".qi" / "store.json").write_text(json.dumps({
        "wanted": ["skill:4"],
    }), encoding="utf-8")
    assert cli.run(["sync", "-y"], cwd=project, http_factory=factory(upstream())) == EXIT_OK
    book = Ledger.load()
    assert book.find("skill:4") is not None
    assert book.find("a2a:agent-4c4af5bf") is not None     # 没被动


def test_sync_without_declaration_explains(store_home, project, capsys):
    seed_login()
    assert cli.run(["sync"], cwd=project, http_factory=factory(upstream())) == EXIT_OK
    assert "wanted" in capsys.readouterr().out


def test_sync_dry_run_writes_nothing(store_home, project, capsys):
    seed_login()
    (project / ".qi").mkdir(parents=True, exist_ok=True)
    (project / ".qi" / "store.json").write_text(json.dumps({
        "wanted": ["skill:4"],
    }), encoding="utf-8")
    assert cli.run(["sync", "--dry-run"], cwd=project,
                   http_factory=factory(upstream())) == EXIT_OK
    assert not (paths.global_agent_dir() / "skills").exists()
    assert "dry-run" in capsys.readouterr().out


def test_sync_reports_unresolvable_declaration(store_home, project, capsys):
    seed_login()
    (project / ".qi").mkdir(parents=True, exist_ok=True)
    (project / ".qi" / "store.json").write_text(json.dumps({
        "wanted": ["skill:999"],
    }), encoding="utf-8")
    assert cli.run(["sync", "-y"], cwd=project, http_factory=factory(upstream())) == EXIT_OK
    assert "解析不了" in capsys.readouterr().err


def test_sync_project_scope_lands_in_the_repo(store_home, project, capsys):
    seed_login()
    (project / ".qi").mkdir(parents=True, exist_ok=True)
    (project / ".qi" / "store.json").write_text(json.dumps({
        "wanted": ["skill:4"],
    }), encoding="utf-8")
    assert cli.run(["sync", "-l", "-y"], cwd=project,
                   http_factory=factory(upstream())) == EXIT_OK
    assert (project / ".qi" / "skills" / "ppt-generator" / "SKILL.md").is_file()
    record = Ledger.load().find("skill:4")
    assert record is not None and record.scope == "project"
