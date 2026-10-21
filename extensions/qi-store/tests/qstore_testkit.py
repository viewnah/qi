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


def upstream(*, mcp_servers: list[dict] | None = None,
             skill_zip: bytes = SKILL_ZIP,
             skills: dict | None = None) -> httpx.MockTransport:
    """假上游:形状照 `tests/fixtures/` 的真实响应。"""
    servers = MCP_SERVER if mcp_servers is None else mcp_servers

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
        if path == "/a2a/agents":
            return httpx.Response(200, json=A2A)
        return httpx.Response(404, json={"code": 404, "message": f"no {path}"})

    return httpx.MockTransport(handler)


def factory(transport: httpx.MockTransport):
    """把 MockTransport 塞进 CLI 的 `http_factory` 钩子(生产路径一行不改)。"""
    def make(source, **kwargs):
        return StoreClient(source, http=httpx.Client(transport=transport), **kwargs)
    return make
