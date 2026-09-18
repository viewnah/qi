"""P8:插件消费型配置(data_sources)动态装载门控 + MCP 解析/工具桥。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent.config import ConfigError  # noqa: E402
from qi_agent.loader import (  # noqa: E402
    LoadError,
    load_agent_dir,
    load_all_agents,
    load_mcp_scopes,
    mcp_name_index,
)
from qi_agent.registry import CapabilityRegistry, PluginApi, ToolCatalog  # noqa: E402
from qi_agent.tools import register_builtin_tools  # noqa: E402


def _agent_with_ds(base: Path) -> Path:
    d = base / "db-analyst"
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.md").write_text(
        "---\nname: db-analyst\ndescription: 查数据库。适用:数据查询。\nkeywords: [查询, sql]\n"
        'tools: ["*"]\n---\n你是 db 分析师。\n', encoding="utf-8")
    (d / "data_sources.json").write_text(json.dumps({
        "dataSources": [{"id": "orders", "type": "mysql", "dsn": "{env:ORDERS_DSN}",
                         "description": "订单库"}]}, ensure_ascii=False), encoding="utf-8")
    return d


def test_data_sources_ignored_without_plugin(tmp_path):
    """插件缺席:data_sources.json 不装载、不校验、不报错。"""
    d = _agent_with_ds(tmp_path)
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    unit = load_agent_dir(d, "user", catalog.names, has_data_source_provider=False)
    assert unit.data_sources == []


def test_data_sources_loaded_and_validated_with_plugin(tmp_path):
    """插件在场:装载 + type 校验(无支持类型 → 报错)。"""
    d = _agent_with_ds(tmp_path)
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    # 插件声明消费 data_sources,支持 mysql
    caps = CapabilityRegistry()
    api = PluginApi(catalog=catalog, _name="db-tools")
    api.provides_config("data_sources", ["mysql", "postgresql"])
    caps.merge(api)
    unit = load_agent_dir(d, "user", catalog.names,
                          has_data_source_provider=caps.has_provider("data_sources"),
                          ds_types=caps.types("data_sources"))
    assert len(unit.data_sources) == 1
    assert unit.data_sources[0].type == "mysql"
    # 不支持的 type → 报错
    (d / "data_sources.json").write_text(json.dumps({
        "dataSources": [{"id": "x", "type": "neo4j", "dsn": "{env:X}"}]}), encoding="utf-8")
    with pytest.raises(LoadError, match="无插件支持"):
        load_agent_dir(d, "user", catalog.names,
                       has_data_source_provider=True, ds_types=caps.types("data_sources"))


def test_plaintext_dsn_rejected(tmp_path):
    d = _agent_with_ds(tmp_path)
    (d / "data_sources.json").write_text(json.dumps({
        "dataSources": [{"id": "x", "type": "mysql", "dsn": "mysql://root:pw12345@host/db"}]}), encoding="utf-8")
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    with pytest.raises(LoadError):
        load_agent_dir(d, "user", catalog.names, has_data_source_provider=True)


def test_private_mcp_parsed(tmp_path):
    d = _agent_with_ds(tmp_path)
    (d / "mcp.json").write_text(json.dumps({
        "mcpServers": {"github": {"type": "streamable-http", "url": "https://api.githubcopilot.com/mcp/"}}}))
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    unit = load_agent_dir(d, "user", catalog.names)
    assert [s.name for s in unit.mcp_private] == ["github"]
    assert unit.mcp_private[0].config["url"].startswith("https://")


def test_directory_plugin_discovery(tmp_path, monkeypatch):
    """本地目录插件通道:plugins/<name>/plugin.py 被发现并提供工具 + 能力。"""
    from qi_agent import paths

    home = tmp_path / "home"
    plugin_dir = home / "plugins" / "db-tools"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(
        "from qi_agent.registry import Tool\n"
        "def register(api):\n"
        "    api.add_tool(Tool('db_query', '只读查询', {'type':'object','properties':{}}, None))\n"
        "    api.provides_config('data_sources', ['mysql'])\n", encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    from qi_agent.registry import discover_plugins

    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    caps = CapabilityRegistry()
    plugins = discover_plugins(catalog, caps)
    assert "db-tools" in plugins
    assert catalog.get("db_query") is not None
    assert caps.has_provider("data_sources")
    assert caps.types("data_sources") == {"mysql"}


def test_plugin_required_for_data_source_load_all(tmp_path, monkeypatch):
    """动态装载端到端:插件在场才装载 data_sources;缺席则跳过。"""
    from qi_agent import paths

    home = tmp_path / "home"
    d = _agent_with_ds(home / "agents")
    # 修正:该 agent 目录里再放一个普通兄弟避免 name mismatch(仅一个即可)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    units = load_all_agents(cwd=tmp_path, catalog_names=catalog.names,
                            has_data_source_provider=False)
    assert units["db-analyst"].data_sources == []


# ── 全局 / 项目 mcp.json(声明表 + 门控)──────────────────────
#
# 为什么这两层要单独测:agent 私有那份是"写在谁目录里就归谁",而全局/项目那两份是
# **一张声明表** —— 谁能看见由 agent.md 的 `mcp_servers` 决定(凭证敏感 → 默认无、
# 必须显式声明)。门控错了会出现"某个 agent 悄悄多了一个带凭证的 server"。

def _mcp_files(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """隔离环境:返回 (全局 mcp.json 路径, 项目 mcp.json 路径)。

    项目根靠 `.git` 锚定(与 find_project_root 一致),否则会一路向上找到真实仓库。
    """
    from qi_agent import paths

    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    return home / "mcp.json", proj / ".qi" / "mcp.json"


def test_scope_table_is_project_over_global(tmp_path, monkeypatch):
    """同名 server:项目覆盖全局(与 settings/agent 的层级一致)。"""
    g, p = _mcp_files(tmp_path, monkeypatch)
    g.write_text(json.dumps({"mcpServers": {"github": {"type": "streamable-http",
                                                       "url": "https://global.example/mcp"}}}))
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"mcpServers": {"github": {"type": "streamable-http",
                                                       "url": "https://project.example/mcp"}}}))
    proj = p.parent.parent
    table = load_mcp_scopes(proj)
    assert [scope for scope, _f, _s in table] == ["global", "project"]
    assert all(f.is_file() for _scope, f, _s in table)
    assert mcp_name_index(proj)["github"].config["url"] == "https://project.example/mcp"


def test_declared_mcp_is_gated_by_the_agent(tmp_path, monkeypatch):
    """全局/项目里的 server,**声明才绑**;没声明的 agent 一个都拿不到。"""
    g, p = _mcp_files(tmp_path, monkeypatch)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"mcpServers": {
        "github": {"type": "streamable-http", "url": "https://x/mcp"},
        "db": {"type": "stdio", "command": "npx"}}}))
    proj = p.parent.parent
    home = tmp_path / "home"

    def _agent(name: str, extra: str = "") -> Path:
        d = home / "agents" / name
        d.mkdir(parents=True)
        (d / "agent.md").write_text(
            f"---\nname: {name}\ndescription: 描述。\nkeywords: [k]\ntools: [\"*\"]\n{extra}---\n正文\n")
        return d

    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    declared = load_agent_dir(_agent("wants", "mcp_servers: [github]\n"), "user",
                              catalog.names, mcp_table=mcp_name_index(proj))
    assert [s.name for s in declared.mcp_declared] == ["github"]
    assert declared.mcp_private == []

    silent = load_agent_dir(_agent("quiet"), "user", catalog.names,
                            mcp_table=mcp_name_index(proj))
    assert silent.mcp_declared == [] and silent.mcp_private == []


def test_declared_mcp_unknown_name_is_an_error(tmp_path, monkeypatch):
    """声明了但表里没有 → 报错(不静默失效),并把可用名字列出来。"""
    g, p = _mcp_files(tmp_path, monkeypatch)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"mcpServers": {"github": {"type": "streamable-http",
                                                       "url": "https://x/mcp"}}}))
    proj = p.parent.parent
    d = tmp_path / "home" / "agents" / "typo"
    d.mkdir(parents=True)
    (d / "agent.md").write_text(
        "---\nname: typo\ndescription: 描述。\nkeywords: [k]\ntools: [\"*\"]\n"
        "mcp_servers: [githbu]\n---\n正文\n")
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    with pytest.raises(LoadError) as exc:
        load_agent_dir(d, "user", catalog.names, mcp_table=mcp_name_index(proj))
    assert "githbu" in str(exc.value) and "github" in str(exc.value)


def test_declared_mcp_not_resolved_without_scope_context(tmp_path, monkeypatch):
    """`mcp_table=None`(import 校验):只校验形状,不解析名字 —— 因为"要装到哪"未知。"""
    _mcp_files(tmp_path, monkeypatch)
    d = tmp_path / "home" / "agents" / "portable"
    d.mkdir(parents=True)
    (d / "agent.md").write_text(
        "---\nname: portable\ndescription: 描述。\nkeywords: [k]\ntools: [\"*\"]\n"
        "mcp_servers: [whatever]\n---\n正文\n")
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    unit = load_agent_dir(d, "user", catalog.names)      # 不传 mcp_table
    assert unit.mcp_declared == []


def test_load_all_agents_wires_the_scope_table(tmp_path, monkeypatch):
    """端到端:load_all_agents 自己把两张表合起来喂给每个 agent。"""
    g, p = _mcp_files(tmp_path, monkeypatch)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"mcpServers": {"github": {"type": "streamable-http",
                                                       "url": "https://x/mcp"}}}))
    proj = p.parent.parent
    d = tmp_path / "home" / "agents" / "wired"
    d.mkdir(parents=True)
    (d / "agent.md").write_text(
        "---\nname: wired\ndescription: 描述。\nkeywords: [k]\ntools: [\"*\"]\n"
        "mcp_servers: [github]\n---\n正文\n")
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    units = load_all_agents(cwd=proj, catalog_names=catalog.names)
    assert [s.name for s in units["wired"].mcp_declared] == ["github"]
