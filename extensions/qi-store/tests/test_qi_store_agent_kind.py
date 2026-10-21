"""`agent` kind:hikqin 的**离线包** → qi 的角色目录。

源是 `GET /api/agents/{id}/download`(Agent Plugins 1.0.0 的 zip),不是模板蓝图 ——
"远端有 6 个智能体"说的是 `/api/agents`;模板(`/api/agent-templates`)是另一类东西
(管理员维护的蓝图,一条两条)。

每条转换都单独钉:

* frontmatter 写什么、什么时候**不**写 `model:` / `tools:`;
* 私有 `mcp.json`(用私有清单而不是规范 mcp.json:`tool_names` + stdio 都在私有那份里);
* 技能是**真文件**,按 `package_path` 复制到 `<角色>/skills/<名>/`;
* 子智能体**平铺成同级角色**(qi 没有嵌套),同名要跳过而不是互盖;
* 丢弃的段落(`opening` / `data_sources` / `config`)要在计划里说出来;
* 卸载要把主角色 + 子角色一起清掉;
* `api_key` 是 AES 密文 —— 一个字节都不该出现在本地。
"""

from __future__ import annotations

import json

from qi_store import cli, config, paths
from qi_store.errors import EXIT_OK, EXIT_USAGE
from qi_store.ledger import Ledger

from qstore_testkit import AGENT_PACKAGE, agent_package, factory, jwt, upstream


def seed(*, models: bool = True) -> None:
    config.save_auth("hikqin", {"login": "admin", "role": "admin", "access": jwt()},
                     base_url="http://h:6091")
    config.save_default("hikqin")
    if models:
        # 与合成包里的 model 对齐:provider=stub / name=m1
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


# ── 目录:Agent 就是 `/api/agents` ───────────────────────


def test_catalog_lists_every_visible_agent(store_home, capsys):
    """**商店里的 Agent = `GET /api/agents`**,草稿与已发布都列(可见性由账号决定)。"""
    seed()
    capsys.readouterr()
    assert cli.run(["search", "-k", "agent"], http_factory=factory(upstream())) == EXIT_OK
    out = capsys.readouterr().out
    assert "PPT 工厂" in out and "我的助手" in out


def test_publish_status_is_a_badge_not_a_filter(store_home):
    from qi_store.kinds import agent as agent_kind

    items = agent_kind.fetch(_client())
    badges = {item.name: item.badges["publish"] for item in items}
    assert badges == {"PPT 工厂": "published", "我的助手": "draft"}


def _client():
    from qi_store.client import StoreClient
    import httpx

    from qi_store.config import Source

    source = Source(name="hikqin", base_url="http://h:6091", auth={"access": jwt()})
    return StoreClient(source, http=httpx.Client(transport=upstream()))


# ── 转换 ───────────────────────────────────────────────


def test_install_writes_a_self_contained_role(store_home):
    seed()
    assert cli.run(["install", "agent:agent-factory", "-y"],
                   http_factory=factory(upstream())) == EXIT_OK

    role = agents_root() / "PPT 工厂"
    fields = frontmatter(role)
    assert fields["name"] == "PPT 工厂"
    assert fields["description"] == "拆大纲再排版"
    assert fields["model"] == "stub/m1"          # 本地 models.json 里有 → 写上
    assert body(role) == "你是 PPT 工厂的调度员。先把需求拆成大纲,再交给子智能体排版。"


