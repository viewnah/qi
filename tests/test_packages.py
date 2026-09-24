"""packages 声明层:解析、双向比对、以及「认不出的声明不静默丢」。

这一批的重点不是覆盖率,而是把三条容易退化的规矩钉住:
① 裸 URL 不许猜名字;② 声明与已装两个方向都要报;③ 认不出的声明必须浮出来,
否则「声明了但看起来没声明」是最难诊断的那种坏。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qi_agent import paths
from qi_agent.registry import HOST_DISTRIBUTION  # noqa: E402
from qi_agent.packages import (DeclaredPackage, InstalledExtension, build_report,
                               discover_declared, install_hints, normalize_name,
                               parse_declaration)


def test_normalize_name_is_pep503():
    assert normalize_name("Qi.MCP") == normalize_name("qi-mcp") == "qi-mcp"
    assert normalize_name("qi_coding_agent") == "qi-coding-agent"
    assert normalize_name("") == ""


@pytest.mark.parametrize("spec,channel,name", [
    ("pip:qi-mcp", "pip", "qi-mcp"),
    ("qi-mcp", "pip", "qi-mcp"),
    ("qi-mcp==0.1.0", "pip", "qi-mcp"),
    ("local:/tmp/ext", "local", "ext"),
    ("/tmp/my-ext", "local", "my-ext"),
    ("file:/tmp/other", "local", "other"),
    ("qi-mcp @ git+https://example.invalid/x", "pip", "qi-mcp"),
])
def test_parse_declaration_forms(spec, channel, name):
    decl = parse_declaration(spec, "settings:user")
    assert decl is not None, spec
    assert (decl.channel, decl.name) == (channel, name)


def test_parse_declaration_accepts_dict_form():
    """`settings.packages` 声明为 list[Any],结构体写法历史上允许。"""
    decl = parse_declaration({"source": "pip:qi-web"}, "settings:project")
    assert decl is not None
    assert decl.name == "qi-web" and decl.source == "settings:project"


@pytest.mark.parametrize("spec", ["", "   ", "git+https://example.invalid/x",
                                 "https://example.invalid/qi-mcp"])
def test_parse_declaration_refuses_to_guess(spec):
    """裸 URL 提不出名字:宁可不认,不猜。

    猜的后果具体可见:`git+https://host/qi-mcp` 若按“取开头的标识符”处理会得到一个
    叫 `git` 的包,它永远不会与已装的 `qi-mcp` 对上,于是每次都报「声明未安装」。
    """
    assert parse_declaration(spec, "settings:user") is None


def _decl(spec: str, source: str = "settings:user") -> DeclaredPackage:
    """走真实的解析路径造声明 —— 自己拼 name 会把 `pip:` 前缀算进名字里。"""
    decl = parse_declaration(spec, source)
    assert decl is not None, spec
    return decl


def test_report_reports_both_directions():
    installed = [
        InstalledExtension("qi-mcp", "pip", "qi-mcp 0.1.0", "user"),
        InstalledExtension("qi-web", "local", "/x/extension.py", "user"),
    ]
    report = build_report(installed, [_decl("pip:qi-mcp"), _decl("pip:qi-agents")])
    assert [d.name for d in report.missing] == ["qi-agents"]     # 声明了但装不上
    assert [i.name for i in report.undeclared] == ["qi-web"]     # 装了但没声明
    assert not report.consistent


def test_report_consistent_on_exact_match():
    report = build_report([InstalledExtension("qi-mcp", "pip", "qi-mcp 0.1.0", "user")],
                          [_decl("pip:qi-mcp")])
    assert report.consistent
    assert report.missing == [] and report.undeclared == [] and report.unparsed == []


def test_unparsed_declaration_is_surfaced_not_dropped():
    report = build_report([], [], ["git+https://example.invalid/x"])
    assert report.unparsed == ["git+https://example.invalid/x"]
    assert not report.consistent, "有认不出的声明就不能报一致"


def test_discover_declared_reads_both_scopes_project_wins(tmp_path, monkeypatch):
    from qi_agent import settings as settings_mod
    from qi_agent.settings import QiSettings

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.setattr(
        settings_mod, "load_settings_by_scope",
        lambda cwd=None: {
            "user": QiSettings(packages=["pip:qi-mcp", "git+https://example.invalid/x"]),
            "project": QiSettings(packages=["pip:qi-mcp==0.2.0", "pip:qi-web"]),
        })

    declared, unparsed = discover_declared(tmp_path)
    by_name = {d.name: d for d in declared}
    assert by_name["qi-mcp"].spec == "pip:qi-mcp==0.2.0", "同名应以项目级为准,且 spec 原样保留"
    assert by_name["qi-mcp"].source == "settings:project"
    assert by_name["qi-web"].source == "settings:project"
    assert unparsed == ["git+https://example.invalid/x"]


def test_install_hints_are_copy_pasteable():
    hints = install_hints("pip:qi-mcp")
    # 首选 qi install(它自己会挑安装器、写声明),不再直接给裸 pip 命令
    assert any(h == 'qi install "qi-mcp"' for h in hints)
    assert any(h.startswith(f"uv tool install {HOST_DISTRIBUTION} --with") for h in hints)
    # 通道前缀必须剥掉,否则用户复制到的是一条跑不通的命令
    assert all("pip:" not in h for h in hints)
