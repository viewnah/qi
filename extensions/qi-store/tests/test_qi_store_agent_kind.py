"""`agent` kind:hikqin 的**智能体详情** → qi 的角色目录。

源是 `GET /api/agents`(目录)+ `GET /api/agents/{id}`(详情),MCP/技能的细节**自己再取**
(`/api/mcp/servers/{id}`、`/api/skills/{id}/download`)—— 上游没有"直接给我一个 qi 能用的包"
这种接口,离线包与模板蓝图都是别的格式。

每条转换都单独钉:

* 目录 = `/api/agents`,草稿与已发布都列(`publish_status` 只是徽标);
* frontmatter 写什么、什么时候**不**写 `model:` / `tools:`;
* `tool_names` 为 `None` 与为列表**含义不同**(全部工具 vs 白名单 → `includeTools`);
* 技能从 zip 取,落到 `<角色>/skills/<名>/`;
* 子智能体要**各自再取详情**(详情里只有 id),平铺成同级角色;同名跳过、成环跳过;
* 取不到某个 server / 技能时**降级**(记一笔,角色照样装),而不是整个失败;
* 丢弃的段落要在计划里说出来;卸载要把主角色 + 子角色一起清掉。
"""

from __future__ import annotations

import json

from qi_store import cli, config, paths
from qi_store.errors import EXIT_OK, EXIT_USAGE
from qi_store.ledger import Ledger

from qstore_testkit import (
    AGENT_CHILD_ID,
    AGENT_DETAIL_REAL,
    AGENT_FACTORY_ID,
    agent_child_detail,
    agent_detail,
    factory,
    jwt,
    skill_zip_for,
    upstream,
)


def seed(*, models: bool = True) -> None:
    config.save_auth("hikqin", {"login": "admin", "role": "admin", "access": jwt()},
                     base_url="http://h:6091")
    config.save_default("hikqin")
    if models:
        # 与合成详情里的 model 对齐:provider=stub / model_name=m1
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


def install(*, package=None, **upstream_kwargs) -> int:
    """装 `agent:agent-factory`(或 `package` 指定的那一条)。"""
    target = package or f"agent:{AGENT_FACTORY_ID}"
    return cli.run(["install", target, "-y"],
                   http_factory=factory(upstream(**upstream_kwargs)))


# ── 目录:Agent 就是 `/api/agents` ───────────────────────


def test_catalog_lists_every_visible_agent(store_home, capsys):
    """**商店里的 Agent = `GET /api/agents`**,草稿与已发布都列(可见性由账号决定)。"""
    seed()
    capsys.readouterr()
    assert cli.run(["search", "-k", "agent"], http_factory=factory(upstream())) == EXIT_OK
    out = capsys.readouterr().out
    assert "PPT 工厂" in out and "我的助手" in out


def test_publish_status_is_a_badge_not_a_filter():
    from qi_store.kinds import agent as agent_kind

    import httpx

    from qi_store.client import StoreClient
    from qi_store.config import Source

    source = Source(name="hikqin", base_url="http://h:6091", auth={"access": jwt()})
    client = StoreClient(source, http=httpx.Client(transport=upstream()))
    badges = {item.name: item.badges["publish"] for item in agent_kind.fetch(client)}
    assert badges == {"PPT 工厂": "published", "我的助手": "draft"}


# ── 转换 ───────────────────────────────────────────────


def test_install_writes_a_self_contained_role(store_home):
    seed()
    assert install() == EXIT_OK

    role = agents_root() / "PPT 工厂"
    fields = frontmatter(role)
    # 只写 name + description（与 hikqin 导出的包同一口径）：
    # 不写 model（跟随会话/父角色）、不写 tools（继承父 + 私有 MCP 自动接上）
    assert set(fields) == {"name", "description"}
    assert fields["name"] == "PPT 工厂"
    assert fields["description"] == "拆大纲再排版"
    assert body(role) == "你是 PPT 工厂的调度员。先把需求拆成大纲,再交给子智能体排版。"


