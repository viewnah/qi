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

import asyncio
import contextlib
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
#: 设了 `QI_TEST_MCP_PIDFILE` 就把自己的 pid 写进去 —— "退出后子进程真的没了吗"
#: 只能从外部看 pid(`SessionClient` 不暴露 process)。
SERVER_SOURCE = '''
import os
import pathlib

_pidfile = os.environ.get("QI_TEST_MCP_PIDFILE")
if _pidfile:
    pathlib.Path(_pidfile).write_text(str(os.getpid()))

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
async def test_unimplemented_transport_is_a_readable_error():
    """E21 定了 stdio + Streamable HTTP;**socket(`rmcp-mux`)不做** —— 要说清“没做”,不假装。

    (HTTP 曾经也在这条里,切片 2c 实现后移到 `test_qi_mcp_http.py` 真连真服务器。)
    """
    sock = ServerSpec(name="sock", config={"socket": "/tmp/mcp.sock"}, scope="global")
    manager = ServerManager({"sock": sock}, connect)
    tool = build_tool(manager)
    try:
        text = _text(await tool.execute({"search": "任何"}, None))
        assert "尚未实现" in text and "socket" in text
        assert "sock" in manager.failed
    finally:
        await manager.aclose()


# ── `/mcp tools`:人在 TUI 里看到的工具清单(真 server)─────

@pytest.mark.asyncio
async def test_mcp_tools_lists_real_server_tools(server_script: Path, tmp_path, monkeypatch):
    """`/mcp tools` 端到端:声明一个**真** stdio server → 面板列出它的工具。

    这条盯的是"面板说的是不是真的有哪些工具" —— 只在读代码层面看,很容易写成
    "列了声明的 server 名" 而不是 "连上去问它有什么工具"。
    """
    import json

    from qi_agent import paths
    from qi_agent.extensions import (
        CommandRegistry,
        ExtensionApi,
        ExtensionBus,
        ExtensionContext,
        ExtensionUi,
    )
    from qi_agent.registry import ToolCatalog

    from qi_mcp import register

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    (project / ".qi").mkdir(parents=True, exist_ok=True)
    (project / ".qi" / "mcp.json").write_text(json.dumps({"mcpServers": {"test": {
        "command": sys.executable, "args": [str(server_script)],
    }}}), encoding="utf-8")

    registry = CommandRegistry()
    register(ExtensionApi(catalog=ToolCatalog(), bus=ExtensionBus(),
                          _commands=registry, _name="mcp"))
    command = registry.find("mcp")
    assert command is not None

    class _Ui:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def notify(self, message: str, *, level: str = "info") -> None:
            self.messages.append(message)

    ui = _Ui()
    await command.handler("tools", ExtensionContext(cwd=project, ui=ExtensionUi(frontend=ui)))
    text = "\n".join(ui.messages)

    assert "mcp__test__echo" in text                     # 工具名(带前缀)
    assert "原样回显" in text                            #描述来自 server 自己
    assert "mcp__test__boom" in text                     # 第二个工具也在
    assert "1 个 server" in text and "共 2 个" in text   # 计数说准
    # 没设 directTools → 全走代理,一个都不该标 [直连]
    assert "[直连]" not in text


@pytest.mark.asyncio
async def test_mcp_tools_marks_direct_tools(server_script: Path, tmp_path, monkeypatch):
    """开了 `directTools` 的 server → 那几个工具在面板里标 `[直连]`。

    这是"我在工具清单里怎么找不到它"的唯一线索:直连的会直接出现在模型工具清单里,
    没标的只能经 `mcp` 代理调。
    """
    import json

    from qi_agent import paths
    from qi_agent.extensions import (
        CommandRegistry,
        ExtensionApi,
        ExtensionBus,
        ExtensionContext,
        ExtensionUi,
    )
    from qi_agent.registry import ToolCatalog

    from qi_mcp import register

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    (project / ".qi").mkdir(parents=True, exist_ok=True)
    (project / ".qi" / "mcp.json").write_text(json.dumps({"mcpServers": {"test": {
        "command": sys.executable, "args": [str(server_script)],
        "directTools": ["echo"],                          # 只直连 echo,boom 仍走代理
    }}}), encoding="utf-8")

    registry = CommandRegistry()
    register(ExtensionApi(catalog=ToolCatalog(), bus=ExtensionBus(),
                          _commands=registry, _name="mcp"))
    command = registry.find("mcp")
    assert command is not None

    class _Ui:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def notify(self, message: str, *, level: str = "info") -> None:
            self.messages.append(message)

    ui = _Ui()
    await command.handler("tools test", ExtensionContext(cwd=project,
                                                         ui=ExtensionUi(frontend=ui)))
    lines = [ln for ln in "\n".join(ui.messages).splitlines() if "mcp__test__" in ln]
    echo = next(ln for ln in lines if "echo" in ln)
    boom = next(ln for ln in lines if "boom" in ln)
    assert "[直连]" in echo
    assert "[直连]" not in boom


# ── `session_shutdown`:退出时真把子进程关掉 ────────────────

@pytest.mark.asyncio
async def test_session_shutdown_kills_the_stdio_child(server_script: Path, tmp_path,
                                                      monkeypatch):
    """退出 / 换会话 / 重载前，qi-mcp 必须**真关连接** —— 断言子进程没了。

    回归：`ServerManager.aclose()` 一直写好也测过，但**没有任何调用方**（全仓 grep 只有
    测试自己）。所以每个用过 MCP 的会话退出后都留下一个活着的 `python server.py`。
    这条端到端跑真子进程：靠 server 自报 pid，再从外部看它是不是真的死了 ——
    `SessionClient` 不暴露 process，只断言 `aclose()` 被调过就本末倒置了。
    """
    import json
    import os

    from qi_agent import paths
    from qi_agent.extensions import (
        CommandRegistry,
        ExtensionApi,
        ExtensionBus,
        ExtensionContext,
        ExtensionUi,
    )
    from qi_agent.registry import ToolCatalog

    from qi_mcp import register

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    (project / ".qi").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))

    pidfile = tmp_path / "server.pid"
    (project / ".qi" / "mcp.json").write_text(json.dumps({"mcpServers": {"test": {
        "command": sys.executable,
        "args": [str(server_script)],
        "env": {"QI_TEST_MCP_PIDFILE": str(pidfile)},
    }}}), encoding="utf-8")

    bus = ExtensionBus()
    registry = CommandRegistry()
    register(ExtensionApi(catalog=ToolCatalog(), bus=bus,
                          _commands=registry, _name="mcp"))

    class _Ui:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def notify(self, message: str, *, level: str = "info") -> None:
            self.messages.append(message)

    ui = _Ui()
    ctx = ExtensionContext(cwd=project, ui=ExtensionUi(frontend=ui))

    def _alive() -> bool:
        if not pidfile.is_file():
            return False
        try:
            pid = int(pidfile.read_text())
        except ValueError:               # 文件还在写 / 内容不是数字
            return False
        try:
            os.kill(pid, 0)
        except OSError as exc:
            # 只要一个子句:`PermissionError` = 进程存在但不属于我们 → 算活着;
            # 其余(ProcessLookupError 等)= 已经没了。(写两个子句会被一个
            # 语法型规则误判成“前面那个 catch 了全部”—— 它分不出哪个类型更宽。)
            return isinstance(exc, PermissionError)
        return True

    try:
        # `/mcp tools` 会真连 → 子进程起来并写下 pid
        command = registry.find("mcp")
        assert command is not None
        await command.handler("tools", ctx)
        assert "mcp__test__echo" in "\n".join(ui.messages)
        assert pidfile.is_file(), "server 没写下 pid —— 前面的连接本身就没成"
        assert _alive(), "刚连上，子进程应该是活的"

        # 宿主在退出 / 换会话 / 重载前发这个事件
        await bus.emit("session_shutdown", {"reason": "quit"}, ctx=ctx)

        # 收尾是异步的（先退 stdio ctx 再收进程），给一点时间轮询
        for _ in range(100):
            if not _alive():
                break
            await asyncio.sleep(0.05)
        assert not _alive(), "session_shutdown 之后 stdio 子进程还活着 —— 连接没被关"
    finally:
        # 测试自己失败时也别把子进程留在机器上
        if _alive():
            with contextlib.suppress(OSError):     # 尽力而为:进程可能刚好自己退了
                os.kill(int(pidfile.read_text()), 9)


@pytest.mark.asyncio
async def test_session_shutdown_is_idempotent_and_safe_without_managers(tmp_path,
                                                                       monkeypatch):
    """没人连过（或发两次）都不该炸 —— 宿主在 `quit` 与显式 exit 两条路都可能发。"""
    from qi_agent import paths
    from qi_agent.extensions import (
        CommandRegistry,
        ExtensionApi,
        ExtensionBus,
        ExtensionContext,
    )
    from qi_agent.registry import ToolCatalog

    from qi_mcp import register

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))

    bus = ExtensionBus()
    # `register` 会 `registerCommand` —— 没给登记处它会**大声报错**(那是刻意的)。
    register(ExtensionApi(catalog=ToolCatalog(), bus=bus,
                          _commands=CommandRegistry(), _name="mcp"))
    ctx = ExtensionContext(cwd=project)
    assert bus.has("session_shutdown"), "qi-mcp 该订阅这个事件"

    await bus.emit("session_shutdown", {"reason": "quit"}, ctx=ctx)
    await bus.emit("session_shutdown", {"reason": "quit"}, ctx=ctx)   # 幂等


# ── 列工具超时:卡住的 server 不能把调用方永久挂住 ─────────────

#: 一个 `tools/list` **永不返回**的 server。
#: 手法:`MCPServer._handle_list_tools` 只是 `await self.list_tools()`,而 `list_tools`
#: 是**公开**方法 —— 换掉它就等于让 tools/list 卡住(比 monkeypatch 私有 handler 稳,
#: 那个在 lowlevel server 构造时就已经绑定好了)。`init` 照常快,所以超时只可能发生在列工具。
HANGING_SERVER_SOURCE = '''
import anyio

