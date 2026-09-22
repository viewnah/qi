"""qi-mcp 切片 3:直连(`directTools`)+ 过滤(`includeTools`/`excludeTools`)+ `toolPrefix`。

三块:
1. **命名**:`mcp__<server>__<tool>`(E22 默认)/ `<server>__<tool>` / 原名;
2. **过滤**:include → exclude 的顺序,匹配原名与生成名两种写法;
3. **接线**:`session_start` 时把 `directTools` 点名的工具注册进 catalog 并加入当前工具集 ——
   `register_direct_tools` 抽成独立函数,所以能直接测,不必真起一个会话。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "extensions" / "qi-mcp"))

import pytest  # noqa: E402

from qi_agent.extensions import ExtensionApi, ExtensionBus, Tool  # noqa: E402
from qi_agent.registry import CommandRegistry, ToolCatalog  # noqa: E402
from qi_mcp import register_direct_tools  # noqa: E402
from qi_mcp.config import ServerSpec  # noqa: E402
from qi_mcp.direct import (  # noqa: E402
    direct_selection,
    prefix_mode,
    select_tools,
    tool_name,
)
from qi_mcp.servers import ServerManager, ToolInfo  # noqa: E402

_SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


async def _noop(args: dict, ctx: Any) -> str:
    return "ok"


class FakeClient:
    def __init__(self, tools) -> None:
        self._tools = list(tools)
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        return list(self._tools)

    async def call_tool(self, name: str, args: dict) -> str:
        self.calls.append((name, args))
        return f"ok:{name}"

    async def aclose(self) -> None:
        pass


class FakeHost:
    """宿主侧工具集读写面(`getActiveTools` / `setActiveTools` 的后端)。"""

    def __init__(self, names=("read", "write")) -> None:
        self.names = list(names)

    def tool_names(self) -> list[str]:
        return list(self.names)

    def set_tool_names(self, names) -> None:
        self.names = list(names)


class FakeUi:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def notify(self, message: str, *, level: str = "info") -> None:
        self.messages.append(message)


def _spec(**cfg) -> ServerSpec:
    return ServerSpec(name="gh", config={"command": "x", **cfg}, scope="global")


def _infos(*names: str, server: str = "gh") -> list[ToolInfo]:
    return [ToolInfo(server=server, name=n, description=f"{n} 做什么", schema=_SCHEMA)
            for n in names]


# ── 命名 ────────────────────────────────────────────────

def test_tool_name_modes():
    assert tool_name("gh", "create_issue") == "mcp__gh__create_issue"    # 默认(E22)
    assert tool_name("gh", "create_issue", "server") == "gh__create_issue"
    assert tool_name("gh", "create_issue", "none") == "create_issue"


def test_unknown_prefix_mode_falls_back_to_default():
    """手滑的 `toolPrefix` 不该让 server 直接不可用 —— 回落到默认并继续。"""
    assert prefix_mode(_spec(toolPrefix="nonsense")) == "mcp"
    assert prefix_mode(_spec(toolPrefix="server")) == "server"
    assert prefix_mode(_spec()) == "mcp"


# ── 过滤 ────────────────────────────────────────────────

def test_include_matches_original_and_generated_names():
    """`includeTools` 三种写法都认(照 pi):原名、`mcp__gh__*`、`gh__*`。"""
    infos = _infos("create_issue", "list_issues", "delete_repo")

    assert [i.name for i in select_tools(infos, _spec(includeTools=["*_issue*"]))] == \
        ["create_issue", "list_issues"]
    assert [i.name for i in select_tools(infos, _spec(includeTools=["mcp__gh__list_*"]))] == \
        ["list_issues"]
    assert [i.name for i in select_tools(infos, _spec(includeTools=["gh__delete_*"]))] == \
        ["delete_repo"]
    assert [i.name for i in select_tools(infos, _spec(includeTools="create_issue"))] == \
        ["create_issue"]                                   # 单个字符串也收


def test_exclude_applied_after_include():
    """顺序照 pi:先 include 再 exclude。反过来的话 `include: ["*"] + exclude: ["*_issue"]`
    会得出不同结果,所以这个顺序是可观察的行为。"""
    infos = _infos("create_issue", "list_issues", "delete_repo")
    kept = select_tools(infos, _spec(includeTools=["*_issue*"], excludeTools=["create_*"]))
    assert [i.name for i in kept] == ["list_issues"]


def test_no_filters_means_everything():
    infos = _infos("a", "b")
    assert len(select_tools(infos, _spec())) == 2


def test_filters_also_hide_tools_from_the_proxy():
    """过滤决定的是 server 的**可见工具集** —— 否则过滤就是装饰:代理照样搜得到、调得到。"""
    import asyncio

    client = FakeClient([("create_issue", "d", _SCHEMA), ("secret", "d", _SCHEMA)])

    async def connector(_spec):
        return client

    manager = ServerManager({"gh": _spec(excludeTools=["secret"])}, connector)
    visible = asyncio.run(manager.tools())
    assert [i.name for i in visible] == ["create_issue"]


# ── 直连选择 ────────────────────────────────────────────

def test_direct_tools_default_off():
    """**默认不直连** —— 走代理(E25)。一个 server 的工具定义轻松 10k+ token。"""
    assert direct_selection(_infos("a", "b"), _spec()) == []


def test_direct_tools_true_selects_all_filtered():
    infos = _infos("a", "b", "c")
    chosen = direct_selection(infos, _spec(directTools=True, excludeTools=["c"]))
    assert [i.name for i in chosen] == ["a", "b"]           # 过滤先生效


def test_direct_tools_list_selects_a_subset():
    infos = _infos("create_issue", "list_issues")
    chosen = direct_selection(infos, _spec(directTools=["list_issues"]))
    assert [i.name for i in chosen] == ["list_issues"]
    # 生成名写法也认
    assert [i.name for i in direct_selection(infos, _spec(directTools=["mcp__gh__create_*"]))] == \
        ["create_issue"]
    # 写错名字 → 不注册,但不抛(由 /mcp 面板与 note 反映)
    assert direct_selection(infos, _spec(directTools=["nope"])) == []


# ── 接线:session_start 时注册 ──────────────────────────

def _api(host: FakeHost) -> ExtensionApi:
    return ExtensionApi(catalog=ToolCatalog(), bus=ExtensionBus(), _name="mcp", _host=host,
                        _commands=CommandRegistry())


def _manager(spec: ServerSpec, tools) -> ServerManager:
    client = FakeClient([(n, f"{n} 做什么", _SCHEMA) for n in tools])

    async def connector(_spec):
        return client

    return ServerManager({spec.name: spec}, connector)


@pytest.mark.asyncio
async def test_direct_tools_are_registered_and_activated():
    spec = _spec(directTools=True)
    manager = _manager(spec, ["create_issue", "list_issues"])
    host, ui = FakeHost(), FakeUi()
    api = _api(host)

    added = await register_direct_tools(api, SimpleNamespace(cwd=Path("/tmp"), ui=ui),
                                        manager=manager)

    assert added == ["mcp__gh__create_issue", "mcp__gh__list_issues"]
    assert set(added) <= set(api.catalog.names)              # 进了 catalog
    assert host.names == ["read", "write", *added]           # 并在当前工具集里(原有工具没丢)


@pytest.mark.asyncio
async def test_registered_tool_actually_calls_through():
    spec = _spec(directTools=["create_issue"])
    manager = _manager(spec, ["create_issue"])
    api = _api(FakeHost())

    await register_direct_tools(api, SimpleNamespace(cwd=Path("/tmp"), ui=FakeUi()),
                                manager=manager)
    tool = api.catalog.get("mcp__gh__create_issue")           # 拿回注册的那个
    assert tool is not None
    assert await tool.execute({"q": "x"}, None) == "ok:create_issue"
    assert tool.parameters == _SCHEMA                          # 参数 schema 原样带过来
    assert tool.prompt_line == "create_issue 做什么"            # 描述进提示词那一行


@pytest.mark.asyncio
async def test_direct_tools_off_registers_nothing():
    manager = _manager(_spec(), ["create_issue"])
    host = FakeHost()
    api = _api(host)
    assert await register_direct_tools(api, SimpleNamespace(cwd=Path("/tmp"), ui=FakeUi()),
                                       manager=manager) == []
    assert host.names == ["read", "write"]                    # 工具集没被动过


@pytest.mark.asyncio
async def test_name_collision_is_skipped_and_explained():
    """撞名**跳过并说明**,不静默覆盖 —— 静默覆盖在现场的表现是"我的工具被顶掉了"。

    真实场景里内置工具就在 catalog 里(core 注册的),所以这里也放一个 —— 否则"撞名"
    是假的:host 的工具集里有 `read`,但 catalog 里没它。
    """
    spec = _spec(directTools=True, toolPrefix="none")          # 原名 → 容易撞
    manager = _manager(spec, ["read"])
    host, ui = FakeHost(), FakeUi()
    api = _api(host)
    api.catalog.register(Tool("read", "读文件", {"type": "object", "properties": {}}, _noop))

    added = await register_direct_tools(api, SimpleNamespace(cwd=Path("/tmp"), ui=ui),
                                        manager=manager)

    assert added == []
    assert any("同名" in m and "toolPrefix" in m for m in ui.messages)


@pytest.mark.asyncio
async def test_connection_failure_does_not_break_startup():
    """一个 server 连不上 → 记 note、不注册、**不抛**(否则会话起不来)。"""
    spec = _spec(directTools=True)

    async def connector(_spec):
        raise RuntimeError("起不来")

    manager = ServerManager({"gh": spec}, connector)
    ui = FakeUi()
    added = await register_direct_tools(_api(FakeHost()),
                                        SimpleNamespace(cwd=Path("/tmp"), ui=ui),
                                        manager=manager)
    assert added == []
    assert "gh" in manager.failed
    assert any("连接失败" in n for n in manager.notes)


@pytest.mark.asyncio
async def test_disabled_server_is_not_connected_for_direct_tools():
    spec = _spec(directTools=True, disabled=True)
    called: list[str] = []

    async def connector(s):
        called.append(s.name)
        return FakeClient([])

    manager = ServerManager({"gh": spec}, connector)
    await register_direct_tools(_api(FakeHost()), SimpleNamespace(cwd=Path("/tmp"), ui=FakeUi()),
                               manager=manager)
    assert called == []


@pytest.mark.asyncio
async def test_activation_is_not_pinned_to_a_stale_snapshot():
    """直连注册**不能**把工具集变成一个静态覆盖列表。

    `setActiveTools` 的语义是**覆盖**,不是"加进去"。此前这段无条件执行:一旦直连注册过,
    当前集合就冻结成"当时那一份 + 直连工具" —— 此后任何**动态注册**的工具(另一个扩展的,
    或本扩展下一条 `session_start` 加的)都被挡在外面,而症状只是"装了新工具却调不到"。

    判据:没人收窄过工具集(集合 === catalog)时**什么都不该做** —— catalog 里刚注册进来的
    工具本来就在集合里。
    """
    spec = _spec(directTools=True)
    manager = _manager(spec, ["create_issue"])

    class CountingHost(FakeHost):
        """真宿主:工具集默认等于 catalog(`tool_names()` 就是 catalog 的全部)。"""

        def __init__(self) -> None:
            super().__init__(names=[])
            self.set_calls = 0

        def tool_names(self) -> list[str]:
            return sorted(self.names)

        def set_tool_names(self, names) -> None:
            self.set_calls += 1
            super().set_tool_names(names)

    host = CountingHost()
    api = ExtensionApi(catalog=ToolCatalog(), bus=ExtensionBus(), _name="mcp",
                       _host=host, _commands=CommandRegistry())
    api.catalog.register(Tool("read", "读", {"type": "object", "properties": {}}, _noop))
    host.names = ["read"]                      # 集合 === catalog → 没人收窄过

    added = await register_direct_tools(api, SimpleNamespace(cwd=Path("/tmp"), ui=FakeUi()),
                                        manager=manager)

    assert added == ["mcp__gh__create_issue"]
    assert host.set_calls == 0, "集合与 catalog 一致时不该去覆盖工具集"


@pytest.mark.asyncio
async def test_activation_still_extends_a_narrowed_tool_set():
    """被 `--tools` 之类收窄过时,直连工具仍要补进去(原有那份不能丢)。"""
    spec = _spec(directTools=True)
    manager = _manager(spec, ["create_issue"])
    host = FakeHost(names=["read"])            # 收窄过:集合 != catalog
    api = ExtensionApi(catalog=ToolCatalog(), bus=ExtensionBus(), _name="mcp",
                       _host=host, _commands=CommandRegistry())
    for name in ("read", "write", "bash"):     # catalog 比集合大 → 判定为"被收窄过"
        api.catalog.register(Tool(name, name, {"type": "object", "properties": {}}, _noop))

    added = await register_direct_tools(api, SimpleNamespace(cwd=Path("/tmp"), ui=FakeUi()),
                                        manager=manager)

    assert added == ["mcp__gh__create_issue"]
    assert host.names == ["read", "mcp__gh__create_issue"], "收窄的那份要保留,只补直连工具"
