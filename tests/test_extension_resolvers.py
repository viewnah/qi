"""能力交接:提供方 `registerResolver` / 消费方 `resolveTools`(E20 / §7.5)。

这一步补的是 core 的**消费方**那半 —— `provides_config` 此前只有"声明"。三条不变量:

1. **没人提供 → `[]`**(优雅降级):装 qi-agents 不装 qi-mcp 时角色照跑,只是没有 MCP 工具。
   这是 E20 选"能力交接"而不是 `import` 兄弟扩展的**全部理由**,所以它由测试守着;
2. 多个提供者 → 合并,按**登记顺序**(qi-mcp + 第三方的 MCP 实现可以并存);
3. 提供者自己的失败**不在这里捕** —— 一个 server 起不来是 qi-mcp 的事(它该记 note 并跳过
   那个 server),不是每个消费方都要重新决定一遍。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from qi_agent.extensions import ExtensionApi, ExtensionBus, Tool  # noqa: E402
from qi_agent.registry import CapabilityRegistry, ToolCatalog  # noqa: E402

KIND = "mcp_servers"


def _api(registry: CapabilityRegistry | None, name: str = "probe") -> ExtensionApi:
    return ExtensionApi(catalog=ToolCatalog(), bus=ExtensionBus(), _name=name,
                        _capabilities=registry)


async def _echo(args: dict, ctx: object) -> str:
    """工具执行器必须是 **async**(`ToolExecutor` 要求 awaitable)。"""
    return "ok"


def _tool(name: str) -> Tool:
    return Tool(name, "演示用", {"type": "object", "properties": {}}, _echo)


# ── 优雅降级(选 E20 的全部理由)────────────────────────

@pytest.mark.asyncio
async def test_no_provider_returns_empty_instead_of_failing():
    """**没有提供者不是错误。** 它必须是 []，否则"只装 qi-agents"就不可用。"""
    api = _api(CapabilityRegistry())
    assert await api.resolveTools(KIND, scope=Path("/some/agent")) == []
    assert await api.resolveTools("没人管的种类") == []


@pytest.mark.asyncio
async def test_resolve_without_a_host_registry_is_also_empty():
    """宿主没注入汇合点(老宿主 / 单测里手搓的 api)→ 同样降级成 []，不抛。"""
    api = _api(None)
    assert await api.resolveTools(KIND) == []


# ── 合并 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_provider_tools_are_returned():
    registry = CapabilityRegistry()
    _api(registry).registerResolver(KIND, lambda scope=None: [_tool("mcp__demo__echo")])
    got = await _api(registry).resolveTools(KIND)
    assert [t.name for t in got] == ["mcp__demo__echo"]


@pytest.mark.asyncio
async def test_multiple_providers_merge_in_registration_order():
    """两个提供者各给一批 —— qi-mcp 与"别人的 MCP 实现"可以并存,顺序稳定可预期。"""
    registry = CapabilityRegistry()
    _api(registry, "first").registerResolver(KIND, lambda scope=None: [_tool("a")])
    _api(registry, "second").registerResolver(KIND, lambda scope=None: [_tool("b"), _tool("c")])

    got = await _api(registry, "consumer").resolveTools(KIND)
    assert [t.name for t in got] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_a_resolver_may_return_a_single_tool_or_nothing():
    """宽容:写扩展的人常常 `return some_tool` 或干脆 `return None`(无事可做)。"""
    registry = CapabilityRegistry()
    _api(registry).registerResolver(KIND, lambda scope=None: _tool("solo"))
    _api(registry, "quiet").registerResolver(KIND, lambda scope=None: None)

    got = await _api(registry).resolveTools(KIND)
    assert [t.name for t in got] == ["solo"]


@pytest.mark.asyncio
async def test_async_resolver_is_awaited():
    """真会用到:MCP 要起进程 / 建连接(list_tools),天然是 async。"""
    registry = CapabilityRegistry()

    async def resolve(scope=None):
        return [_tool("mcp__slow__x")]

    _api(registry).registerResolver(KIND, resolve)
    got = await _api(registry).resolveTools(KIND)
    assert [t.name for t in got] == ["mcp__slow__x"]


@pytest.mark.asyncio
async def test_resolver_receives_the_scope():
    """`scope` 是提供方与消费方之间的事(§7.4:qi 里通常是 agent 目录),core 只负责传。"""
    registry = CapabilityRegistry()
    seen: list[object] = []

    def resolve(scope=None):
        seen.append(scope)
        return []

    _api(registry).registerResolver(KIND, resolve)
    scope = Path("/agents/reviewer")
    await _api(registry).resolveTools(KIND, scope=scope)
    assert seen == [scope]


# ── 失败面 ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_provider_failure_propagates_instead_of_being_swallowed():
    """这里**故意不捕** —— 静默吞掉提供者的 bug,现场只会看到"工具莫名少了几个"。"""
    registry = CapabilityRegistry()

    def boom(scope=None):
        raise RuntimeError("server 起不来")

    _api(registry).registerResolver(KIND, boom)
    with pytest.raises(RuntimeError, match="server 起不来"):
        await _api(registry).resolveTools(KIND)


# ── 与声明那一半的关系 ──────────────────────────────────

def test_register_resolver_also_declares_the_kind():
    """能解析就等于在管这个种类 —— 不必再写一遍 `provides_config`(容易忘)。"""
    registry = CapabilityRegistry()
    api = _api(registry, "qi-mcp")
    api.registerResolver(KIND, lambda scope=None: [])

    assert registry.has_resolver(KIND)
    registry.merge(api)                      # 发现阶段的汇总
    assert registry.has_provider(KIND)       # 声明那一半也生效了
    assert KIND in registry.kinds


def test_register_resolver_without_a_host_registry_raises():
    """与 `registerCommand` / `registerFlag` 同一套规则:没汇合点就**大声报错**。"""
    with pytest.raises(RuntimeError, match="能力汇合点"):
        _api(None).registerResolver(KIND, lambda scope=None: [])


# ── 端到端:真扩展文件走发现阶段 ────────────────────────

def test_discovery_injects_the_registry_into_extensions(tmp_path):
    """`discover_extensions` 要把汇合点注入进每个 api,否则扩展里的 `registerResolver` 会报错。"""
    from qi_agent.registry import discover_extensions

    ext_dir = tmp_path / ".qi" / "extensions" / "fake-mcp"
    ext_dir.mkdir(parents=True)
    (ext_dir / "extension.py").write_text(
        "from qi_agent.extensions import Tool\n"
        "\n"
        "async def _echo(args, ctx):\n"
        "    return 'ok'\n"
        "\n"
        "def register(api):\n"
        "    def resolve(scope=None):\n"
        "        return [Tool('mcp__demo__echo', '回显', {'type': 'object', 'properties': {}},\n"
        "                     _echo)]\n"
        "    api.registerResolver('mcp_servers', resolve)\n",
        encoding="utf-8")

    capabilities = CapabilityRegistry()
    catalog = ToolCatalog()
    warnings: list[str] = []
    discover_extensions(catalog, capabilities, tmp_path, bus=ExtensionBus(),
                        on_warning=warnings.append)

    assert warnings == []
    assert capabilities.has_resolver(KIND)


@pytest.mark.asyncio
async def test_discovered_resolver_is_reachable_from_another_extension(tmp_path):
    """**消费方来自另一个扩展的另一半** —— 这就是 qi-agents 问 qi-mcp 的实际路径。"""
    from qi_agent.registry import discover_extensions

    d = tmp_path / ".qi" / "extensions" / "provider"
    d.mkdir(parents=True)
    (d / "extension.py").write_text(
        "from qi_agent.extensions import Tool\n"
        "\n"
        "async def _echo(args, ctx):\n"
        "    return 'ok'\n"
        "\n"
        "def register(api):\n"
        "    api.registerResolver('mcp_servers', lambda scope=None: [\n"
        "        Tool('mcp__g__x', 'd', {}, _echo)])\n",
        encoding="utf-8")

    capabilities = CapabilityRegistry()
    discover_extensions(ToolCatalog(), capabilities, tmp_path, bus=ExtensionBus())

    # 消费方(qi-agents / qi-web)拿到的是**同一个**汇合点,所以看得见 provider 登记的解析器
    got = await capabilities.resolve_tools(KIND)
    assert [t.name for t in got] == ["mcp__g__x"]