from mcp.server.mcpserver import MCPServer

server = MCPServer("qi-hang")


@server.tool()
def ping() -> str:
    """占位工具(这个 server 的价值只在于它的 tools/list 会卡住)。"""
    return "pong"


async def _hang(*_args, **_kwargs):
    await anyio.sleep(3600)
    raise AssertionError("永远走不到这里")


server.list_tools = _hang           # `_handle_list_tools` 会 await 它
server.run()
'''


@pytest.fixture(scope="module")
def hanging_script(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("mcp-hang") / "hang.py"
    path.write_text(HANGING_SERVER_SOURCE, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_hanging_list_tools_times_out_instead_of_hanging_forever(
        server_script: Path, hanging_script: Path):
    """server 在 `tools/list` 卡住 → **有界返回** + 记 note,而不是永久等下去。

    回归:`ClientSession.list_tools()` **不收** `read_timeout_seconds`(签名只有 `params`,
    只有 `call_tool` 收)—— 所以以前列工具**完全没有超时**,`mcp({search})` 与 `/mcp tools`
    会一直挂住。现在超时定在**会话**上,覆盖所有请求。

    顺带钉住第二件事:卡住的那个**不会拖住**好用的那个(并发 + 各自超时)。
    """
    import time

    hang = ServerSpec(name="hang",
                      config={"command": sys.executable, "args": [str(hanging_script)],
                              "requestTimeoutMs": 1500},          # 1.5s,留出启动余量
                      scope="global")
    good = _spec(server_script, name="good")
    manager = ServerManager({"hang": hang, "good": good}, connect)

    started = time.monotonic()
    infos = await manager.tools()
    elapsed = time.monotonic() - started
    await manager.aclose()

    assert {i.name for i in infos} == {"echo", "boom"}       # 好的那个照常拿到
    assert any("hang" in n and "列工具失败" in n for n in manager.notes), manager.notes
    # 串行 + 无超时的话这里是 3600s;有界即可,阈值给宽一点避免慢机器误报
    assert elapsed < 20, f"列工具没有被超时兜住:{elapsed:.1f}s"


# ── 输出护栏:第三方 server 的巨量结果不能打爆上下文 ────────────

#: 一个返回**巨量文本**的 server —— 专门用来验护栏。
BIG_SERVER_SOURCE = '''
from mcp.server.mcpserver import MCPServer

