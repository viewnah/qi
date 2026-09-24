"""系统提示词 + 内置 agent:零配置可执行、SYSTEM.md 覆盖、动态注入。

覆盖:
  1. 默认基座在代码里(`system_prompt.py`),按工具集生成「可用工具 / 指南」
  2. SYSTEM.md 覆盖层级:项目 > 用户;**整体替换**默认基座;空文件视为未配置
  3. 组装:自定义/默认基座 → 项目上下文 → 技能 → 工作目录(P-E4c 起无角色层)
  4. 项目上下文:AGENTS.override.md > AGENTS.md > CLAUDE.md,全局 + 祖先链(远 → 近)
  5. 技能:pi 式 <available_skills> XML,且没有可读文件的工具时不注入
  6. 内置 general:零 agent 目录也可执行;用户/项目版同名覆盖
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qi_agent import paths
from qi_agent.llm import ChatResponse
from qi_agent.loader import (
    load_project_context,
    resolve_base_prompt,
)
from qi_agent.models import Skill
from qi_agent.registry import ToolCatalog
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


def test_tool_list_prefers_prompt_snippet_over_description() -> None:
    """`description` 进 tool schema,`prompt_snippet` 进提示词清单 —— 两个面向。

    回落规则只此一处(`Tool.prompt_line`):没写 snippet 就退到 description,
    所以老写法(只给 description)的行为一字不变。
    """
    from qi_agent.extensions import Tool

    async def _noop(args: dict, ctx: object) -> str:
        return ""

    long_desc = "这是一段写给模型看的、比较长的工具说明。"
    bare = Tool("bare", long_desc, {"type": "object", "properties": {}}, _noop)
    snip = Tool("snip", long_desc, {"type": "object", "properties": {}}, _noop,
                prompt_snippet="一行摘要")

    text = default_base_prompt([bare, snip])
    assert f"- bare: {long_desc}" in text      # 无 snippet → 回落 description
    assert "- snip: 一行摘要" in text          # 有 snippet → 用它
    assert f"- snip: {long_desc}" not in text
    # schema 里仍然是 description(snippet 不进 schema)
    assert snip.to_llm_schema()["function"]["description"] == long_desc


def test_tool_prompt_guidelines_appear_only_when_the_tool_is_active() -> None:
    """工具自带的指南随工具启用而出现(pi 的 `promptGuidelines`)。"""
    read_line = "用 read 读文件,不要用 bash 的 cat/sed/head/tail 代替(带行号且省 token)。"

    with_read = "\n".join(build_guidelines(["read", "bash"], _tools("read", "bash")))
    assert read_line in with_read

    # 指南绑的是**实际启用**的工具:把 read 换掉后它就不该在
    without = "\n".join(build_guidelines(["bash"], _tools("bash")))
    assert read_line not in without


def test_guidelines_follow_available_tools() -> None:
    """对齐 pi:有 shell 但没有 grep/find/ls 时才提示用 shell 做文件操作,按组合分三种措辞。"""
    bash_only = ["read", "bash"]
    ps_only = ["read", "powershell"]
    both = ["read", "bash", "powershell"]
    has_ls = ["read", "bash", "powershell", "ls"]
    assert "用 bash 做文件操作:列目录、搜索、找文件" in build_guidelines(bash_only)
    assert "用 PowerShell 做文件操作:列目录、搜索、找文件" in build_guidelines(ps_only)
    assert "用 bash 或 PowerShell 做文件操作:列目录、搜索、找文件" in build_guidelines(both)
    # 有 ls/find/grep 就不要这条(pi 同条件)
    assert not [g for g in build_guidelines(has_ls) if "做文件操作" in g]
    # 无条件的三条一直在
    for names in (["read"], both, has_ls):
        assert "结论先行,简明扼要" in build_guidelines(names)


def test_guidelines_route_extension_installs_through_qi_install() -> None:
    """有 shell 才提示装扩展 —— 而且只认 `qi install`,不认裸 pip / uv。"""
    with_bash = build_guidelines(["read", "bash"])
    line = next(g for g in with_bash if "qi install" in g)
    assert "pip install" in line and "uv add" in line
    # 没有 shell 就执行不了安装命令 —— 不注入,免得诱导模型调不存在的工具
    assert not [g for g in build_guidelines(["read"]) if "qi install" in g]


def test_default_base_prompt_never_claims_bash_is_readonly() -> None:
    """防回归:bash 白名单已删除(见 design/bash-allowlist.md),提示词不得再声称只读。

    这句旧文案曾在基座里存活过一次提交(4aaedb3 漏改),所以锁死。
    """
    text = default_base_prompt(_tools()) + build_system_prompt(cwd=Path.cwd())
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

def test_custom_base_replaces_default_base(tmp_path, monkeypatch) -> None:
    """pi 的 customPrompt 语义:自定义基座**整体替换**默认基座。

    副作用(有意保留,已写进文档):默认基座里的「可用工具 / 指南」随默认基座一起消失。
    P-E4c 起**没有角色层**了 —— 角色归 qi-agents,它通过 `before_agent_start` 改提示词。
    """
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    prompt = build_system_prompt("基座XYZ", cwd=tmp_path, tools=_tools("read", "bash"))

    assert prompt.startswith("基座XYZ")
    assert "可用工具:" not in prompt
    assert "指南:" not in prompt
    assert "结论先行" not in prompt


def test_build_system_prompt_defaults_to_code_base(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    prompt = build_system_prompt(cwd=tmp_path, tools=_tools("read", "bash"))
    assert prompt.startswith("你是运行在 qi 框架中的 AI 助手。")
    assert "- read: " in prompt


def test_working_directory_is_appended_last(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    prompt = build_system_prompt(cwd=tmp_path, tools=_tools("read"))
    assert prompt.rstrip().endswith(f"当前工作目录: {tmp_path}")



# ── 4. 项目上下文(AGENTS.md,对齐 pi)────────────────────

def test_project_context_global_and_ancestors(tmp_path, monkeypatch) -> None:
    """全局最先;祖先链由远到近,**一路到文件系统根**(对齐 pi,不认 git 根)。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").write_text("全局约定", encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))

    (tmp_path / "AGENTS.md").write_text("上层约定", encoding="utf-8")   # 在 git 根**之上**
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "AGENTS.md").write_text("仓库约定", encoding="utf-8")
    sub = root / "pkg"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("包约定", encoding="utf-8")

    files = load_project_context(sub)
    assert [p.name for p, _ in files] == ["AGENTS.md"] * 4
    # 全局 → git 根**之上** → 仓库 → 子目录:证明边界不是 git 根
    assert [c for _, c in files] == ["全局约定", "上层约定", "仓库约定", "包约定"]


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

    prompt = build_system_prompt(cwd=proj, tools=_tools("read"))
    assert "<project_context>" in prompt
    assert "项目专属说明与约定:" in prompt
    assert f'path="{proj / "AGENTS.md"}"' in prompt
    assert "提交信息用中文" in prompt
    assert prompt.index("<project_context>") < prompt.rstrip().rfind("当前工作目录:")


