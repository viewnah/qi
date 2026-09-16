"""系统提示词 + 内置 agent:零配置可执行、SYSTEM.md 覆盖、动态注入。

覆盖:
  1. 默认基座在代码里(`system_prompt.py`),按工具集生成「可用工具 / 指南」
  2. SYSTEM.md 覆盖层级:项目 > 用户;**整体替换**默认基座;空文件视为未配置
  3. 组装:自定义/默认基座 → 角色层 → 项目上下文 → 技能 → 工作目录
  4. 项目上下文:AGENTS.override.md > AGENTS.md > CLAUDE.md,全局 + 祖先链(远 → 近)
  5. 技能:pi 式 <available_skills> XML,且没有可读文件的工具时不注入
  6. 内置 general:零 agent 目录也可执行;用户/项目版同名覆盖
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qi_agent import paths
from qi_agent.dispatcher import Dispatcher
from qi_agent.llm import ChatResponse
from qi_agent.loader import (
    builtin_agents_dir,
    load_all_agents,
    load_project_context,
    resolve_base_prompt,
    scan_agent_dirs,
)
from qi_agent.models import AgentConfig, AgentUnit, Skill
from qi_agent.registry import AgentRegistry, ToolCatalog
from qi_agent.system_prompt import (
    build_guidelines,
    build_system_prompt,
    default_base_prompt,
)
from qi_agent.tools import register_builtin_tools


def _catalog() -> ToolCatalog:
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    return catalog


def _tools(*names: str):
    """真工具对象(清单里带 description,和线上一致)。"""
    catalog = _catalog()
    return catalog.resolve(list(names or sorted(catalog.names)))


def _unit(body: str = "角色正文", skills: list[Skill] | None = None,
          data_sources: list | None = None, name: str = "general") -> AgentUnit:
    return AgentUnit(
        config=AgentConfig(name=name, display_name="qi", description="d", tools=["*"]),
        source="builtin", path=Path("."), system_prompt=body,
        skills=list(skills or []), data_sources=list(data_sources or []),
    )


def _skill(name: str, description: str, path: str) -> Skill:
    return Skill(name=name, description=description, path=Path(path))


def _minimal_config(base: Path, monkeypatch) -> Path:
    """最小可运行配置:providers 在 models.json,默认模型在 settings.json。

    默认模型属于 settings.json(对齐 pi),models.json 不再提供该字段。
    """
    import json

    (base / "models.json").write_text(
        '{"providers": {"ollama": {"api": "openai-completions", '
        '"models": [{"id": "x"}]}}}', encoding="utf-8")
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"defaultProvider": "ollama", "defaultModel": "x"}), encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(base / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    return home


def _write_agent(agents_root: Path, name: str, description: str, body: str = "角色正文") -> Path:
    d = agents_root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nkeywords: []\ntools: [\"*\"]\n---\n{body}\n",
        encoding="utf-8",
    )
    return d


# ── 1. 默认基座(代码内,按工具集生成) ──────────────────────

def test_default_base_prompt_has_identity() -> None:
    text = default_base_prompt()
    assert text.strip()
    assert "qi" in text
    assert "可用工具:" in text
    assert "(无)" in text          # 无工具时清单不空着


def test_default_base_prompt_lists_resolved_tools() -> None:
    """清单就是模型可调用的全集(含每行 snippet),不是写死的名字。"""
    text = default_base_prompt(_tools("read", "bash"))
    assert "- read: " in text
    assert "- bash: " in text
    assert "- grep: " not in text           # 没给的工具不能出现


def test_guidelines_follow_available_tools() -> None:
    """对齐 pi:有 bash 但没有 grep/find/ls 时才提示用 bash 做文件操作。"""
    assert "用 bash 做文件操作:列目录、搜索、找文件" in build_guidelines(["read", "bash"])
    assert "用 bash 做文件操作:列目录、搜索、找文件" not in build_guidelines(["read", "bash", "grep"])
    assert "用 bash 做文件操作:列目录、搜索、找文件" not in build_guidelines(["read"])
    # 无条件的两条一直在
    for names in (["read"], ["read", "bash", "grep"]):
        assert "结论先行,简明扼要" in build_guidelines(names)


def test_default_base_prompt_never_claims_bash_is_readonly() -> None:
    """防回归:bash 白名单已删除(见 docs/bash-allowlist.md),提示词不得再声称只读。

    这句旧文案曾在基座里存活过一次提交(4aaedb3 漏改),所以锁死。
    """
    text = default_base_prompt(_tools()) + build_system_prompt(_unit(), cwd=Path.cwd())
    assert "只读命令" not in text
    assert "不要尝试绕过安全策略" not in text


# ── 2. SYSTEM.md:整体替换默认基座 ────────────────────────

def test_resolve_defaults_to_code_default(tmp_path, monkeypatch) -> None:
    """没有任何 SYSTEM.md → 返回空串 + builtin(调用方改用代码内默认)。"""
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    text, source = resolve_base_prompt(tmp_path)
    assert text == ""
    assert source == "builtin"


def test_user_system_md_overrides_builtin(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "SYSTEM.md").write_text("全局基座", encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    text, source = resolve_base_prompt(tmp_path)
    assert text == "全局基座"
    assert source.startswith("user:")


def test_project_system_md_overrides_user(tmp_path, monkeypatch) -> None:
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


def test_blank_system_md_falls_through_to_code_default(tmp_path, monkeypatch) -> None:
    """空文件视为未配置,不能静默降级成空提示词。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "SYSTEM.md").write_text("   \n\n  ", encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    text, source = resolve_base_prompt(tmp_path)
    assert source == "builtin"
    assert text == ""


# ── 3. 组装顺序:基座 → 角色层 → 上下文 → 技能 → cwd ──────────

def test_custom_base_replaces_default_but_keeps_role_layer(tmp_path, monkeypatch) -> None:
    """pi 的 customPrompt 语义:自定义基座整体替换默认,角色层照旧追加。

    副作用(有意保留,已写进文档):默认基座里的「可用工具 / 指南」随默认基座一起消失。
    """
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    unit = _unit(body="MARKER-ROLE")
    prompt = build_system_prompt(unit, "基座XYZ", cwd=tmp_path, tools=_tools("read", "bash"))

    assert prompt.startswith("基座XYZ")
    assert "MARKER-ROLE" in prompt
    assert "可用工具:" not in prompt
    assert "指南:" not in prompt
    assert "结论先行" not in prompt


def test_build_system_prompt_defaults_to_code_base(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    prompt = build_system_prompt(_unit(), cwd=tmp_path, tools=_tools("read", "bash"))
    assert prompt.startswith("你是运行在 qi 框架中的 AI 助手。")
    assert "- read: " in prompt


def test_working_directory_is_appended_last(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    prompt = build_system_prompt(_unit(), cwd=tmp_path, tools=_tools("read"))
    assert prompt.rstrip().endswith(f"当前工作目录: {tmp_path}")


def test_base_prompt_prepended_to_agent_role(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    unit = load_all_agents(cwd=tmp_path, catalog_names=_catalog().names)["general"]
    prompt = build_system_prompt(unit, "基座XYZ", cwd=tmp_path)
    assert prompt.startswith("基座XYZ")
    assert "默认执行者" in prompt          # 角色层仍在(标记词仅存在于 general.md)
    # 顺序:基座在前,角色在后
    assert prompt.index("基座XYZ") < prompt.index("默认执行者")


# ── 4. 项目上下文(AGENTS.md,对齐 pi)────────────────────

def test_project_context_global_and_ancestors(tmp_path, monkeypatch) -> None:
    """全局最先;祖先链由远到近(近者最后,覆盖远者的同义约定)。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").write_text("全局约定", encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)          # 祖先链止于 git 根
    (root / "AGENTS.md").write_text("仓库约定", encoding="utf-8")
    sub = root / "pkg"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("包约定", encoding="utf-8")

    files = load_project_context(sub)
    assert [p.name for p, _ in files] == ["AGENTS.md", "AGENTS.md", "AGENTS.md"]
    assert [c for _, c in files] == ["全局约定", "仓库约定", "包约定"]


def test_project_context_candidate_priority_and_fallback(tmp_path, monkeypatch) -> None:
    """每级取第一个命中:AGENTS.override.md > AGENTS.md;都没有时退到 CLAUDE.md。"""
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "AGENTS.md").write_text("被 override 顶掉", encoding="utf-8")
    (root / "AGENTS.override.md").write_text("override 生效", encoding="utf-8")
    sub = root / "pkg"
    sub.mkdir()
    (sub / "CLAUDE.md").write_text("退到 CLAUDE.md", encoding="utf-8")   # 该级没有 AGENTS.md

    assert [c for _, c in load_project_context(sub)] == ["override 生效", "退到 CLAUDE.md"]


def test_project_context_shows_up_in_prompt(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    (proj / "AGENTS.md").write_text("提交信息用中文", encoding="utf-8")

    prompt = build_system_prompt(_unit(), cwd=proj, tools=_tools("read"))
    assert "<project_context>" in prompt
    assert "项目专属说明与约定:" in prompt
    assert f'path="{proj / "AGENTS.md"}"' in prompt
    assert "提交信息用中文" in prompt
    assert prompt.index("<project_context>") < prompt.rstrip().rfind("当前工作目录:")


def test_project_context_can_be_disabled_explicitly(tmp_path, monkeypatch) -> None:
    """`context_files=[]` 表示显式不注入(测试/嵌入方要确定性时用)。"""
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    prompt = build_system_prompt(_unit(), cwd=tmp_path, tools=_tools("read"), context_files=[])
    assert "<project_context>" not in prompt


# ── 5. 技能:pi 式 XML + 渐进披露 ──────────────────────────

def test_skills_rendered_as_xml_with_location() -> None:
    unit = _unit(skills=[_skill("code-review-checklist", "评审核对", "/s/code-review-checklist/SKILL.md")])
    prompt = build_system_prompt(unit, "BASE", tools=_tools("read"), context_files=[])
    assert "<available_skills>" in prompt
    assert "<name>code-review-checklist</name>" in prompt
    assert "<description>评审核对</description>" in prompt
    assert "<location>/s/code-review-checklist/SKILL.md</location>" in prompt
    assert "用 read 读取它的 SKILL.md 全文" in prompt
    # 渐进披露:正文不进 prompt
    assert "_body" not in prompt


def test_skills_use_bash_wording_when_read_is_absent() -> None:
    unit = _unit(skills=[_skill("s", "d", "/s/SKILL.md")])
    prompt = build_system_prompt(unit, "BASE", tools=_tools("bash"), context_files=[])
    assert "用 bash 读取它的 SKILL.md 全文" in prompt


def test_skills_omitted_when_no_tool_can_read_them() -> None:
    """注入却读不到 = 诱导模型调用不存在的工具,所以整块不注入(对齐 pi)。"""
    unit = _unit(skills=[_skill("s", "d", "/s/SKILL.md")])
    prompt = build_system_prompt(unit, "BASE", tools=_tools("write", "edit"), context_files=[])
    assert "<available_skills>" not in prompt


# ── 6. 内置 general ──────────────────────────────────────

def test_builtin_agents_dir_exists() -> None:
    d = builtin_agents_dir()
    assert d is not None and (d / "general" / "agent.md").is_file()


def test_zero_config_has_general(tmp_path, monkeypatch) -> None:
    """零配置(无 ~/.qi/agents,无项目 .qi)也必须有一个可执行 agent。"""
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    units = load_all_agents(cwd=tmp_path, catalog_names=_catalog().names)
    assert "general" in units
    assert units["general"].source == "builtin"


def test_user_agent_overrides_builtin(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    _write_agent(home / "agents", "general", "用户版兜底", body="用户角色")
    units = load_all_agents(cwd=tmp_path, catalog_names=_catalog().names)
    assert units["general"].source == "user"
    assert "用户角色" in units["general"].system_prompt


def test_project_agent_overrides_user_and_builtin(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    _write_agent(home / "agents", "general", "用户版兜底", body="用户角色")
    proj = tmp_path / "proj"
    _write_agent(proj / ".qi" / "agents", "general", "项目版兜底", body="项目角色")
    found = scan_agent_dirs(proj)
    assert found["general"][1] == "project"
    units = load_all_agents(cwd=proj, catalog_names=_catalog().names)
    assert "项目角色" in units["general"].system_prompt


def test_builtin_general_is_dispatch_fallback(tmp_path, monkeypatch) -> None:
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
    """端到端:只有最小配置(models.json + settings.json),无任何 agent 目录 → 仍能执行并回话。"""
    from qi_agent.runtime import QiRuntime, RuntimeConfig
    from qi_agent.session import SessionStore

    _minimal_config(tmp_path, monkeypatch)

    class StubLLM:
        async def chat(self, messages, tools=None, temperature=None):
            return ChatResponse(text="你好,我是内置兜底。")

    sessions = SessionStore(root=tmp_path / "sessions")
    rt = QiRuntime(cwd=tmp_path, runtime_cfg=RuntimeConfig(workdir=tmp_path),
                   session_store=sessions, llm=StubLLM(), disable_router=True)
    assert rt.registry.get("general") is not None
    assert rt.base_prompt_source == "builtin"
    assert rt.base_prompt == ""                 # 无自定义基座 → 用代码内默认

    events = [e async for e in rt.stream("你好", sessions.create("t"))]
    assert not [e for e in events if e.kind == "error"]
    disp = next(e for e in events if e.kind == "dispatch")
    assert disp.agent == "general"
    assert any("内置兜底" in e.text for e in events if e.kind == "text")


@pytest.mark.asyncio
async def test_runtime_prompt_carries_tools_context_and_cwd(tmp_path, monkeypatch):
    """真跑一遍(:system 消息是拼出来的)—— 工具清单、项目上下文、cwd 都在。"""
    from qi_agent.runtime import QiRuntime, RuntimeConfig
    from qi_agent.session import SessionStore

    _minimal_config(tmp_path, monkeypatch)
    (tmp_path / "AGENTS.md").write_text("本仓库约定:提交信息用中文", encoding="utf-8")
    seen: dict = {}

    class StubLLM:
        async def chat(self, messages, tools=None, temperature=None):
            seen["system"] = next(m.content for m in messages if m.role == "system")
            return ChatResponse(text="ok")

    sessions = SessionStore(root=tmp_path / "sessions")
    rt = QiRuntime(cwd=tmp_path, runtime_cfg=RuntimeConfig(workdir=tmp_path),
                   session_store=sessions, llm=StubLLM(), disable_router=True)
    session = sessions.create("t")
    [e async for e in rt.stream("你好", session)]

    system = seen["system"]
    assert "- read: " in system                      # 工具清单来自真 catalog
    assert "AGENTS.md" in system and "提交信息用中文" in system
    assert system.rstrip().endswith(f"当前工作目录: {tmp_path}")


# ── 分层不重复:基座管通用做法,角色层只说"我是谁" ────────────

def test_each_agent_gets_only_its_own_role_layer(tmp_path, monkeypatch):
    """多 agent 不变式:共享基座,但角色层按 agent 二选一(auto 分派“换 agent = 换角色层”)。

    自定义基座时也必须成立 —— 它是分派语义的地基,不能被基座改动连带打掉。
    """
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    marks = {"writer": "你是「文档工程师」。", "code-analyst": "你是「代码分析师」。"}
    for name, mark in marks.items():
        _write_agent(tmp_path / ".qi" / "agents", name, f"{name} 的描述", body=mark)
    units = load_all_agents(cwd=tmp_path, catalog_names=_catalog().names)

    for name, own in marks.items():
        other = next(m for k, m in marks.items() if k != name)
        for base in (None, "自定义基座"):        # 默认基座与自定义基座两种情形
            prompt = build_system_prompt(units[name], base, cwd=tmp_path, tools=_tools("read"))
            assert own in prompt
            assert other not in prompt


def test_role_layer_does_not_duplicate_base_rules(tmp_path, monkeypatch):
    """角色层不应重复基座层已交代的通用做法(否则自相矛盾且浪费 token)。"""
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    unit = load_all_agents(cwd=tmp_path, catalog_names=_catalog().names)["general"]
    role = build_system_prompt(unit, "BASE_MARKER", cwd=tmp_path).split("BASE_MARKER", 1)[1]
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

    _minimal_config(tmp_path, monkeypatch)

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

    _minimal_config(tmp_path, monkeypatch)

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
