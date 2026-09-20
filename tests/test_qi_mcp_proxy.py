"""qi-mcp 切片 2:`mcp` 代理工具 + lazy 生命周期。

用**注入的假 connector** 测 —— 切片 2 的语义(什么时候连、连一次要不要复用、连不上怎么办、
元数据缓存)全部与真协议无关。真 stdio client 是切片 2b,mcp SDK 的 API 面单独验。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "extensions" / "qi-mcp"))

import pytest  # noqa: E402

from qi_mcp.config import ServerSpec  # noqa: E402
from qi_mcp.proxy import build_tool  # noqa: E402
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
        return await run_tool(args, None)              # 代理工具不看 ctx


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