server = MCPServer("qi-big")


@server.tool()
def big(lines: int) -> str:
    """返回 lines 行文本(每行 48 字),用来验输出护栏。"""
    return "\\n".join(f"line {i} " + "x" * 40 for i in range(lines))


server.run()
'''


@pytest.fixture(scope="module")
def big_script(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("mcp-big") / "big.py"
    path.write_text(BIG_SERVER_SOURCE, encoding="utf-8")
    return path


def test_output_guard_truncates_by_chars():
    """超字符数 → 留 head + 说明里带上**总量**(不然模型会把截断当成“就这么多”)。"""
    from qi_mcp.client import MAX_RESULT_CHARS, _guard_result

    text = "y" * (MAX_RESULT_CHARS + 5_000)
    out = _guard_result(text)

    assert len(out) < len(text)
    assert len(out) <= MAX_RESULT_CHARS + 200          # head + 那行说明
    assert "已截断" in out
    assert "字" in out and str(len(text)) in out        # 总量报出来


def test_output_guard_truncates_by_lines():
    """行数超了也截:很多短行时字符数可能不超,但行数能把上下文拖长。"""
    from qi_mcp.client import MAX_RESULT_LINES, _guard_result

    text = "\n".join(f"l{i}" for i in range(MAX_RESULT_LINES + 100))
    out = _guard_result(text)

    assert out.count("\n") < MAX_RESULT_LINES + 5
    assert "已截断" in out
    assert str(MAX_RESULT_LINES + 100) in out           # 总行数
    assert "l0" in out                                  # head 保住了
    assert f"l{MAX_RESULT_LINES + 99}" not in out        # 尾巴确实被砍了


def test_output_guard_leaves_normal_results_alone():
    """正常大小的结果**一个字符都不动** —— 护栏不该在常见路径上留痕。"""
    from qi_mcp.client import _guard_result

    text = "line 1\nline 2\nline 3"
    assert _guard_result(text) == text


def test_output_guard_can_be_disabled(monkeypatch):
    """`MCP_OUTPUT_GUARD=0` → 原样返回(急用时的逃生口,与 pi 同名同义)。"""
    from qi_mcp.client import MAX_RESULT_CHARS, _guard_result

    text = "z" * (MAX_RESULT_CHARS + 1_000)
    monkeypatch.setenv("MCP_OUTPUT_GUARD", "0")
    assert _guard_result(text) == text


@pytest.mark.asyncio
async def test_huge_tool_result_is_guarded_end_to_end(big_script: Path):
    """**端到端**:真 server 回 ~950KB 文本 → 到模型这边必须是有界的,且带截断说明。

    `runtime.py` 那道上限只管**落盘**;模型上下文这条路上以前没有任何限制 ——
    代理工具与直连工具都经 `_render_call_result`,所以这是唯一的收口处。
    """
    from qi_mcp.client import MAX_RESULT_CHARS

    manager = ServerManager({"big": _spec(big_script, name="big")}, connect)
    try:
        out = await manager.call("big", "big", {"lines": 20_000})
    finally:
        await manager.aclose()

    assert "line 0" in out                              # head 在
    assert "line 19999" not in out                      # 尾巴被砍
    assert "已截断" in out
    assert len(out) <= MAX_RESULT_CHARS + 200, f"护栏没生效:{len(out)} 字"
