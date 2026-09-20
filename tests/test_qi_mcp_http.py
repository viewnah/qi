"""qi-mcp 切片 2c:Streamable HTTP 传输(E21 说的另一半)。

分两层测:

1. **纯单测**(不需要网络):头/bearer token 的组装、`${VAR}` 展开、缺变量在发请求前就报错、
   `url` 缺失的可读错误、传输分派;
2. **端到端**:起一个真的 `MCPServer(transport="streamable-http")` 子进程,经本案的
   `connect_http` 连上去 list/call —— 证明"HTTP 也走通,不只是编译过"。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "extensions" / "qi-mcp"))

import pytest  # noqa: E402

from qi_mcp.client import _headers, _http_opener, connect, connect_http  # noqa: E402
from qi_mcp.config import ServerSpec  # noqa: E402
from qi_mcp.proxy import build_tool  # noqa: E402
from qi_mcp.servers import ManagerError, ServerManager  # noqa: E402

HTTP_SERVER_SOURCE = '''
import sys

from mcp.server.mcpserver import MCPServer

server = MCPServer("qi-http-test")


@server.tool()
def echo(text: str) -> str:
    """原样回显 text。"""
    return f"echo:{text}"


server.run(transport="streamable-http", host="127.0.0.1", port=int(sys.argv[1]))
'''


def _http_spec(**cfg) -> ServerSpec:
    return ServerSpec(name="remote", config={"url": "https://mcp.example.com/mcp", **cfg},
                      scope="global")


# ── 头与凭据(纯单测)──────────────────────────────────

def test_headers_are_expanded_from_the_environment(monkeypatch):
    monkeypatch.setenv("QI_TEST_TOKEN", "s3cret")
    spec = _http_spec(headers={"X-Api-Key": "${QI_TEST_TOKEN}", "X-Plain": "literal"})
    assert _headers(spec) == {"X-Api-Key": "s3cret", "X-Plain": "literal"}


def test_missing_env_var_fails_before_any_request(monkeypatch):
    """缺变量**在发请求之前**就报错 —— 否则现场只看到一个 401,不知道是变量没设。"""
    monkeypatch.delenv("QI_TEST_NOPE", raising=False)
    with pytest.raises(ManagerError, match="QI_TEST_NOPE"):
        _headers(_http_spec(headers={"X-Api-Key": "${QI_TEST_NOPE}"}))


def test_bearer_token_from_literal_and_from_env(monkeypatch):
    assert _headers(_http_spec(auth="bearer", bearerToken="abc"))["Authorization"] == "Bearer abc"

    monkeypatch.setenv("QI_TEST_BEARER", "from-env")
    spec = _http_spec(auth="bearer", bearerTokenEnv="QI_TEST_BEARER")
    assert _headers(spec)["Authorization"] == "Bearer from-env"

    monkeypatch.delenv("QI_TEST_BEARER", raising=False)
    with pytest.raises(ManagerError, match="bearerTokenEnv"):
        _headers(spec)


def test_explicit_authorization_header_wins_over_bearer():
    """`headers` 里手写的 Authorization 优先 —— 用户显式写的东西不该被配置项覆盖。"""
    spec = _http_spec(auth="bearer", bearerToken="abc",
                      headers={"Authorization": "Bearer manual"})
    assert _headers(spec)["Authorization"] == "Bearer manual"


def test_oauth_is_not_silently_ignored():
    """明确不做 OAuth(已记入范围):`auth: "oauth"` 不会假装加了个头。"""
    assert "Authorization" not in _headers(_http_spec(auth="oauth"))


def test_missing_url_is_a_readable_error():
    spec = ServerSpec(name="x", config={}, scope="global")
    with pytest.raises(ManagerError, match="没有 url"):
        _http_opener(spec)


@pytest.mark.asyncio
async def test_socket_transport_says_not_implemented():
    spec = ServerSpec(name="s", config={"socket": "/tmp/mcp.sock"}, scope="global")
    with pytest.raises(ManagerError, match="尚未实现"):
        await connect(spec)


# ── 端到端:真的 HTTP server ─────────────────────────────

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, *, timeout: float = 30.0) -> bool:
    """等 server 起来。**轮询 TCP 而不是 sleep 固定时间** —— 免得机器慢就假失败。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.15)
    return False


@pytest.mark.asyncio
async def test_streamable_http_end_to_end(tmp_path: Path):
    """真 HTTP server → 本案客户端连上 → list → call。**HTTP 也真的通了。**"""
    script = tmp_path / "http_server.py"
    script.write_text(HTTP_SERVER_SOURCE, encoding="utf-8")
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(script), str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        if not _wait_for_port(port):
            err = b""
            if proc.stderr is not None:
                proc.stderr.close()            # 关掉才读得到已缓冲的内容
            pytest.fail(f"HTTP MCP server 没起来(port={port}){err!r}")

        spec = ServerSpec(name="remote",
                          config={"url": f"http://127.0.0.1:{port}/mcp"},
                          scope="global")
        client = await connect_http(spec)
        try:
            tools = await client.list_tools()
            assert "echo" in [name for name, _d, _s in tools]
            assert "echo:http 通" in await client.call_tool("echo", {"text": "http 通"})
            # 第二次调用复用同一个会话(HTTP 侧同样有 task 亲和那条约束)
            assert "echo:再来" in await client.call_tool("echo", {"text": "再来"})
        finally:
            await client.aclose()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if proc.stderr is not None:
            proc.stderr.close()


@pytest.mark.asyncio
async def test_http_connect_failure_is_a_readable_error():
    """连不上的 HTTP server(端口空着)→ **记一条带原因的 note**,不抛裸异常、不挂死。

    走 manager + 代理工具的完整路径 —— 因为这才是模型实际会遇到的面(它看到的是一句
    能读懂的话,而不是一个 traceback)。
    """
    dead = ServerSpec(name="dead",
                      config={"url": f"http://127.0.0.1:{_free_port()}/mcp",
                              "requestTimeoutMs": 800},
                      scope="global")
    manager = ServerManager({"dead": dead}, connect)
    tool = build_tool(manager)
    try:
        result = await tool.execute({"search": "任何"}, None)
        assert isinstance(result, str)
        assert "连接失败的" in result and "dead" in result      # 概览里说清哪个连不上
        assert any("dead" in n and "连接失败" in n for n in manager.notes)
        assert "dead" in manager.failed
    finally:
        await manager.aclose()
