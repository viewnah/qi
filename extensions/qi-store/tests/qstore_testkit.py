"""qi-store 测试的**共用夹具工厂**(不是测试,不会被收集)。

为什么不放 `conftest.py` 里再 `from conftest import …`:两个 `tests/` 目录下各有一份
`conftest.py`,一起跑时先导入的那份会占住 `conftest` 这个名字(仓库里测试目录没有
`__init__.py`),于是另一个目录的 `from conftest import FIXTURES` 会拿到**别人的**
conftest —— 单独跑绿、一起跑红。所以共用东西放一个**唯一名字**的模块里。
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

import httpx

from qi_store.client import StoreClient

FIXTURES = Path(__file__).resolve().parent / "fixtures"

SKILL_MD = "---\nname: ppt-generator\ndescription: 做 PPT\n---\n\n正文\n"


def jwt(exp: int | None = None) -> str:
    """假令牌:只要 payload 里有 `exp`,续期判定与 `whoami` 就够用了(不验签)。"""
    import base64

    payload = {"username": "admin", "role": "admin", "exp": exp or int(time.time()) + 3600}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{raw}.sig"


def make_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


SKILL_ZIP = make_zip({"ppt-generator/SKILL.md": SKILL_MD,
                      "ppt-generator/scripts/gen.py": "print(1)\n"})

SKILLS = json.loads((FIXTURES / "skills.json").read_text(encoding="utf-8"))
A2A = json.loads((FIXTURES / "a2a_agents.json").read_text(encoding="utf-8"))
MCP_SERVER = json.loads((FIXTURES / "mcp_server_synthetic.json").read_text(encoding="utf-8"))
#: 真实响应:列表页与详情(抓的部署实例) —— 形状的事实来源
AGENTS_LIST_REAL = json.loads((FIXTURES / "agents_list.json").read_text(encoding="utf-8"))
AGENT_DETAIL_REAL = json.loads((FIXTURES / "agent_detail.json").read_text(encoding="utf-8"))

#: 目录页 `/api/agents` 的合成样本(真实实例上是你自己的 6 个智能体)。
#: 一条已发布、一条草稿 —— 商店**两个都列**(可见性由账号决定,发布状态只是运营信息)。
AGENT_LIST = [
    {"id": "agent-factory", "name": "PPT 工厂", "description": "拆大纲再排版",
     "model_name": "m1", "mcp_services_count": 1, "skills_count": 1,
     "subagents_count": 1, "agent_type": "react", "status": "ready",
     "publish_status": "published", "created_at": "2026-10-19T01:00:00"},
    {"id": "agent-draft", "name": "我的助手", "description": "草稿态",
     "model_name": None, "mcp_services_count": 0, "skills_count": 0,
     "subagents_count": 0, "agent_type": "react", "status": "ready",
     "publish_status": "draft", "created_at": "2026-10-20T01:00:00"},
]


AGENT_FACTORY_ID = "agent-factory"
AGENT_CHILD_ID = "agent-outline-child"


def agent_detail(*, mcp: bool = True, skills: bool = True, sub_agents: bool = True,
                 description: str = "拆大纲再排版",
                 system_prompt: str = "你是 PPT 工厂的调度员。先把需求拆成大纲,再交给子智能体排版。",
                 name: str = "PPT 工厂", model: dict | None = None,
                 tool_names: list[str] | None = None) -> dict:
    """一份**合成**的 `GET /api/agents/{id}` 详情(真实实例上没有带 MCP/子智能体的样本)。

    形状照上游 `AgentResponse`:MCP 只给 `mcp_service_id` + `tool_names`(`config` 恒为
    `{}`),技能只给 `skill_id`,子智能体只给 `child_agent_id` —— 细节都要再取一次。
    """
    return {
        "id": AGENT_FACTORY_ID, "name": name, "description": description,
        "icon": None, "agent_type": "react", "system_prompt": system_prompt,
        "config": {"max_iterations": 10, "timeout": 300},
        "opening_enabled": True, "opening_message": "说说你想要什么 PPT",
        "opening_suggestions": ["帮我做季度汇报"],
        "interrupt_on": {}, "status": "ready", "publish_status": "published",
        "model": model if model is not None else {
            "model_id": 3, "model_name": "m1", "provider": "stub"},
        "mcp_services": ([{
            "mcp_service_id": 3, "tool_names": tool_names,
            "mcp_service_name": "ppt-tools", "description": "PPT 渲染",
            "url": "https://mcp.example.com/mcp", "connection_status": "available",
            "server_enabled": True, "enabled": True, "tools_count": 2,
            "tools": [], "config": {},
        }] if mcp else []),
        "skill_bindings": ([{"skill_id": 9, "name": "ppt-style",
                             "description": "PPT 配色与版式规范"}] if skills else []),
        "data_sources": [{"data_source_id": 1, "name": "sales-db", "db_type": "mysql"}],
        "knowledge_bases": [],
        "sub_agents": ([{"id": "link-1", "child_agent_id": AGENT_CHILD_ID,
                         "name": "outline-writer", "description": "只写大纲",
                         "status": "ready"}] if sub_agents else []),
        "subagents": [],
        "created_at": "2026-10-19T01:00:00", "updated_at": "2026-10-20T01:00:00",
    }


def agent_child_detail(**overrides) -> dict:
    detail = {
        "id": AGENT_CHILD_ID, "name": "outline-writer", "description": "只写大纲",
        "icon": None, "agent_type": "react",
        "system_prompt": "你只负责把需求写成大纲,不排版。",
        "config": {}, "opening_enabled": False, "opening_message": "",
        "opening_suggestions": [], "interrupt_on": {}, "status": "ready",
        "publish_status": "draft", "model": None, "mcp_services": [],
        "skill_bindings": [{"skill_id": 10, "name": "outline-style",
                            "description": "大纲模板"}],
        "data_sources": [], "knowledge_bases": [], "sub_agents": [], "subagents": [],
        "created_at": "2026-10-19T01:00:00", "updated_at": "2026-10-19T01:00:00",
    }
    detail.update(overrides)
    return detail


def skill_zip_for(name: str, description: str) -> bytes:
    """一个技能 zip(顶层目录 = 技能名,内含 SKILL.md)—— 与上游 `create_skill_zip` 同形。"""
    return make_zip({
        f"{name}/SKILL.md": f"---\nname: {name}\ndescription: {description}\n---\n\n正文\n",
        f"{name}/scripts/check.py": 'print("ok")\n',
    })


#: 默认技能 zip:技能 9 与 10(agent 详情里引用的那两个)
SKILL_ZIPS = {
    9: skill_zip_for("ppt-style", "PPT 配色与版式规范"),
    10: skill_zip_for("outline-style", "大纲模板"),
}


def upstream(*, mcp_servers: list[dict] | dict | None = None,
             skill_zip: bytes = SKILL_ZIP,
             skills: dict | None = None,
             agents: list[dict] | None = None,
             details: dict | None = None,
             skill_zips: dict | None = None) -> httpx.MockTransport:
    """假上游:形状照 `tests/fixtures/` 的真实响应。"""
    servers = MCP_SERVER if mcp_servers is None else mcp_servers
    agent_rows = AGENT_LIST if agents is None else agents
    detail_map = {AGENT_FACTORY_ID: agent_detail(), AGENT_CHILD_ID: agent_child_detail()}
    if details is not None:
        detail_map = details
    zips = SKILL_ZIPS if skill_zips is None else skill_zips

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/skills":
            return httpx.Response(200, json=skills if skills is not None else SKILLS)
        if path == "/api/skills/4/download":
            return httpx.Response(200, content=skill_zip,
                                  headers={"Content-Type": "application/zip"})
        if path.startswith("/api/skills/") and path.endswith("/download"):
            skill_id = int(path.split("/")[3])
            if skill_id in zips:
                return httpx.Response(200, content=zips[skill_id],
                                      headers={"Content-Type": "application/zip"})
            return httpx.Response(404, json={"code": 404, "message": path})
        if path == "/api/skills/4/files":
            return httpx.Response(200, json={"skill_id": 4, "skill_name": "ppt-generator",
                                             "files": [{"name": "SKILL.md",
                                                        "is_directory": False}]})
        if path == "/api/mcp/servers":
            items = []
            for server in ([servers] if isinstance(servers, dict) else servers):
                items.append({k: v for k, v in server.items() if k != "headers"}
                             | ({"headers": server["headers"]} if server.get("headers") else {}))
            return httpx.Response(200, json={"total": len(items), "page": 1,
                                             "page_size": 100, "items": items})
        if path.startswith("/api/mcp/servers/"):
            server_id = path.rsplit("/", 1)[-1]
            known = {str(server.get("id")) for server in
                     ([servers] if isinstance(servers, dict) else servers)}
            if server_id in known:
                return httpx.Response(200, json=MCP_SERVER)
            return httpx.Response(404, json={"code": 404, "message": path})
        if path == "/api/agents":
            return httpx.Response(200, json={"total": len(agent_rows), "page": 1,
                                             "page_size": 50, "items": agent_rows})
        if path.startswith("/api/agents/"):
            agent_id = path.rsplit("/", 1)[-1]
            if agent_id in detail_map:
                return httpx.Response(200, json=detail_map[agent_id])
            return httpx.Response(404, json={"code": 404, "message": path})
        if path == "/a2a/agents":
            return httpx.Response(200, json=A2A)
        return httpx.Response(404, json={"code": 404, "message": f"no {path}"})

    return httpx.MockTransport(handler)


def factory(transport: httpx.MockTransport):
    """把 MockTransport 塞进 CLI 的 `http_factory` 钩子(生产路径一行不改)。"""
    def make(source, **kwargs):
        return StoreClient(source, http=httpx.Client(transport=transport), **kwargs)
    return make
