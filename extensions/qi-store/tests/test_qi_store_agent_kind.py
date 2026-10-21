"""`agent` kind:hikqin 的模板蓝图 → qi 的角色目录。

这个 kind 的转换点比别的多,所以每条都单独钉:

* frontmatter 写什么、什么时候**不**写 `model:` / `tools:`;
* 私有 `mcp.json` 与 `includeTools`(hikqin 的 `tool_names`);
* 内联 base64 的技能落到 `<角色>/skills/<名>/`;
* 子智能体**平铺成同级角色**(qi 没有嵌套),同名要跳过而不是互盖;
* 丢弃的段落(`opening` / `data_sources` / `config`)要在计划里说出来;
* 卸载要把主角色 + 子角色一起清掉。
"""

from __future__ import annotations

import json

from qi_store import cli, config, paths
from qi_store.errors import EXIT_OK, EXIT_USAGE
from qi_store.ledger import Ledger

from qstore_testkit import AGENT_TEMPLATE_RICH, factory, jwt, upstream


def seed(project, *, models: bool = True) -> None:
    config.save_auth("hikqin", {"login": "admin", "role": "admin", "access": jwt()},
                     base_url="http://h:6091")
    config.save_default("hikqin")
    if models:
        # 与合成蓝图里的 model 对齐:provider=stub / id=m1
        (paths.global_agent_dir() / "models.json").write_text(json.dumps({
            "providers": {"stub": {"api": "openai-completions",
                                   "models": [{"id": "m1"}]}},
        }), encoding="utf-8")


def agents_root():
    return paths.global_agent_dir() / "agents"


def frontmatter(role) -> dict[str, str]:
    text = (role / "agent.md").read_text(encoding="utf-8")
    assert text.startswith("---")
    block = text.split("---", 2)[1]
    fields = {}
    for line in block.strip().splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def body(role) -> str:
    return (role / "agent.md").read_text(encoding="utf-8").split("---", 2)[2].strip()


# ── 转换 ───────────────────────────────────────────────


def test_install_writes_a_self_contained_role(store_home, project):
    seed(project)
    assert cli.run(["install", "agent:7", "-y"], cwd=project,
                   http_factory=factory(upstream())) == EXIT_OK

    role = agents_root() / "ppt-factory"
    assert (role / "agent.md").is_file()
    fields = frontmatter(role)
    assert fields["name"] == "ppt-factory"
    assert fields["description"] == "拆大纲再排版的 PPT 流水线"
    assert fields["model"] == "stub/m1"          # 本地 models.json 里有 → 写上
    assert body(role) == AGENT_TEMPLATE_RICH["data"]["content"]["agent"]["system_prompt"]