def test_project_context_can_be_disabled_explicitly(tmp_path, monkeypatch) -> None:
    """`context_files=[]` 表示显式不注入(测试/嵌入方要确定性时用)。"""
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(tmp_path / "home"))
    prompt = build_system_prompt(cwd=tmp_path, tools=_tools("read"), context_files=[])
    assert "<project_context>" not in prompt


# ── 5. 技能:pi 式 XML + 渐进披露 ──────────────────────────

def test_skills_rendered_as_xml_with_location() -> None:
    skills = [_skill("code-review-checklist", "评审核对", "/s/code-review-checklist/SKILL.md")]
    prompt = build_system_prompt("BASE", tools=_tools("read"), skills=skills,
                                 context_files=[])
    assert "<available_skills>" in prompt
    assert "<name>code-review-checklist</name>" in prompt
    assert "<description>评审核对</description>" in prompt
    assert "<location>/s/code-review-checklist/SKILL.md</location>" in prompt
    assert "用 read 读取它的 SKILL.md 全文" in prompt
    # 渐进披露:正文不进 prompt
    assert "_body" not in prompt


def test_skills_use_bash_wording_when_read_is_absent() -> None:
    prompt = build_system_prompt("BASE", tools=_tools("bash"), skills=[_skill("s", "d", "/s/SKILL.md")], context_files=[])
    assert "用 bash 读取它的 SKILL.md 全文" in prompt


def test_skills_omitted_when_no_tool_can_read_them() -> None:
    """注入却读不到 = 诱导模型调用不存在的工具,所以整块不注入(对齐 pi)。"""
    prompt = build_system_prompt("BASE", tools=_tools("write", "edit"), skills=[_skill("s", "d", "/s/SKILL.md")], context_files=[])
    assert "<available_skills>" not in prompt
