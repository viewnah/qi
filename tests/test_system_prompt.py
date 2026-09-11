"""基座系统提示词 + 内置 agent:零配置可执行、SYSTEM.md 覆盖、三层优先级。

覆盖:
  1. 包内置 SYSTEM.md 永远存在(基座层不为空)
  2. SYSTEM.md 覆盖层级:项目 > 用户 > 内置;空文件视为未配置
  3. 基座层替换 + 角色层追加(多 agent 语义不变)
  4. 内置 general:零 agent 目录也可执行;用户/项目版同名覆盖
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qi_agent import paths
from qi_agent.dispatcher import Dispatcher
from qi_agent.llm import ChatResponse
from qi_agent.loader import (
    builtin_agents_dir,
    builtin_system_prompt,
    load_all_agents,
    resolve_base_prompt,
    scan_agent_dirs,
)
from qi_agent.registry import AgentRegistry, ToolCatalog
from qi_agent.tools import register_builtin_tools


def _catalog() -> ToolCatalog:
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    return catalog


def _write_agent(agents_root: Path, name: str, description: str, body: str = "角色正文") -> Path:
    d = agents_root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nkeywords: []\ntools: [\"*\"]\n---\n{body}\n",
        encoding="utf-8",
    )
    return d


# ── 1. 内置基座提示词 ─────────────────────────────────────

def test_builtin_system_prompt_exists_and_nonempty():
    text = builtin_system_prompt()
    assert text.strip()
    assert "qi" in text


def test_resolve_defaults_to_builtin(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    text, source = resolve_base_prompt(tmp_path)
    assert source == "builtin"
    assert text == builtin_system_prompt()


# ── 2. SYSTEM.md 覆盖层级 ─────────────────────────────────

def test_user_system_md_overrides_builtin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "SYSTEM.md").write_text("全局基座", encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    text, source = resolve_base_prompt(tmp_path)
    assert text == "全局基座"
    assert source.startswith("user:")


def test_project_system_md_overrides_user(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "SYSTEM.md").write_text("全局基座", encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    proj = tmp_path / "proj"
    (proj / ".qi").mkdir(parents=True)
    (proj / ".qi" / "SYSTEM.md").write_text("项目基座", encoding="utf-8")
    text, source = resolve_base_prompt(proj)
    assert text == "项目基座"
    assert source.startswith("project:")


def test_blank_system_md_falls_through_to_builtin(tmp_path, monkeypatch):
    """空文件视为未配置,不能静默降级成空提示词。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "SYSTEM.md").write_text("   \n\n  ", encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    text, source = resolve_base_prompt(tmp_path)
    assert source == "builtin"
    assert text == builtin_system_prompt()


# ── 3. 基座层 + 角色层 ────────────────────────────────────

def test_base_prompt_prepended_to_agent_role(tmp_path):
    from qi_agent.runner import build_system_prompt

    unit = load_all_agents(cwd=tmp_path, catalog_names=_catalog().names)["general"]
    prompt = build_system_prompt(unit, "基座XYZ")
    assert prompt.startswith("基座XYZ")
    assert "默认执行者" in prompt          # 角色层仍在(标记词仅存在于 general.md)
    # 顺序:基座在前,角色在后
    assert prompt.index("基座XYZ") < prompt.index("默认执行者")


def test_build_system_prompt_defaults_to_builtin_base(tmp_path):
    from qi_agent.runner import build_system_prompt

    unit = load_all_agents(cwd=tmp_path, catalog_names=_catalog().names)["general"]
    prompt = build_system_prompt(unit)          # base_prompt=None → 内置基座
    assert prompt.startswith(builtin_system_prompt()[:20])


# ── 4. 内置 general ──────────────────────────────────────

def test_builtin_agents_dir_exists():
    d = builtin_agents_dir()
    assert d is not None and (d / "general" / "agent.md").is_file()


def test_zero_config_has_general(tmp_path, monkeypatch):
    """零配置(无 ~/.qi/agents,无项目 .qi)也必须有一个可执行 agent。"""
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    units = load_all_agents(cwd=tmp_path, catalog_names=_catalog().names)
    assert "general" in units
    assert units["general"].source == "builtin"


def test_user_agent_overrides_builtin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    _write_agent(home / "agents", "general", "用户版兜底", body="用户角色")
    units = load_all_agents(cwd=tmp_path, catalog_names=_catalog().names)
    assert units["general"].source == "user"
    assert "用户角色" in units["general"].system_prompt


def test_project_agent_overrides_user_and_builtin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    _write_agent(home / "agents", "general", "用户版兜底", body="用户角色")
    proj = tmp_path / "proj"
    _write_agent(proj / ".qi" / "agents", "general", "项目版兜底", body="项目角色")
    found = scan_agent_dirs(proj)
    assert found["general"][1] == "project"
    units = load_all_agents(cwd=proj, catalog_names=_catalog().names)
    assert "项目角色" in units["general"].system_prompt


def test_builtin_general_is_dispatch_fallback(tmp_path, monkeypatch):
    """dispatcher 的 L4 兜底必须能命中内置 general(这是"零配置可跑"的关键一环)。"""
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    catalog = _catalog()
    reg = AgentRegistry()
    reg.register_all(load_all_agents(cwd=tmp_path, catalog_names=catalog.names))
    d = Dispatcher(reg, router_llm=None)._fallback("随便聊聊")
    assert d.agent == "general"
    assert d.source == "fallback"


@pytest.mark.asyncio
async def test_zero_config_runtime_executes(tmp_path, monkeypatch):
    """端到端:只有 models.json,没有任何 agent 目录 → 仍能执行并回话。"""
    from qi_agent.runtime import QiRuntime, RuntimeConfig
    from qi_agent.session import SessionStore

    (tmp_path / "models.json").write_text(
        '{"defaultProvider": "ollama", "defaultModel": "x", '
        '"providers": {"ollama": {"api": "openai-completions", '
        '"models": [{"id": "x"}]}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))

    class StubLLM:
        async def chat(self, messages, tools=None, temperature=None):
            return ChatResponse(text="你好,我是内置兜底。")

    sessions = SessionStore(root=tmp_path / "sessions")
    rt = QiRuntime(cwd=tmp_path, runtime_cfg=RuntimeConfig(workdir=tmp_path),
                   session_store=sessions, llm=StubLLM(), disable_router=True)
    assert rt.registry.get("general") is not None
    assert rt.base_prompt_source == "builtin"

    events = [e async for e in rt.stream("你好", sessions.create("t"))]
    assert not [e for e in events if e.kind == "error"]
    disp = next(e for e in events if e.kind == "dispatch")
    assert disp.agent == "general"
    assert any("内置兜底" in e.text for e in events if e.kind == "text")


# ── 分层不重复:基座管通用做法,角色层只说"我是谁" ────────────

def test_role_layer_does_not_duplicate_base_rules(tmp_path):
    """角色层不应重复基座层已交代的通用做法(否则自相矛盾且浪费 token)。"""
    from qi_agent.runner import build_system_prompt

    unit = load_all_agents(cwd=tmp_path, catalog_names=_catalog().names)["general"]
    role = build_system_prompt(unit, "BASE_MARKER").split("BASE_MARKER", 1)[1]
    for duplicated in ("clarify", "read/ls/find/grep", "write/edit", "结论先行", "bash"):
        assert duplicated not in role, f"角色层重复了基座已有的 {duplicated!r}"
    # 但身份声明与"可被覆盖"说明应保留
    assert "默认执行者" in role
    assert "export general" in role


# ── 分派事件用 display_name(与 agents list 一致) ─────────────

@pytest.mark.asyncio
async def test_dispatch_event_uses_display_name(tmp_path, monkeypatch):
    """分派行应显示 display_name:内置 general 是 `qi`,不是 `general`。"""
    from qi_agent.llm import ChatResponse
    from qi_agent.runtime import QiRuntime, RuntimeConfig
    from qi_agent.session import SessionStore

    (tmp_path / "models.json").write_text(
        '{"defaultProvider": "ollama", "defaultModel": "x", '
        '"providers": {"ollama": {"api": "openai-completions", '
        '"models": [{"id": "x"}]}}}', encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))

    class StubLLM:
        async def chat(self, messages, tools=None, temperature=None):
            return ChatResponse(text="ok")

    sessions = SessionStore(root=tmp_path / "sessions")
    rt = QiRuntime(cwd=tmp_path, runtime_cfg=RuntimeConfig(workdir=tmp_path),
                   session_store=sessions, llm=StubLLM(), disable_router=True)
    events = [e async for e in rt.stream("你好", sessions.create("t"))]
    disp = next(e for e in events if e.kind == "dispatch")

    assert disp.agent == "general"              # 机器标识仍是 name
    assert disp.data["display_name"] == "qi"    # 展示名
    assert disp.text.startswith("qi ")          # 渲染串用展示名
    assert "general" not in disp.text


@pytest.mark.asyncio
async def test_dispatch_entry_persists_display_name(tmp_path, monkeypatch):
    """展示名要落盘到 dispatch entry —— 回放靠它,不回放时查 registry。"""
    from qi_agent.llm import ChatResponse
    from qi_agent.runtime import QiRuntime, RuntimeConfig
    from qi_agent.session import SessionStore

    (tmp_path / "models.json").write_text(
        '{"defaultProvider": "ollama", "defaultModel": "x", '
        '"providers": {"ollama": {"api": "openai-completions", '
        '"models": [{"id": "x"}]}}}', encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))

    class StubLLM:
        async def chat(self, messages, tools=None, temperature=None):
            return ChatResponse(text="ok")

    sessions = SessionStore(root=tmp_path / "sessions")
    rt = QiRuntime(cwd=tmp_path, runtime_cfg=RuntimeConfig(workdir=tmp_path),
                   session_store=sessions, llm=StubLLM(), disable_router=True)
    session = sessions.create("t")
    [e async for e in rt.stream("你好", session)]

    disp = next(e for e in session.entries if e.get("type") == "dispatch")
    assert disp["agent"] == "general"
    assert disp["display_name"] == "qi"

    # 重新从磁盘读回(模拟 qi sessions show),展示名仍在
    reloaded = sessions.get(session.id)
    assert reloaded is not None
    disp2 = next(e for e in reloaded.entries if e.get("type") == "dispatch")
    assert disp2["display_name"] == "qi"
