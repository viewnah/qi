"""qi-mcp 切片 2b:真 stdio 客户端,端到端跑一个**真 MCP server**(`mcp` 2.x)。

这里起的是真东西:一个几行的 `MCPServer` 子进程,走真 stdio 协议。所以这些测试同时
盯着三件靠读代码看不出来的事:

1. **会话归一个长驻 task 所有** —— SDK 的传输是 task 亲和的,若由调用方轮流进出
   `async with`,这里会直接炸 `Attempted to exit cancel scope in a different task`。
   所以"连一次、多次调用"这个形状本身就是被测的对象;
2. **v2 的字段拼写**(`is_error` / `input_schema`,1.x 是驼形)—— 写错会静默拿不到值;
3. **连接失败**在真客户端下也走"记 note、其它 server 照常"那条路,而不是抛栈。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "extensions" / "qi-mcp"))

import pytest  # noqa: E402

from qi_mcp.client import connect, connect_stdio  # noqa: E402
from qi_mcp.config import ServerSpec  # noqa: E402
from qi_mcp.proxy import build_tool  # noqa: E402
from qi_mcp.servers import ManagerError, ServerManager  # noqa: E402


def _text(result: object) -> str:
    """代理工具的契约:结果**总是纯文本**(错误也是文本,让模型能自己改正)。

    这里显式断言而不是直接 `in` —— `Tool.execute` 的返回类型是 `str | ToolOutcome`,
    但这个工具的契约是不抛、不回结构化结果;写成断言就是把契约钉下来。
    """
    assert isinstance(result, str), f"代理工具应当返回纯文本,收到 {type(result).__name__}"
    return result

#: 一个真的 MCP server(`mcp` 2.x:`FastMCP` 已更名为 `MCPServer`)。
#: `echo` 带回显、`boom` 故意抛 —— 用来验"工具自己报错"能被标出来。
SERVER_SOURCE = '''
from mcp.server.mcpserver import MCPServer

server = MCPServer("qi-test")


@server.tool()
def echo(text: str) -> str:
    """原样回显 text。"""
    return f"echo:{text}"


@server.tool()
def boom() -> str:
    """总是失败。"""
    raise RuntimeError("故意炸的")


server.run()
'''


@pytest.fixture(scope="module")
def server_script(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("mcp-server") / "server.py"
    path.write_text(SERVER_SOURCE, encoding="utf-8")
    return path


def _spec(script: Path, name: str = "test") -> ServerSpec:
    return ServerSpec(name=name,
                      config={"command": sys.executable, "args": [str(script)]},
                      scope="global")


# ── 客户端本体 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_stdio_client_lists_and_calls(server_script: Path):
    """连一次 → 列表 → 调用 → 再调用。**多次调用复用同一个会话**(task 亲和那条坑就走这里)。"""
    client = await connect_stdio(_spec(server_script))
    try:
        tools = await client.list_tools()
        names = [name for name, _desc, _schema in tools]
        assert "echo" in names

        echo = next(t for t in tools if t[0] == "echo")
        assert echo[1] == "原样回显 text。"                  # v2 的 description 拿到了
        assert "text" in (echo[2].get("properties") or {})   # input_schema 拿到了(不是 None)

        assert "echo:哈喽" in await client.call_tool("echo", {"text": "哈喽"})
        # 第二次调用:若会话是"轮流进出 ctx"的写法,这里就会炸 task 亲和
        assert "echo:再来" in await client.call_tool("echo", {"text": "再来"})
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_tool_error_is_flagged(server_script: Path):
    """工具自己报错 → 标成 `[工具报告错误]`(靠 v2 的 `is_error` 字段)。"""
    client = await connect_stdio(_spec(server_script))
    try:
        text = await client.call_tool("boom", {})
        assert "[工具报告错误]" in text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_client_can_be_closed_then_reused_field_errors(server_script: Path):
    """关掉之后再调用 → 可读错误(而不是 await 一个已死的 task)。"""
    client = await connect_stdio(_spec(server_script))
    await client.aclose()
    with pytest.raises(ManagerError, match="已关闭"):
        await client.call_tool("echo", {"text": "x"})


# ── 经 manager + 代理工具(真实路径)────────────────────

@pytest.mark.asyncio
async def test_proxy_tool_search_then_call_over_stdio(server_script: Path):
    """模型看到的完整路径:`mcp({search})` 找到 → `mcp({tool,args})` 调通。"""
    manager = ServerManager({"test": _spec(server_script)}, connect)
    tool = build_tool(manager)
    try:
        found = _text(await tool.execute({"search": "回显"}, None))
        assert "mcp__test__echo" in found
        assert "text (string)" in found                     # 参数渲染生效(证明 schema 拿到了)

        out = _text(await tool.execute({"tool": "mcp__test__echo", "args": {"text": "hi"}}, None))
        assert "echo:hi" in out
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_broken_command_becomes_a_note_not_a_crash(server_script: Path):
    """起不来的 server(命令不存在)→ 记 note、降级,而不是把整个工具炸掉。"""
    bad = ServerSpec(name="bad", config={"command": "/nonexistent/qi-nope"}, scope="global")
    manager = ServerManager({"bad": bad, "test": _spec(server_script)}, connect)
    tool = build_tool(manager)
    try:
        text = _text(await tool.execute({"search": "回显"}, None))
        assert "mcp__test__echo" in text                     # 好的那个照常可用
        assert "bad" in manager.failed
        assert any("bad" in n and "连接失败" in n for n in manager.notes)
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_http_transport_is_a_readable_not_implemented(server_script: Path):
    """E21 说 stdio + Streamable HTTP,但 HTTP 还没做 —— **说清"没做",不假装**。"""
    http = ServerSpec(name="remote", config={"url": "https://mcp.example.com/mcp"},
                      scope="global")
    manager = ServerManager({"remote": http}, connect)
    tool = build_tool(manager)
    try:
        text = _text(await tool.execute({"search": "任何"}, None))
        assert "尚未实现" in text and "stdio" in text
        assert "remote" in manager.failed
    finally:
        await manager.aclose()