def test_mcp_details_are_fetched_per_server(store_home):
    """详情里只有 `mcp_service_id` + `tool_names`,细节回 `/api/mcp/servers/{id}` 取。"""
    seed()
    install()
    role = agents_root() / "PPT 工厂"
    entry = json.loads((role / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["ppt-tools"]
    assert entry["type"] == "streamable-http"
    assert entry["url"] == "https://mcp.example.com/mcp"
    assert entry["headers"] == {"X-Tenant": "demo"}
    assert entry["auth"] == "bearer"
    assert entry["bearerTokenEnv"] == "PPT_TOOLS_TOKEN"
    # 合成详情里 tool_names=None = 全部工具 → **不写** includeTools
    assert "includeTools" not in entry

    # 不写 tools：qi-agents 会在 tools 省略时自动接上角色私有 server
    assert "tools" not in frontmatter(role)
    record = Ledger.load().find(f"agent:{AGENT_FACTORY_ID}")
    assert record is not None and record.needs_config == ["PPT_TOOLS_TOKEN"]


def test_tool_names_whitelist_becomes_include_tools(store_home):
    """hikqin 的 `tool_names`:None = 全部,列表 = 白名单(→ qi-mcp 的 includeTools)。"""
    seed()
    details = {AGENT_FACTORY_ID: agent_detail(tool_names=["render", "export"]),
               AGENT_CHILD_ID: agent_child_detail()}
    install(details=details)
    entry = json.loads((agents_root() / "PPT 工厂" / "mcp.json").read_text(encoding="utf-8"))
    assert entry["mcpServers"]["ppt-tools"]["includeTools"] == ["render", "export"]


def test_skills_come_from_the_skill_zip(store_home):
    seed()
    install()
    skill = agents_root() / "PPT 工厂" / "skills" / "ppt-style"
    assert "description: PPT 配色与版式规范" in (skill / "SKILL.md").read_text(encoding="utf-8")
    assert (skill / "scripts" / "check.py").read_text(encoding="utf-8") == 'print("ok")\n'


def test_sub_agents_are_fetched_and_become_siblings(store_home):
    """详情里的 `sub_agents[]` 只有 id/名字 → 各自再取一次详情,平铺成同级角色。"""
    seed()
    install()
    outline = agents_root() / "outline-writer"
    assert frontmatter(outline)["description"] == "只写大纲"
    assert body(outline).startswith("你只负责把需求写成大纲")
    assert "tools" not in frontmatter(outline)          # 没有 MCP → 继承父
    assert (outline / "skills" / "outline-style" / "SKILL.md").is_file()


def test_dropped_sections_are_stated_in_the_plan(store_home, capsys):
    seed()
    capsys.readouterr()
    cli.run(["install", f"agent:{AGENT_FACTORY_ID}", "--dry-run"],
            http_factory=factory(upstream()))
    out = capsys.readouterr().out
    assert "已丢弃" in out and "数据源" in out and "开场白" in out


def test_model_is_never_written(store_home, capsys):
    """**不写 model**：角色跟随会话/父角色的模型（上游的 api_key 是密文，qi 也用不了）。"""
    seed()
    assert install() == EXIT_OK
    assert "model" not in frontmatter(agents_root() / "PPT 工厂")


def test_role_without_mcp_omits_tools(store_home):
    """没有 MCP 时**不写** `tools:`(= 继承父)。写了就是收窄,没必要就别动。"""
    seed()
    details = {AGENT_FACTORY_ID: agent_detail(mcp=False),
               AGENT_CHILD_ID: agent_child_detail()}
    install(details=details)
    assert "tools" not in frontmatter(agents_root() / "PPT 工厂")
    assert not (agents_root() / "PPT 工厂" / "mcp.json").exists()


def test_literal_backslash_n_in_prompt_is_unescaped(store_home, capsys):
    """上游录 prompt 时多转义了一层 → 导出的是一串**字面**反斜杠-n。"""
    seed()
    capsys.readouterr()
    details = {AGENT_FACTORY_ID: agent_detail(system_prompt="第一段\\n\\n第二段"),
               AGENT_CHILD_ID: agent_child_detail()}
    assert cli.run(["install", f"agent:{AGENT_FACTORY_ID}", "--json", "--dry-run"],
                   http_factory=factory(upstream(details=details))) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert "字面" in payload["plan"][0]["note"]

    install(details=details)
    assert body(agents_root() / "PPT 工厂") == "第一段\n\n第二段"


def test_prompt_with_real_newlines_is_left_alone(store_home):
    """真有换行、又夹着字面写法的(比如在讲转义)不动 —— 只修"全是字面"的那种。"""
    seed()
    details = {AGENT_FACTORY_ID: agent_detail(system_prompt="第一段\n讲一下 \\n 这个写法"),
               AGENT_CHILD_ID: agent_child_detail()}
    install(details=details)
    assert "\\n" in body(agents_root() / "PPT 工厂")


def test_missing_mcp_server_details_degrade_instead_of_failing(store_home, capsys):
    """一个 server 取不到不该让整个角色装不了 —— 记一笔,不写那个条目。"""
    seed()
    capsys.readouterr()
    details = {AGENT_FACTORY_ID: agent_detail(), AGENT_CHILD_ID: agent_child_detail()}
    details[AGENT_FACTORY_ID]["mcp_services"][0]["mcp_service_id"] = 999   # 404
    assert cli.run(["install", f"agent:{AGENT_FACTORY_ID}", "-y"],
                   http_factory=factory(upstream(details=details))) == EXIT_OK
    out = capsys.readouterr().out
    assert "跳过 MCP" in out
    role = agents_root() / "PPT 工厂"
    assert (role / "agent.md").is_file()
    assert not (role / "mcp.json").exists()


def test_missing_skill_zip_degrades_instead_of_failing(store_home, capsys):
    seed()
    capsys.readouterr()
    assert cli.run(["install", f"agent:{AGENT_FACTORY_ID}", "-y"],
                   http_factory=factory(upstream(skill_zips={}))) == EXIT_OK
    out = capsys.readouterr().out
    assert "跳过技能" in out
    assert (agents_root() / "PPT 工厂" / "agent.md").is_file()


def test_sub_agent_pointing_back_to_the_parent_is_skipped(store_home, capsys):
    """成环要挡住,不然递归取详情会一直转。"""
    seed()
    child = agent_child_detail(sub_agents=[{"id": "link-2", "child_agent_id": AGENT_FACTORY_ID,
                                            "name": "PPT 工厂"}])
    details = {AGENT_FACTORY_ID: agent_detail(), AGENT_CHILD_ID: child}
    capsys.readouterr()
    assert install(details=details) == EXIT_OK
    assert "成环" in capsys.readouterr().out


def test_sub_agent_with_the_same_name_is_skipped(store_home, capsys):
    """同名互盖的症状是"装了 A、跑成 B"—— 宁可少装一个并说出来。"""
    seed()
    details = {AGENT_FACTORY_ID: agent_detail(name="PPT 工厂"),
               AGENT_CHILD_ID: agent_child_detail(name="PPT 工厂")}
    capsys.readouterr()
    install(details=details)
    assert "同名" in capsys.readouterr().out
    assert frontmatter(agents_root() / "PPT 工厂")["description"] == "拆大纲再排版"


# ── 账本 / 卸载 ────────────────────────────────────────


def test_uninstall_removes_main_and_sub_roles(store_home):
    seed()
    install()
    assert (agents_root() / "PPT 工厂").is_dir()
    assert (agents_root() / "outline-writer").is_dir()

    assert cli.run(["uninstall", f"agent:{AGENT_FACTORY_ID}", "-y"],
                   http_factory=factory(upstream())) == EXIT_OK
    assert not (agents_root() / "PPT 工厂").exists()
    assert not (agents_root() / "outline-writer").exists()
    assert not agents_root().exists()          # 空目录也清掉
    assert Ledger.load().find(f"agent:{AGENT_FACTORY_ID}") is None


def test_existing_role_dir_is_a_conflict(store_home):
    seed()
    role = agents_root() / "PPT 工厂"
    role.mkdir(parents=True)
    (role / "agent.md").write_text("---\nname: x\ndescription: 我自己的\n---\n我的\n",
                                   encoding="utf-8")
    assert install() == 5
    assert "我自己的" in (role / "agent.md").read_text(encoding="utf-8")


def test_download_is_refused_for_agents(store_home, capsys, tmp_path):
    """agent 是多文件目录,`download`(单件搬运)对它没意义 —— 明确拒绝。"""
    seed()
    assert cli.run(["download", f"agent:{AGENT_FACTORY_ID}", "-o", str(tmp_path / "out")],
                   http_factory=factory(upstream())) == EXIT_USAGE
    assert "download" in capsys.readouterr().err


# ── 真实详情的形状 ──────────────────────────────────────


def test_real_agent_detail_shape_is_understood(store_home, capsys):
    """拿抓下来的**真实**详情跑一遍:字段名/嵌套层级变了就会红。"""
    seed()
    agent_id = AGENT_DETAIL_REAL["id"]
    details = {agent_id: AGENT_DETAIL_REAL}
    zips = {3: skill_zip_for("skill-creator", "Guide for creating effective skills.")}
    agents = [{"id": agent_id, "name": AGENT_DETAIL_REAL["name"],
               "description": AGENT_DETAIL_REAL.get("description"), "model_name": None,
               "mcp_services_count": 0, "skills_count": 1, "subagents_count": 0,
               "agent_type": "react", "status": "ready", "publish_status": "draft",
               "created_at": "2026-10-16T10:00:00"}]
    assert cli.run(["install", f"agent:{agent_id}", "-y"],
                   http_factory=factory(upstream(agents=agents, details=details,
                                                 skill_zips=zips))) == EXIT_OK
    role = agents_root() / AGENT_DETAIL_REAL["name"]
    assert "description" in frontmatter(role)
    # 真实 prompt 里是字面反斜杠-n(上游录入时多转义了一层)→ 已还原成换行
    assert "\n" in body(role)
    assert "\\\\n" not in body(role)
    assert (role / "skills" / "skill-creator" / "SKILL.md").is_file()
