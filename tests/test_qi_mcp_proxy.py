"""qi-mcp 切片 2:`mcp` 代理工具 + lazy 生命周期。

用**注入的假 connector** 测 —— 切片 2 的语义(什么时候连、连一次要不要复用、连不上怎么办、
元数据缓存)全部与真协议无关。真 stdio client 是切片 2b,mcp SDK 的 API 面单独验。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "extensions" / "qi-mcp"))

import pytest  # noqa: E402

from qi_mcp.config import ServerSpec  # noqa: E402
from qi_mcp.proxy import (  # noqa: E402
    DEFAULT_SEARCH_LIMIT,
    MAX_SEARCH_LIMIT,
    build_tool,
)
from qi_mcp.servers import ServerManager, qualified, split_qualified  # noqa: E402

_ISSUE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "标题"},
        "labels": {"type": "array"},
        "state": {"type": "string", "enum": ["open", "closed"], "default": "open"},
    },
    "required": ["title"],
}


class FakeClient:
    """假的已连接 server。只实现切片 2 用到的那三个方法。"""

    def __init__(self, tools=(), *, list_error: str | None = None,
                 call_error: str | None = None) -> None:
        self._tools = list(tools)
        self._list_error = list_error
        self._call_error = call_error
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def list_tools(self):
        if self._list_error:
            raise RuntimeError(self._list_error)
        return list(self._tools)

    async def call_tool(self, name: str, args: dict) -> str:
        if self._call_error:
            raise RuntimeError(self._call_error)
        self.calls.append((name, args))
        return f"ok:{name}:{sorted(args)}"

    def alive(self) -> bool:
        """`Client` 协议的存活面（关掉就不再复用 —— 见 servers.py 的重连逻辑）。"""
        return not self.closed

    async def aclose(self) -> None:
        self.closed = True


class Harness:
    """`build_tool(manager)` + 假 connector,并记下**什么时候连了谁**。"""

    def __init__(self, servers: dict[str, ServerSpec], *, tools: dict | None = None,
                 fail: tuple[str, ...] = (), list_error: dict[str, str] | None = None,
                 call_error: dict[str, str] | None = None) -> None:
        self.log: list[str] = []
        self.clients: dict[str, FakeClient] = {}

        async def connector(spec: ServerSpec) -> FakeClient:
            self.log.append(spec.name)                 # lazy 的证据就在这个列表里
            if spec.name in fail:
                raise RuntimeError("起不来")
            client = FakeClient((tools or {}).get(spec.name, ()),
                                list_error=(list_error or {}).get(spec.name),
                                call_error=(call_error or {}).get(spec.name))
            self.clients[spec.name] = client
            return client

        self.manager = ServerManager(servers, connector)
        self.tool = build_tool(self.manager)

    async def call(self, args: dict) -> str:
        # 绑定到局部变量再调:本仓已知的 semgrep 误报模式(`.execute(` 被当成 SQL sink,
        # runner.py:421 同样处理)—— 解析器解析不出属性链的终点,只看到动态执行。
        run_tool = self.tool.execute
        result = await run_tool(args, None)            # 代理工具不看 ctx
        # 代理工具的契约是“**总是纯文本**”(它自己从不回结构化结果)—— 把它钉下来,
        # 而不是把 `str | ToolOutcome` 原样当 str 交出去。
        assert isinstance(result, str), f"代理工具应当返回纯文本,收到 {type(result).__name__}"
        return result


def spec(name: str, **cfg) -> ServerSpec:
    return ServerSpec(name=name, config={"command": "x", **cfg}, scope="global")


def _gh() -> dict[str, ServerSpec]:
    return {"gh": spec("gh")}


# ── lazy:真用才连 ──────────────────────────────────────

@pytest.mark.asyncio
async def test_nothing_connects_until_first_use():
    """**构造 manager 不等于启动任何进程** —— 这是 lazy 的全部意义。"""
    h = Harness(_gh(), tools={"gh": [("create_issue", "建 issue", _ISSUE_SCHEMA)]})
    assert h.log == [] and h.clients == {}          # 还没调用 → 一个都没连
    await h.call({"search": "issue"})
    assert h.log == ["gh"]                          # 搜元数据时才连


@pytest.mark.asyncio
async def test_metadata_is_listed_once_and_reused():
    h = Harness(_gh(), tools={"gh": [("create_issue", "建 issue", _ISSUE_SCHEMA)]})
    await h.call({"search": "issue"})
    await h.call({"search": "issue"})
    assert h.log == ["gh"]                          # 连接只有一次
    assert len(h.clients["gh"].calls) == 0          # 搜索不调工具


@pytest.mark.asyncio
async def test_disabled_server_is_never_connected():
    h = Harness({"off": spec("off", disabled=True), "on": spec("on")},
                tools={"on": [("t", "d", {})], "off": [("x", "y", {})]})
    text = await h.call({"search": "t"})
    assert h.log == ["on"]                          # disabled 的连都不连
    assert "off" not in text


# ── search ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_renders_qualified_name_description_and_params():
    h = Harness(_gh(), tools={"gh": [("create_issue", "建一个 issue", _ISSUE_SCHEMA)]})
    text = await h.call({"search": "issue"})
    assert "mcp__gh__create_issue" in text          # E22 的全名
    assert "建一个 issue" in text
    assert "title (string) (必填) - 标题" in text     # 必填 + 描述
    assert 'enum: "open", "closed"' in text         # enum 展开
    assert '[默认 "open"]' in text                  # 默认值


@pytest.mark.asyncio
async def test_search_miss_gives_a_server_overview_not_every_tool_name():
    """搜不到时**不列全部工具名** —— 列名字正是这个工具要省掉的那笔上下文。"""
    h = Harness({"gh": spec("gh"), "fg": spec("fg")},
                tools={"gh": [("create_issue", "建 issue", {})],
                       "fg": [("read_figjam", "读 Figma", {})]})
    text = await h.call({"search": "zzz"})
    assert "没有工具匹配" in text
    assert "gh(1)" in text and "fg(1)" in text      # 每 server 的工具数
    assert "create_issue" not in text               # 但名字一个都不列


@pytest.mark.asyncio
async def test_search_matches_description_too():
    h = Harness(_gh(), tools={"gh": [("weird_name", "截图用的", {})]})
    assert "mcp__gh__weird_name" in await h.call({"search": "截图"})


# ── call ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_call_by_qualified_name():
    h = Harness(_gh(), tools={"gh": [("create_issue", "d", {})]})
    text = await h.call({"tool": "mcp__gh__create_issue", "args": {"title": "x"}})
    assert text == "ok:create_issue:['title']"
    assert h.clients["gh"].calls == [("create_issue", {"title": "x"})]


@pytest.mark.asyncio
async def test_args_accepts_a_json_string():
    """pi 两种都支持:有的 provider 对复杂 schema 更稳,所以对象与 JSON 字符串都认。"""
    h = Harness(_gh(), tools={"gh": [("create_issue", "d", {})]})
    assert (await h.call({"tool": "mcp__gh__create_issue", "args": '{"title": "y"}'})
            ) == "ok:create_issue:['title']"


@pytest.mark.asyncio
async def test_bad_args_json_is_a_readable_result_not_a_crash():
    h = Harness(_gh(), tools={"gh": [("create_issue", "d", {})]})
    text = await h.call({"tool": "mcp__gh__create_issue", "args": "{这不是 JSON"})
    assert "不是合法 JSON" in text and "mcp 调用失败" in text


@pytest.mark.asyncio
async def test_bare_tool_name_works_when_unambiguous():
    h = Harness(_gh(), tools={"gh": [("create_issue", "d", {})]})
    assert (await h.call({"tool": "create_issue"})) == "ok:create_issue:[]"


@pytest.mark.asyncio
async def test_ambiguous_bare_name_asks_for_the_qualified_one():
    """两个 server 都有同名工具时必须报清楚 —— 静默挑一个会让调用去错地方。"""
    h = Harness({"a": spec("a"), "b": spec("b")},
                tools={"a": [("search", "d", {})], "b": [("search", "d", {})]})
    text = await h.call({"tool": "search"})
    assert "多个 server 里都有" in text
    assert "mcp__a__search" in text and "mcp__b__search" in text


@pytest.mark.asyncio
async def test_unknown_tool_lists_near_matches():
    h = Harness(_gh(), tools={"gh": [("create_issue", "d", {})]})
    text = await h.call({"tool": "mcp__gh__create_issu"})     # 少一个 e(全名不匹配)
    assert "不存在" in text
    assert "mcp__gh__create_issue" in text                    # 裸名相近的会提示


# ── 失败面:一个 server 坏了不拖垮别的 ────────────────────

@pytest.mark.asyncio
async def test_connect_failure_is_a_note_and_other_servers_still_work():
    h = Harness({"bad": spec("bad"), "good": spec("good")},
                fail=("bad",), tools={"good": [("t", "好工具", {})]})
    text = await h.call({"search": "好工具"})
    assert "mcp__good__t" in text                    # 好的照常可用
    assert "bad" in h.manager.failed
    assert any("bad" in n and "连接失败" in n for n in h.manager.notes)


@pytest.mark.asyncio
async def test_first_failure_appears_in_the_overview():
    h = Harness({"bad": spec("bad")}, fail=("bad",))
    text = await h.call({"search": "任何"})
    assert "连接失败的" in text and "bad" in text


@pytest.mark.asyncio
async def test_list_tools_failure_is_a_note_not_a_crash():
    h = Harness(_gh(), tools={"gh": [("t", "d", {})]}, list_error={"gh": "列工具炸了"})
    text = await h.call({"search": "t"})
    assert "没有工具匹配" in text                     # 降级成"没有工具",不抛
    assert any("列工具失败" in n for n in h.manager.notes)


@pytest.mark.asyncio
async def test_call_failure_is_reported_with_the_reason():
    h = Harness(_gh(), tools={"gh": [("t", "d", {})]}, call_error={"gh": "服务器 500"})
    text = await h.call({"tool": "mcp__gh__t"})
    assert "调用失败" in text and "服务器 500" in text


@pytest.mark.asyncio
async def test_failed_server_is_retried_next_time():
    """失败**不缓存**:临时的网络问题不该让一次会话永久失去一个 server。"""
    h = Harness({"bad": spec("bad")}, fail=("bad",))
    await h.call({"search": "x"})
    await h.call({"search": "x"})
    assert h.log == ["bad", "bad"]                   # 又试了一次


# ── 收尾与用法 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_args_returns_usage_and_overview():
    h = Harness(_gh(), tools={"gh": [("t", "d", {})]})
    text = await h.call({})
    assert "用法:" in text and "search" in text and "tool" in text


@pytest.mark.asyncio
async def test_close_closes_every_connected_client():
    h = Harness(_gh(), tools={"gh": [("t", "d", {})]})
    await h.call({"search": "t"})
    await h.manager.aclose()
    assert h.clients["gh"].closed is True
    assert h.manager.connected() == []


@pytest.mark.asyncio
async def test_no_servers_declared_says_so():
    h = Harness({})
    text = await h.call({"search": "x"})
    assert "没有" in text and "mcp.json" in text


# ── 纯函数:E22 的命名 ──────────────────────────────────

def test_qualified_name_round_trip():
    assert qualified("gh", "create_issue") == "mcp__gh__create_issue"
    assert split_qualified("mcp__gh__create_issue") == ("gh", "create_issue")
    assert split_qualified("create_issue") is None            # 裸名
    assert split_qualified("mcp__gh") is None                 # 缺工具名
    assert split_qualified("mcp____x") is None                # 缺 server 名
    # server 名或工具名里带下划线时:按**第一个**双下划线切,server 名不含双下划线
    assert split_qualified("mcp__my_server__do_it") == ("my_server", "do_it")


# ── 连接生命周期:并发 / 重连（servers.py 的三处修复）──────────

class ReconnectingHarness:
    """可控的连接器:能记下每次连接、能让已有连接“死掉”,还能给连接加延迟。

    比扩展 `Harness` 更直白 —— 这三条测的是**连接生命周期**本身,而不是代理工具的
    搜索/调用输出,所以这里只建生命周期需要的那点面。
    """

    def __init__(self, servers: dict[str, ServerSpec], *, delay: float = 0.0,
                 tools: dict | None = None) -> None:
        self.log: list[str] = []
        self.clients: dict[str, FakeClient] = {}
        self._tools = tools or {}

        async def connector(spec: ServerSpec) -> FakeClient:
            self.log.append(spec.name)
            if delay:
                await asyncio.sleep(delay)
            client = FakeClient(self._tools.get(spec.name, ()))
            self.clients[spec.name] = client
            return client

        self.manager = ServerManager(servers, connector)


@pytest.mark.asyncio
async def test_connections_are_not_serialized_behind_one_slow_server():
    """**并发**连:两个各慢 0.2s 的 server 应该 ~0.2s 一起回来,而不是 ~0.4s 排队。

    回归:以前是一把**全局锁**、而且跨 `await self._connect()` 持有 —— 慢的那个会把
    后面全部挡住(最坏等满它的连接超时 60s)。锁只该保护"谁能建这条连接",不保护网络。
    """
    h = ReconnectingHarness(
        {"a": spec("a"), "b": spec("b")}, delay=0.2,
        tools={"a": [("ta", "A", {})], "b": [("tb", "B", {})]},
    )
    started = time.monotonic()
    infos = await h.manager.tools()
    elapsed = time.monotonic() - started

    assert {i.name for i in infos} == {"ta", "tb"}
    assert sorted(h.log) == ["a", "b"]
    # 串行会是 0.4s+;给一点余量,但必须明显小于 2×delay
    assert elapsed < 0.35, f"看起来还是串行:{elapsed:.3f}s"


@pytest.mark.asyncio
async def test_same_server_is_connected_only_once_under_concurrency():
    """同一个 server 被并发要连接时**只连一次**(第二个等锁,拿到同一份)。"""
    h = ReconnectingHarness({"a": spec("a")}, delay=0.1, tools={"a": [("ta", "A", {})]})

    first, second = await asyncio.gather(h.manager.client("a"), h.manager.client("a"))
    assert first is second
    assert h.log == ["a"]


@pytest.mark.asyncio
async def test_dead_server_is_reconnected_instead_of_staying_broken():
    """server 中途崩掉 → 下一次要连接时**重连**,而不是把那条会话永久留给死连接。

    回归：`client()` 以前只看 `name in self._clients`，**不查存活** —— 传输断了以后
    `_clients` 里那个死 client 会被一直返回，整条会话都得到 `连接已关闭`。
    """
    h = ReconnectingHarness({"a": spec("a")}, tools={"a": [("ta", "A", {})]})
    first = await h.manager.client("a")
    assert h.log == ["a"]
    assert first is not None

    # 从 `FakeClient` 引用改:`closed` 是假件自己的面,**不在 `Client` 协议上**
    # (协议只有 list_tools / call_tool / aclose / alive)。
    h.clients["a"].closed = True                       # 模拟传输断掉
    again = await h.manager.client("a")

    assert again is not first, "死连接被原样返回了(该丢掉重连)"
    assert again is not None and again.alive()
    assert h.log == ["a", "a"], "死连接没有被丢掉重连"
    assert any("已断" in n for n in h.manager.notes)


@pytest.mark.asyncio
async def test_reconnect_refreshes_the_tool_cache():
    """重连后**工具集可能变了** → 缓存要丢掉重列,不能拿旧的那份继续用。"""
    h = ReconnectingHarness({"a": spec("a")}, tools={"a": [("old", "旧", {})]})
    assert [i.name for i in await h.manager.tools_of("a")] == ["old"]

    h.clients["a"].closed = True
    h._tools["a"] = [("new", "新", {})]                # server 重启后换了一套工具

    assert [i.name for i in await h.manager.tools_of("a")] == ["new"]


# ── search 分页:代理工具自己不能把上下文倒满 ────────────────────

def _many_tools(n: int) -> list[tuple[str, str, dict]]:
    return [(f"tool_{i:03d}", f"第 {i} 个工具", {}) for i in range(n)]


@pytest.mark.asyncio
async def test_search_is_paginated_and_says_how_to_continue():
    """`search` 默认只给 `DEFAULT_SEARCH_LIMIT` 个,并**说明**共多少、怎么翻页。

    回归:以前一次把全部匹配连同 schema 倒出来 —— 查一个宽泛的词、或某个 server 工具特别多,
    这个“为了省上下文才存在的代理工具”反而自己把上下文倒满了。
    """
    h = Harness({"a": spec("a")}, tools={"a": _many_tools(40)})
    text = await h.call({"search": "工具"})                 # 40 个全匹配

    assert f"tool_{DEFAULT_SEARCH_LIMIT - 1:03d}" in text   # 第 12 个在
    assert f"tool_{DEFAULT_SEARCH_LIMIT:03d}" not in text   # 第 13 个不在
    assert "共 40 个匹配" in text
    assert f"offset={DEFAULT_SEARCH_LIMIT}" in text          # 明确告诉怎么翻页


@pytest.mark.asyncio
async def test_search_offset_walks_to_the_next_page():
    """`offset` 真的翻页,且最后一页不再提示翻页。"""
    h = Harness({"a": spec("a")}, tools={"a": _many_tools(DEFAULT_SEARCH_LIMIT + 3)})
    page2 = await h.call({"search": "工具", "offset": DEFAULT_SEARCH_LIMIT})

    assert f"tool_{DEFAULT_SEARCH_LIMIT:03d}" in page2       # 接上了
    assert "tool_000" not in page2                           # 不重复第一页
    assert "offset=" not in page2                            # 已经是最后一页


@pytest.mark.asyncio
async def test_search_limit_is_clamped_and_tolerates_junk():
    """`limit` 是**模型给的** → 夹到合法区间、容忍字符串/负数/垃圾,不因此报错。"""
    h = Harness({"a": spec("a")}, tools={"a": _many_tools(MAX_SEARCH_LIMIT + 20)})

    huge = await h.call({"search": "工具", "limit": 100000})          # 想一次全拿
    assert f"tool_{MAX_SEARCH_LIMIT - 1:03d}" in huge
    assert f"tool_{MAX_SEARCH_LIMIT:03d}" not in huge                 # 硬上限挡住了

    assert "tool_000" in await h.call({"search": "工具", "limit": "3"})   # 字符串也行
    assert "tool_000" in await h.call({"search": "工具", "limit": None})  # None → 默认
    assert "tool_000" in await h.call({"search": "工具", "limit": -5})    # 负数 → 夹到 1