def test_mcp_servers_become_private_mcp_json_with_include_tools(store_home, project):
    seed(project)
    cli.run(["install", "agent:7", "-y"], cwd=project, http_factory=factory(upstream()))

    role = agents_root() / "ppt-factory"
    servers = json.loads((role / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    entry = servers["ppt-tools"]
    assert entry["type"] == "streamable-http"
    assert entry["url"] == "https://mcp.example.com/mcp"
    assert entry["headers"] == {"X-Tenant": "demo"}          # 上游给的 headers 原样抄
    assert entry["auth"] == "bearer"                          # auth_config 没下发 → 引用
    assert entry["bearerTokenEnv"] == "PPT_TOOLS_TOKEN"
    assert entry["includeTools"] == ["render", "export"]      # hikqin 的 tool_names

    # **有 MCP 就必须写 tools**:qi-agents 的语义是"不写 = 一行 MCP 工作都不做",
    # 而写了就等于"点名的才有",所以内置工具也要列上。
    tools = frontmatter(role)["tools"]
    assert "mcp__ppt-tools__*" in tools
    assert "read" in tools and "bash" in tools
    assert "subagent" in tools                                # 有子智能体

    record = Ledger.load().find("agent:7")
    assert record is not None
    assert record.needs_config == ["PPT_TOOLS_TOKEN"]


def test_inline_skills_land_under_the_role(store_home, project):
    seed(project)
    cli.run(["install", "agent:7", "-y"], cwd=project, http_factory=factory(upstream()))

    skill = agents_root() / "ppt-factory" / "skills" / "ppt-style"
    assert "description: PPT 配色与版式规范" in (skill / "SKILL.md").read_text(encoding="utf-8")
    assert (skill / "scripts" / "check.py").read_text(encoding="utf-8") == 'print("ok")\n'


def test_sub_agents_become_siblings(store_home, project):
    seed(project)
    cli.run(["install", "agent:7", "-y"], cwd=project, http_factory=factory(upstream()))

    outline = agents_root() / "outline-writer"
    assert (outline / "agent.md").is_file()
    assert frontmatter(outline)["description"] == "只写大纲"
    assert body(outline).startswith("你只负责把需求写成大纲")
    # 子角色没有 MCP → **不写 tools**(继承父),也不该有 mcp.json
    assert "tools" not in frontmatter(outline)
    assert not (outline / "mcp.json").exists()


def test_dropped_sections_are_stated_in_the_plan(store_home, project, capsys):
    """`opening` / `data_sources` / `config` 在 qi 里没有对应物 —— 装之前就说清。"""
    seed(project)
    capsys.readouterr()
    cli.run(["install", "agent:7", "--dry-run"], cwd=project,
            http_factory=factory(upstream()))
    out = capsys.readouterr().out
    assert "已丢弃" in out
    assert "数据源" in out and "开场白" in out


def test_model_is_skipped_when_the_local_models_file_lacks_it(store_home, project, capsys):
    seed(project, models=False)
    capsys.readouterr()
    # 用 --json 看 note:表格会按终端宽度换行,长说明在 CI 里断言不可靠
    assert cli.run(["install", "agent:7", "--json", "--dry-run"], cwd=project,
                   http_factory=factory(upstream())) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert "models.json" in payload["plan"][0]["note"]        # 说了为什么没写
    assert cli.run(["install", "agent:7", "-y"], cwd=project,
                   http_factory=factory(upstream())) == EXIT_OK
    role = agents_root() / "ppt-factory"
    assert "model" not in frontmatter(role)


def test_role_without_mcp_omits_tools(store_home, project):
    """没有 MCP 时**不写** `tools:`(= 继承父)。写了就是收窄,没必要就别动。"""
    seed(project)
    detail = json.loads(json.dumps(AGENT_TEMPLATE_RICH))
    detail["data"]["content"]["agent"]["mcp_services"] = []
    detail["data"]["content"]["agent"]["skills"] = []
    cli.run(["install", "agent:7", "-y"], cwd=project,
            http_factory=factory(upstream(agent_detail=detail)))
    role = agents_root() / "ppt-factory"
    assert "tools" not in frontmatter(role)
    assert not (role / "mcp.json").exists()


def test_skill_without_description_is_skipped(store_home, project, capsys):
    """qi 的硬规则:没有 description 的技能等于不存在 —— 装了也白装,所以跳过。"""
    seed(project)
    detail = json.loads(json.dumps(AGENT_TEMPLATE_RICH))
    detail["data"]["content"]["agent"]["skills"][0]["description"] = ""
    capsys.readouterr()
    cli.run(["install", "agent:7", "-y"], cwd=project,
            http_factory=factory(upstream(agent_detail=detail)))
    assert not (agents_root() / "ppt-factory" / "skills").exists()
    assert "description" in capsys.readouterr().out


def test_sub_agent_with_the_same_name_is_skipped(store_home, project, capsys):
    """同名互盖的症状是"装了 A、跑成 B"—— 宁可少装一个并说出来。"""
    seed(project)
    detail = json.loads(json.dumps(AGENT_TEMPLATE_RICH))
    detail["data"]["content"]["agent"]["sub_agents"][0]["name"] = "ppt-factory"
    capsys.readouterr()
    cli.run(["install", "agent:7", "-y"], cwd=project,
            http_factory=factory(upstream(agent_detail=detail)))
    assert "同名" in capsys.readouterr().out
    # 主角色没被子智能体覆盖
    assert frontmatter(agents_root() / "ppt-factory")["description"] == "拆大纲再排版的 PPT 流水线"


# ── 目录 / 账本 / 卸载 ─────────────────────────────────


def test_agent_appears_in_the_catalog(store_home, project, capsys):
    seed(project)
    capsys.readouterr()
    assert cli.run(["search", "-k", "agent"], cwd=project,
                   http_factory=factory(upstream())) == EXIT_OK
    out = capsys.readouterr().out
    assert "多智能体 PPT 工厂" in out and "agent:7" in out


def test_uninstall_removes_main_and_sub_roles(store_home, project):
    seed(project)
    cli.run(["install", "agent:7", "-y"], cwd=project, http_factory=factory(upstream()))
    assert (agents_root() / "ppt-factory").is_dir()
    assert (agents_root() / "outline-writer").is_dir()

    assert cli.run(["uninstall", "agent:7", "-y"], cwd=project,
                   http_factory=factory(upstream())) == EXIT_OK
    assert not (agents_root() / "ppt-factory").exists()
    assert not (agents_root() / "outline-writer").exists()
    assert not agents_root().exists()          # 空目录也清掉
    assert Ledger.load().find("agent:7") is None


def test_existing_role_dir_is_a_conflict(store_home, project, capsys):
    """目标已存在且不是本账本装的 → 冲突(退出码 5),不静默覆盖。"""
    seed(project)
    role = agents_root() / "ppt-factory"
    role.mkdir(parents=True)
    (role / "agent.md").write_text("---\nname: ppt-factory\ndescription: 我自己的\n---\n我的\n",
                                   encoding="utf-8")
    assert cli.run(["install", "agent:7", "-y"], cwd=project,
                   http_factory=factory(upstream())) == 5
    assert "我自己的" in (role / "agent.md").read_text(encoding="utf-8")


def test_download_is_refused_for_agents(store_home, project, capsys, tmp_path):
    """agent 是多文件目录,`download`(单件搬运)对它没意义 —— 明确拒绝,别静默不做事。"""
    seed(project)
    assert cli.run(["download", "agent:7", "-o", str(tmp_path / "out")], cwd=project,
                   http_factory=factory(upstream())) == EXIT_USAGE
    assert "download" in capsys.readouterr().err


# ── 格式防护 ───────────────────────────────────────────


def test_unknown_format_version_is_refused(store_home, project, capsys):
    seed(project)
    detail = json.loads(json.dumps(AGENT_TEMPLATE_RICH))
    detail["data"]["content"]["format_version"] = 2
    assert cli.run(["install", "agent:7", "-y"], cwd=project,
                   http_factory=factory(upstream(agent_detail=detail))) == EXIT_USAGE
    assert "format_version" in capsys.readouterr().err


def test_empty_system_prompt_is_refused(store_home, project, capsys):
    seed(project)
    detail = json.loads(json.dumps(AGENT_TEMPLATE_RICH))
    detail["data"]["content"]["agent"]["system_prompt"] = "   "
    assert cli.run(["install", "agent:7", "-y"], cwd=project,
                   http_factory=factory(upstream(agent_detail=detail))) == EXIT_USAGE
    assert "system_prompt" in capsys.readouterr().err


def test_dropped_labels_are_not_repeated(store_home, project, capsys):
    """`opening_enabled` 与 `opening_message` 是同一件事,计划里只该出现一次"开场白"。"""
    from qi_store.kinds import agent as agent_kind

    dropped = agent_kind._dropped(AGENT_TEMPLATE_RICH["data"]["content"]["agent"])
    assert sum(1 for bit in dropped if bit.startswith("开场白")) == 1
    assert "开场建议×1" in dropped
