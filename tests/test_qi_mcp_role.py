"""qi-mcp 切片 4:按值注入 —— 角色目录的 `mcp.json` 交给 qi-mcp(E25 的最后一块)。

分工是 E25 定死的:**qi-agents 读** `<角色目录>/mcp.json`(它是 agent 目录这个自包含包的
主人),**按值**交给 qi-mcp 去连与注册。所以这里测两件事:

1. qi-mcp 侧的那个 API(`role_specs` / `mcp_entries` / `register_role_mcp`);
2. qi-agents 侧的接线:角色 `tools:` 里的 MCP 条目**换成真实工具名**进子运行清单。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "extensions" / "qi-agents"))
sys.path.insert(0, str(REPO / "extensions" / "qi-mcp"))

import pytest  # noqa: E402

import qi_mcp.role as role_mod  # noqa: E402
from qi_agent import paths  # noqa: E402
from qi_agent.extensions import ExtensionApi, ExtensionBus  # noqa: E402
from qi_agent.registry import ToolCatalog  # noqa: E402
from qi_mcp.role import (  # noqa: E402
    _split_pattern,
    mcp_entries,
    register_role_mcp,
    role_specs,
)

_SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


class FakeClient:
    def __init__(self, tools) -> None:
        self._tools = list(tools)
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        return list(self._tools)

    async def call_tool(self, name: str, args: dict) -> str:
        self.calls.append((name, args))
        return f"ok:{name}"

    async def aclose(self) -> None:
        pass


def _install(monkeypatch, tools: dict[str, list[str]], *, connected: list[str] | None = None):
    """把真连接器换成假的,并记下连了谁(`lazy` 的证据)。"""
    role_mod.reset_managers()

    async def connector(spec):
        if connected is not None:
            connected.append(spec.name)
        return FakeClient([(n, f"{n} 做什么", _SCHEMA) for n in tools.get(spec.name, [])])

    monkeypatch.setattr(role_mod, "_default_connect", lambda: connector)


def _api() -> ExtensionApi:
    return ExtensionApi(catalog=ToolCatalog(), bus=ExtensionBus(), _name="mcp")


def _env(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    return project


def _write(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


# ── server 集合:qi 两层 + 角色私有 ─────────────────────

def test_role_specs_merge_qi_layers_and_role_private(tmp_path, monkeypatch):
    """角色私有**同名覆盖** qi 级 —— 更具体的那份赢(v1 也是这个口径:agent 私有自动绑定)。"""
    project = _env(tmp_path, monkeypatch)
    _write(project / ".qi" / "mcp.json", {"gh": {"command": "project-gh"}, "shared": {"url": "u"}})
    role_dir = tmp_path / "agents" / "scout"
    _write(role_dir / "mcp.json", {"gh": {"command": "role-gh"}})

    specs, private = role_specs(role_dir, project)

    assert set(specs) == {"gh", "shared"}
    assert specs["gh"].config["command"] == "role-gh"        # 角色私有覆盖
    assert private == {"gh"}                                  # 只 gh 是私有的
    assert role_specs(None, project)[0]["gh"].config["command"] == "project-gh"


# ── `tools:` 里的 MCP 条目 ──────────────────────────────

def test_mcp_entries_pick_only_mcp_entries():
    assert mcp_entries(["read", "mcp", "mcp__gh__create_*", "grep"]) == \
        [("proxy", None), ("direct", "mcp__gh__create_*")]
    assert mcp_entries(None) == []
    assert mcp_entries([]) == []
    assert mcp_entries(["read", "grep"]) == []                # 只写内置工具 → 没 MCP
    assert mcp_entries(["mcp__gh__create_issue"]) == [("direct", "mcp__gh__create_issue")]


def test_split_pattern():
    assert _split_pattern("mcp__gh__create_*") == ("gh", "create_*")
    assert _split_pattern("mcp__gh__*") == ("gh", "*")
    assert _split_pattern("mcp__gh__create_issue") == ("gh", "create_issue")
    assert _split_pattern("mcp__gh") == ("gh", "*")            # 没有工具部分 → 全都要


# ── 注册:默认拒绝 / 代理 / 直连 ────────────────────────

@pytest.mark.asyncio
async def test_no_mcp_entries_means_no_work_at_all(tmp_path, monkeypatch):
    """角色没写 MCP 条目 → **连都不连**(默认拒绝,而且不做无用的网络/进程开销)。"""
    project = _env(tmp_path, monkeypatch)
    _write(project / ".qi" / "mcp.json", {"gh": {"command": "x"}})
    connected: list[str] = []
    _install(monkeypatch, {"gh": ["create_issue"]}, connected=connected)

    names = await register_role_mcp(_api(), role_dir=None, cwd=project,
                                    role_tools=["read", "grep"])
    assert names == []
    assert connected == []


@pytest.mark.asyncio
async def test_direct_pattern_registers_matching_tools(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(project / ".qi" / "mcp.json", {"gh": {"command": "x"}})
    _install(monkeypatch, {"gh": ["create_issue", "list_issues"]})

    api = _api()
    names = await register_role_mcp(api, role_dir=None, cwd=project,
                                    role_tools=["read", "mcp__gh__create_*"])
    assert names == ["mcp__gh__create_issue"]
    assert "mcp__gh__create_issue" in api.catalog.names
    assert "mcp__gh__list_issues" not in api.catalog.names     # 没点名的没注册


@pytest.mark.asyncio
async def test_proxy_entry_gives_the_proxy_plus_private_servers_directly(tmp_path, monkeypatch):
    """写 `mcp` → 全局代理 + **角色私有 server 直连**(全局代理看不见私有那些)。"""
    project = _env(tmp_path, monkeypatch)
    _write(project / ".qi" / "mcp.json", {"shared": {"command": "s"}})
    role_dir = tmp_path / "agents" / "scout"
    _write(role_dir / "mcp.json", {"priv": {"command": "p"}})
    _install(monkeypatch, {"shared": ["s_tool"], "priv": ["p_tool"]})

    api = _api()
    names = await register_role_mcp(api, role_dir=role_dir, cwd=project, role_tools=["mcp"])
    assert names == ["mcp", "mcp__priv__p_tool"]                # 代理 + 私有直连
    assert "mcp__shared__s_tool" not in api.catalog.names       # qi 级的靠代理,不直连


@pytest.mark.asyncio
async def test_disabled_server_is_reported_not_registered(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(project / ".qi" / "mcp.json", {"gh": {"command": "x", "disabled": True}})
    _install(monkeypatch, {"gh": ["create_issue"]})
    notes: list[str] = []

    names = await register_role_mcp(_api(), role_dir=None, cwd=project,
                                    role_tools=["mcp__gh__*"], on_note=notes.append)
    assert names == []
    assert any("gh" in n and "disabled" in n for n in notes)


@pytest.mark.asyncio
async def test_broken_role_mcp_json_is_a_note_not_a_crash(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(project / ".qi" / "mcp.json", {"gh": {"command": "x"}})
    role_dir = tmp_path / "agents" / "scout"
    role_dir.mkdir(parents=True)
    (role_dir / "mcp.json").write_text("{ 坏的", encoding="utf-8")
    _install(monkeypatch, {"gh": ["create_issue"]})
    notes: list[str] = []

    names = await register_role_mcp(_api(), role_dir=role_dir, cwd=project,
                                    role_tools=["mcp__gh__*"], on_note=notes.append)
    assert names == []
    assert any("读不了" in n for n in notes)


@pytest.mark.asyncio
async def test_collision_with_an_existing_tool_is_skipped(tmp_path, monkeypatch):
    project = _env(tmp_path, monkeypatch)
    _write(project / ".qi" / "mcp.json", {"gh": {"command": "x"}})
    _install(monkeypatch, {"gh": ["create_issue"]})
    api = _api()
    notes: list[str] = []
    from qi_agent.extensions import Tool

    async def _noop(args, ctx):
        return "ok"

    api.catalog.register(Tool("mcp__gh__create_issue", "已有的", {}, _noop))

    names = await register_role_mcp(api, role_dir=None, cwd=project,
                                    role_tools=["mcp__gh__*"], on_note=notes.append)
    assert names == []
    assert any("同名" in n for n in notes)


@pytest.mark.asyncio
async def test_manager_is_reused_across_runs(tmp_path, monkeypatch):
    """同一角色跑多次**不重连** —— manager 缓存由 qi-mcp 自己持有,连接复用挂在它身上。"""
    project = _env(tmp_path, monkeypatch)
    _write(project / ".qi" / "mcp.json", {"gh": {"command": "x"}})
    connected: list[str] = []
    _install(monkeypatch, {"gh": ["create_issue"]}, connected=connected)

    for _ in range(2):
        await register_role_mcp(_api(), role_dir=None, cwd=project, role_tools=["mcp__gh__*"])
    assert connected == ["gh"]                                  # 只连了一次


# ── qi-agents 侧的接线 ──────────────────────────────────

@pytest.mark.asyncio
async def test_subagent_swaps_mcp_entries_for_real_tool_names(tmp_path, monkeypatch):
    """角色 `tools:` 里的 `mcp__gh__*` 是**模式**,必须换成真名才进子运行清单 ——
    直接留给 runner 会被报成"未知工具"。"""
    import qi_agents.subagent as sub

    captured: list[tuple] = []

    async def fake_register(api, *, role_dir=None, cwd=None, role_tools=None, on_note=None):
        captured.append((role_dir, cwd, role_tools))
        return ["mcp__gh__create_issue"]

    monkeypatch.setattr(sub, "register_role_mcp", fake_register)

    class FakeApi:
        def __init__(self) -> None:
            self.specs: list[dict] = []

        async def runAgent(self, spec, task, abort=None):
            self.specs.append(spec)
            return "子结果"

    role_dir = tmp_path / "agents" / "scout"
    role = sub.Role(name="scout", description="侦察", prompt="你是侦察员。",
                    tools=["read", "subagent", "mcp__gh__*"],
                    path=role_dir / "agent.md")
    api = FakeApi()
    result = await sub._run_one(api, role, "查一下", SimpleNamespace(workdir=tmp_path, ui=None))

    assert result["ok"] is True
    tools = api.specs[0]["tools"]
    assert "mcp__gh__create_issue" in tools          # 模式换成了真名
    assert "mcp__gh__*" not in tools                 # 模式本身不能留给 runner
    assert "subagent" not in tools                   # 递归防护照旧
    assert "read" in tools
    assert captured[0][0] == role_dir                # 角色目录传对了(包主人点自己的成员)
    assert captured[0][2] == ["read", "subagent", "mcp__gh__*"]


@pytest.mark.asyncio
async def test_subagent_without_mcp_entries_does_not_touch_qi_mcp(tmp_path, monkeypatch):
    import qi_agents.subagent as sub

    called: list[Any] = []

    async def fake_register(*args, **kwargs):
        called.append(args)
        return []

    monkeypatch.setattr(sub, "register_role_mcp", fake_register)

    class FakeApi:
        async def runAgent(self, spec, task, abort=None):
            return "子结果"

    role = sub.Role(name="scout", description="d", prompt="p", tools=None, path=None)
    await sub._run_one(FakeApi(), role, "x", SimpleNamespace(workdir=tmp_path, ui=None))
    assert called == []                          # 继承父角色 → 一行 MCP 工作都不做
