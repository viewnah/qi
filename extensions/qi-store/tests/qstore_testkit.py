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
#: 真实离线包的清单(抓的部署实例;`api_key` 已换成占位 —— 那是 AES 密文,别当密钥)
AGENT_PACKAGE = json.loads((FIXTURES / "agent_package.json").read_text(encoding="utf-8"))

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


def agent_package(*, mcp: bool = True, skills: bool = True,
                  sub_agents: bool = True, with_files: bool = True,
                  description: str = "拆大纲再排版",
                  system_prompt: str = "你是 PPT 工厂的调度员。先把需求拆成大纲,再交给子智能体排版。",
                  names: dict | None = None,
                  sub_name: str = "outline-writer") -> bytes:
    """**合成**一份 hikqin 离线包(zip)。

    形状照上游 `app/services/agent/package.py`:`plugin.json` 在插件根,
    hikqin 的私有配置在 `extensions["com.hikqin.agent"]`(format_version 2),
    技能是真文件、按 `package_path` 定位。合成而不是塞一个二进制 fixture:
    二进制 blob 在 diff 里读不出"包长什么样"。
    """
    names = names or {}
    agent = {
        "format_version": 2, "exported_at": "2026-10-21T08:00:00Z",
        "id": "agent-factory", "name": names.get("agent", "PPT 工厂"),
        "description": description, "icon": None, "agent_type": "react",
        "system_prompt": system_prompt,
        "config": {"max_iterations": 10, "timeout": 300},
        "opening_enabled": True, "opening_message": "说说你想要什么 PPT",
        "opening_suggestions": ["帮我做季度汇报"],
        "interrupt_on": {}, "status": "ready", "publish_status": "published",
        "model": {"id": 3, "name": "m1", "provider": "stub", "model_type": "chat",
                  "api_endpoint": "https://api.example.com/v1",
                  # 上游导出的是 AES 密文 —— 不是给我们用的密钥,qi 也解不开
                  "api_key": "eyJpdiI6ICJmYWtlIn0="},
        "mcp_services": ([{
            "id": 3, "name": "ppt-tools", "description": "PPT 渲染", "icon": None,
            "transport": "streamable-http", "url": "https://mcp.example.com/mcp",
            "auth_type": "bearer", "auth_config": None,
            "headers": {"X-Tenant": "demo"}, "enabled": True,
            "tool_names": ["render", "export"], "tools": [],
        }] if mcp else []),
        "skills": ([{"skill_id": 9, "name": "ppt-style", "description": "PPT 配色与版式规范",
                     "status": "parsed", "skill_metadata": {},
                     "package_path": "skills/ppt-style"}] if skills else []),
        "data_sources": [{"id": 1, "name": "sales-db", "db_type": "mysql",
                          "password": "<cipher>", "requires_config": True}],
        "knowledge_bases": [],
        "sub_agents": ([{
            "id": "agent-outline-child", "name": sub_name, "description": "只写大纲",
            "agent_type": "react",
            "system_prompt": "你只负责把需求写成大纲,不排版。",
            "config": {}, "opening_enabled": False, "opening_message": "",
            "opening_suggestions": [], "interrupt_on": {}, "status": "ready",
            "publish_status": "draft", "model": None, "mcp_services": [],
            "skills": ([{"skill_id": 10, "name": "outline-style",
                         "description": "大纲模板", "status": "parsed",
                         "skill_metadata": {},
                         "package_path": "skills/outline-style"}] if skills else []),
            "data_sources": [], "knowledge_bases": [], "sub_agents": [],
        }] if sub_agents else []),
    }
    plugin = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": "ppt-factory", "description": "hikqin 智能体: PPT 工厂",
        "version": "1.0.0", "keywords": ["hikqin", "agent"],
        "extensions": {"com.hikqin.agent": agent},
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        root = "agent-ppt-factory/"
        archive.writestr(root + "plugin.json", json.dumps(plugin, ensure_ascii=False))
        if mcp:
            archive.writestr(root + "mcp.json", json.dumps({"mcpServers": {
                "ppt-tools": {"type": "streamable-http",
                              "url": "https://mcp.example.com/mcp"}}}))
        if with_files and skills:
            archive.writestr(
                root + "skills/ppt-style/SKILL.md",
                "---\nname: ppt-style\ndescription: PPT 配色与版式规范\n---\n\n按规范排版。\n")
            archive.writestr(root + "skills/ppt-style/scripts/check.py", 'print("ok")\n')
            archive.writestr(
                root + "skills/outline-style/SKILL.md",
                "---\nname: outline-style\ndescription: 大纲模板\n---\n\n用这个模板。\n")
    return buffer.getvalue()


def upstream(*, mcp_servers: list[dict] | None = None,
             skill_zip: bytes = SKILL_ZIP,
             skills: dict | None = None,
             agents: list[dict] | None = None,
             package: bytes | None = None) -> httpx.MockTransport:
    """假上游:形状照 `tests/fixtures/` 的真实响应。"""
    servers = MCP_SERVER if mcp_servers is None else mcp_servers
    agent_rows = AGENT_LIST if agents is None else agents
    package_bytes = agent_package() if package is None else package

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/skills":
            return httpx.Response(200, json=skills if skills is not None else SKILLS)
        if path == "/api/skills/4/download":
            return httpx.Response(200, content=skill_zip,
                                  headers={"Content-Type": "application/zip"})
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
        if path == "/api/agents":
            return httpx.Response(200, json={"total": len(agent_rows), "page": 1,
                                             "page_size": 50, "items": agent_rows})
        if path.startswith("/api/agents/") and path.endswith("/download"):
            return httpx.Response(200, content=package_bytes,
                                  headers={"Content-Type": "application/zip"})
        if path == "/a2a/agents":
            return httpx.Response(200, json=A2A)
        return httpx.Response(404, json={"code": 404, "message": f"no {path}"})

    return httpx.MockTransport(handler)


def factory(transport: httpx.MockTransport):
    """把 MockTransport 塞进 CLI 的 `http_factory` 钩子(生产路径一行不改)。"""
    def make(source, **kwargs):
        return StoreClient(source, http=httpx.Client(transport=transport), **kwargs)
    return make