def test_mcp_uses_the_private_manifest(store_home):
    seed()
    cli.run(["install", "agent:agent-factory", "-y"], http_factory=factory(upstream()))

    role = agents_root() / "PPT 工厂"
    entry = json.loads((role / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["ppt-tools"]
    assert entry["type"] == "streamable-http"
    assert entry["url"] == "https://mcp.example.com/mcp"
    assert entry["headers"] == {"X-Tenant": "demo"}
    assert entry["auth"] == "bearer"                     # auth_config 没给值 → 引用
    assert entry["bearerTokenEnv"] == "PPT_TOOLS_TOKEN"
    assert entry["includeTools"] == ["render", "export"]  # 私有清单里的 tool_names

    # **有 MCP 就必须写 tools**(qi-agents:不写 = 一行 MCP 工作都不做);
    # 写了就是"点名的才有",所以内置工具也要在。
    tools = frontmatter(role)["tools"]
    assert "mcp__ppt-tools__*" in tools
    assert "read" in tools and "bash" in tools
    assert "subagent" in tools                            # 有子智能体

    record = Ledger.load().find("agent:agent-factory")
    assert record is not None and record.needs_config == ["PPT_TOOLS_TOKEN"]


def test_skills_are_copied_from_the_package(store_home):
    seed()
    cli.run(["install", "agent:agent-factory", "-y"], http_factory=factory(upstream()))

    skill = agents_root() / "PPT 工厂" / "skills" / "ppt-style"
    assert "description: PPT 配色与版式规范" in (skill / "SKILL.md").read_text(encoding="utf-8")
    assert (skill / "scripts" / "check.py").read_text(encoding="utf-8") == 'print("ok")\n'


def test_sub_agents_become_siblings_with_their_own_skills(store_home):
    seed()
    cli.run(["install", "agent:agent-factory", "-y"], http_factory=factory(upstream()))

    outline = agents_root() / "outline-writer"
    assert frontmatter(outline)["description"] == "只写大纲"
    assert body(outline).startswith("你只负责把需求写成大纲")
    # 子角色没有 MCP → 不写 tools(继承父),也没有 mcp.json
    assert "tools" not in frontmatter(outline)
    assert not (outline / "mcp.json").exists()
    # 包里的技能都摊在同一个 skills/ 根下,靠 package_path 区分 —— 子角色只拿自己那份
    assert (outline / "skills" / "outline-style" / "SKILL.md").is_file()
    assert not (outline / "skills" / "ppt-style").exists()


def test_dropped_sections_are_stated_in_the_plan(store_home, capsys):
    seed()
    capsys.readouterr()
    cli.run(["install", "agent:agent-factory", "--dry-run"],
            http_factory=factory(upstream()))
    out = capsys.readouterr().out
    assert "已丢弃" in out
    assert "数据源" in out and "开场白" in out


def test_model_is_skipped_when_local_models_lack_it(store_home, capsys):
    seed(models=False)
    capsys.readouterr()
    assert cli.run(["install", "agent:agent-factory", "--json", "--dry-run"],
                   http_factory=factory(upstream())) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert "models.json" in payload["plan"][0]["note"]
    assert cli.run(["install", "agent:agent-factory", "-y"],
                   http_factory=factory(upstream())) == EXIT_OK
    assert "model" not in frontmatter(agents_root() / "PPT 工厂")


def test_encrypted_api_key_never_lands_on_disk(store_home):
    """包里的 `api_key` 是 AES 密文 —— 我们只取 provider/name,一个字节都不该落盘。"""
    seed()
    cli.run(["install", "agent:agent-factory", "-y"], http_factory=factory(upstream()))
    role = agents_root() / "PPT 工厂"
    for path in role.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert "eyJpdiI6ICJmYWtlIn0" not in text
            assert "api_key" not in text


def test_role_without_mcp_omits_tools(store_home):
    """没有 MCP 时**不写** `tools:`(= 继承父)。写了就是收窄,没必要就别动。"""
    seed()
    cli.run(["install", "agent:agent-factory", "-y"],
            http_factory=factory(upstream(package=agent_package(mcp=False))))
    assert "tools" not in frontmatter(agents_root() / "PPT 工厂")


def test_skill_without_description_is_skipped(store_home, capsys):
    """qi 的硬规则:没有 description 的技能等于不存在 —— 装了也白装,所以跳过。"""
    seed()
    package = agent_package(skills=False)      # 清单里没有技能 → 文件不会被引用
    capsys.readouterr()
    cli.run(["install", "agent:agent-factory", "-y"],
            http_factory=factory(upstream(package=package)))
    assert not (agents_root() / "PPT 工厂" / "skills").exists()


def test_sub_agent_with_the_same_name_is_skipped(store_home, capsys):
    """同名互盖的症状是"装了 A、跑成 B"—— 宁可少装一个并说出来。"""
    seed()
    package = agent_package(names={"agent": "outline-writer"}, sub_name="outline-writer")
    capsys.readouterr()
    cli.run(["install", "agent:agent-factory", "-y"],
            http_factory=factory(upstream(package=package)))
    assert "同名" in capsys.readouterr().out


def test_dropped_labels_are_not_repeated():
    """`opening_enabled` 与 `opening_message` 是同一件事,只该出现一次"开场白"。"""
    from qi_store.kinds import agent as agent_kind

    manifest = AGENT_PACKAGE["plugin.json"]["extensions"]["com.hikqin.agent"]
    dropped = agent_kind._dropped(manifest)
    assert sum(1 for bit in dropped if bit.startswith("开场白")) == 1


# ── 账本 / 卸载 ────────────────────────────────────────


def test_uninstall_removes_main_and_sub_roles(store_home):
    seed()
    cli.run(["install", "agent:agent-factory", "-y"], http_factory=factory(upstream()))
    assert (agents_root() / "PPT 工厂").is_dir()
    assert (agents_root() / "outline-writer").is_dir()

    assert cli.run(["uninstall", "agent:agent-factory", "-y"],
                   http_factory=factory(upstream())) == EXIT_OK
    assert not (agents_root() / "PPT 工厂").exists()
    assert not (agents_root() / "outline-writer").exists()
    assert not agents_root().exists()          # 空目录也清掉
    assert Ledger.load().find("agent:agent-factory") is None


def test_existing_role_dir_is_a_conflict(store_home):
    """目标已存在且不是本账本装的 → 冲突(退出码 5),不静默覆盖。"""
    seed()
    role = agents_root() / "PPT 工厂"
    role.mkdir(parents=True)
    (role / "agent.md").write_text("---\nname: x\ndescription: 我自己的\n---\n我的\n",
                                   encoding="utf-8")
    assert cli.run(["install", "agent:agent-factory", "-y"],
                   http_factory=factory(upstream())) == 5
    assert "我自己的" in (role / "agent.md").read_text(encoding="utf-8")


def test_download_is_refused_for_agents(store_home, capsys, tmp_path):
    """agent 是多文件目录,`download`(单件搬运)对它没意义 —— 明确拒绝。"""
    seed()
    assert cli.run(["download", "agent:agent-factory", "-o", str(tmp_path / "out")],
                   http_factory=factory(upstream())) == EXIT_USAGE
    assert "download" in capsys.readouterr().err


# ── 格式防护 ───────────────────────────────────────────


def test_unknown_package_format_is_refused(store_home, capsys):
    seed()
    package = agent_package()
    # 把 format_version 改成 3(上游改了格式的信号)
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package)) as src, zipfile.ZipFile(buffer, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if name.endswith("plugin.json"):
                doc = json.loads(data)
                doc["extensions"]["com.hikqin.agent"]["format_version"] = 3
                data = json.dumps(doc, ensure_ascii=False).encode()
            dst.writestr(name, data)

    assert cli.run(["install", "agent:agent-factory", "-y"],
                   http_factory=factory(upstream(package=buffer.getvalue()))) == EXIT_USAGE
    assert "format_version" in capsys.readouterr().err


def test_package_without_the_namespace_is_refused(store_home, capsys):
    seed()
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("agent-x/plugin.json", json.dumps({"name": "x", "extensions": {}}))
    assert cli.run(["install", "agent:agent-factory", "-y"],
                   http_factory=factory(upstream(package=buffer.getvalue()))) == EXIT_USAGE
    assert "com.hikqin.agent" in capsys.readouterr().err


def test_empty_system_prompt_is_refused(store_home, capsys):
    seed()
    package = agent_package(system_prompt="   ")
    assert cli.run(["install", "agent:agent-factory", "-y"],
                   http_factory=factory(upstream(package=package))) == EXIT_USAGE
    assert "system_prompt" in capsys.readouterr().err


def test_literal_backslash_n_in_prompt_is_unescaped(store_home, capsys):
    """上游录 prompt 时多转义了一层 → 导出的是一串**字面**反斜杠-n。"""
    seed()
    capsys.readouterr()
    # 注意:这里是**两个字符**(反斜杠 + n),不是换行 —— 上游导出的就是这种
    package = agent_package(system_prompt="第一段\\n\\n第二段")
    # 表格里的 note 会按终端宽度换行,断言用 --json
    assert cli.run(["install", "agent:agent-factory", "--json", "--dry-run"],
                   http_factory=factory(upstream(package=package))) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert "字面" in payload["plan"][0]["note"]

    assert cli.run(["install", "agent:agent-factory", "-y"],
                   http_factory=factory(upstream(package=package))) == EXIT_OK
    assert body(agents_root() / "PPT 工厂") == "第一段\n\n第二段"


def test_prompt_with_real_newlines_is_left_alone(store_home):
    """真有换行、又夹着字面写法的(比如在讲转义)不动 —— 只修"全是字面"的那种。"""
    seed()
    package = agent_package(system_prompt="第一段\n讲一下 \\n 这个写法")
    cli.run(["install", "agent:agent-factory", "-y"],
            http_factory=factory(upstream(package=package)))
    assert "\\n" in body(agents_root() / "PPT 工厂")
