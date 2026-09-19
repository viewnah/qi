"""P-E2b:运行时工具集(`registerTool` 动态注册 / `getAllTools` / `setActiveTools`)+ `exec`。

为什么把“工具集”单独测:它是**权限面**。`setActiveTools(["read"])` 那种调用如果因为
参数名打错、层级搞错而被静默忽略,表现是“看起来进了只读档,其实什么都能改” ——
错的方向恰好是最危险的那一边。所以这里既测“生效”,也测“不生效时报不报”。

`exec` 同样:不经 shell 是它的**契约**(args 原样进 argv),而不是实现细节 ——
所以有一条测试专门用带空格的参数来钉这件事。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.abort import AbortSignal  # noqa: E402
from qi_agent.extensions import (  # noqa: E402
    ExtensionApi,
    ExtensionBus,
    Tool,
    exec_command,
)
from qi_agent.registry import EXTENSION_ENTRY_FILE, ToolCatalog  # noqa: E402
from qi_agent.tools import register_builtin_tools  # noqa: E402


async def _noop(args: dict, ctx: object) -> str:
    return ""


def _tool(name: str, **kw) -> Tool:
    return Tool(name, f"{name} 的描述", {"type": "object", "properties": {}}, _noop, **kw)


class _Host:
    """最小宿主:只实现契约要的两个方法 + notes。"""

    def __init__(self) -> None:
        self.current: list[str] = ["read", "bash"]
        self.notes: list[str] = []

    def tool_names(self) -> list[str]:
        return list(self.current)

    def set_tool_names(self, names) -> None:
        self.current = list(names)


def _api(catalog: ToolCatalog | None = None, host=None) -> ExtensionApi:
    c = catalog or ToolCatalog()
    if catalog is None:
        register_builtin_tools(c)
    return ExtensionApi(catalog=c, bus=ExtensionBus(), _name="probe", _host=host)


# ── getAllTools ─────────────────────────────────────────

def test_get_all_tools_returns_metadata_with_source_info():
    """`getAllTools()` 是**纯 catalog 查询** —— 装载阶段就能用,不需要宿主。"""
    api = _api()
    tools = api.getAllTools()
    assert [t["name"] for t in tools] == sorted(api.catalog.names)   # 按名排序,稳定

    read = next(t for t in tools if t["name"] == "read")
    assert read["source_info"]["source"] == "builtin"
    assert read["prompt_snippet"]                       # 内置工具有真 snippet
    assert isinstance(read["prompt_guidelines"], list)
    assert read["parameters"]["type"] == "object"


def test_get_all_tools_can_filter_by_source():
    """pi 的典型用法:按 `source_info.source` 区分内置与扩展工具。"""
    catalog = ToolCatalog()
    register_builtin_tools(catalog)
    api = _api(catalog)
    api.registerTool(_tool("ext_tool"))

    builtin = [t for t in api.getAllTools() if t["source_info"]["source"] == "builtin"]
    mine = [t for t in api.getAllTools() if t["source_info"]["source"] == "probe"]
    assert "read" in [t["name"] for t in builtin]
    assert [t["name"] for t in mine] == ["ext_tool"]


# ── active tools ────────────────────────────────────────

def test_get_active_tools_without_host_falls_back_to_the_catalog():
    """没宿主时退回“catalog 里的全部”—— 文档写明的降级,不是静默乱猜。"""
    api = _api()
    assert api.getActiveTools() == sorted(api.catalog.names)


def test_get_and_set_active_tools_with_host():
    host = _Host()
    api = _api(host=host)
    assert api.getActiveTools() == ["read", "bash"]     # 读的是宿主的当前值
    api.setActiveTools(["read"])
    assert host.current == ["read"]
    assert api.getActiveTools() == ["read"]


def test_set_active_tools_without_host_is_loud():
    """没有宿主就**报错**,不是静默无效 —— 静默会让“只读档”变成一句空话。"""
    api = _api()
    with pytest.raises(RuntimeError, match="setActiveTools"):
        api.setActiveTools(["read"])


def test_set_active_tools_filters_unknown_names_but_says_so():
    """未知名字被过滤(pi 允许先写名字、工具随后才注册),但要**看得见**。"""
    host = _Host()
    api = _api(host=host)
    api.setActiveTools(["read", "typo_tool"])
    assert host.current == ["read", "typo_tool"]        # 原样交给宿主(宿主自己按 catalog 滤)
    assert any("typo_tool" in n for n in host.notes)


# ── exec ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exec_captures_stdout_and_exit_code():
    res = await exec_command(sys.executable, ["-c", "print('你好')"])
    assert res.ok and res.code == 0
    assert res.stdout.strip() == "你好"
    assert res.stderr == "" and res.killed is False
    assert res.duration_ms >= 0


@pytest.mark.asyncio
async def test_exec_reports_nonzero_exit_without_raising():
    """非零退出是**结果**不是异常:扩展要能自己决定怎么处理。"""
    res = await exec_command(sys.executable, ["-c", "import sys; sys.exit(3)"])
    assert res.code == 3 and res.ok is False and res.killed is False


@pytest.mark.asyncio
async def test_exec_does_not_go_through_a_shell():
    """`args` 原样进 argv —— 带空格的参数不会被拆成两个,也不需要扩展自己转义。"""
    res = await exec_command(sys.executable,
                             ["-c", "import sys; print(repr(sys.argv[1:]))", "a b", "$HOME"])
    assert res.stdout.strip() == "['a b', '$HOME']"     # shell 会展开 $HOME、拆 a b


@pytest.mark.asyncio
async def test_exec_kills_on_timeout():
    res = await exec_command(sys.executable, ["-c", "import time; time.sleep(30)"],
                             timeout=0.2)
    assert res.killed is True
    assert res.code is None                             # 被信号杀掉 → 没有退出码


@pytest.mark.asyncio
async def test_exec_kills_on_abort_signal():
    """协作式中断在子进程上只能体现为 kill —— 子进程不会看 Python 的 flag。"""
    signal = AbortSignal()
    task = asyncio.ensure_future(
        exec_command(sys.executable, ["-c", "import time; time.sleep(30)"], signal=signal))
    await asyncio.sleep(0.1)
    signal.abort()
    res = await asyncio.wait_for(task, timeout=5)
    assert res.killed is True and res.code is None


@pytest.mark.asyncio
async def test_exec_truncates_huge_output():
    from qi_agent.extensions import MAX_EXEC_OUTPUT
    res = await exec_command(sys.executable,
                             ["-c", f"print('x' * {MAX_EXEC_OUTPUT + 1000})"])
    assert len(res.stdout) < MAX_EXEC_OUTPUT + 100
    assert res.stdout.endswith("…(输出已截断)")


# ── 端到端:扩展在 session_start 里改工具集,真的传到 runner ──

_MINIMAL_MODELS = json.dumps({
    "providers": {"ollama": {"api": "openai-completions",
                             "baseUrl": "http://127.0.0.1:11434/v1",
                             "models": [{"id": "x"}]}},
})


def _env(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "models.json").write_text(_MINIMAL_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"defaultProvider": "ollama", "defaultModel": "x"}), encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.delenv("QI_THEME", raising=False)


class _CapturingLLM:
    """只记录“这一轮被告知可用哪些工具”,然后干净收尾。"""

    def __init__(self) -> None:
        self.seen_tools: list[str] | None = None

    async def chat(self, messages, tools=None, temperature=None):
        from qi_agent.llm import ChatResponse

        self.seen_tools = sorted(t["function"]["name"] for t in (tools or []))
        return ChatResponse(text="好", tool_calls=[])


@pytest.mark.asyncio
async def test_set_active_tools_from_session_start_reaches_the_runner(tmp_path, monkeypatch):
    """一条链:扩展在 `session_start` 里 `setActiveTools(["read"])` → 下一轮的
    tool schema 里真的只有 read。

    这条比“读 `runtime.tool_names()`”强:它证明覆盖穿过了 runner(而不是只改了个字段)。
    """
    _env(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    d = paths.project_home(project) / paths.EXTENSIONS_DIR_NAME / "readonly"
    d.mkdir(parents=True)
    (d / EXTENSION_ENTRY_FILE).write_text("""
def register(api):
    def on_start(payload, ctx):
        api.setActiveTools(["read"])
    api.on("session_start", on_start)
""", encoding="utf-8")

    from qi_agent.runtime import QiRuntime

    llm = _CapturingLLM()
    runtime = QiRuntime(cwd=project, approve_project=True, llm=llm)
    assert runtime.extensions == ["readonly"]
    session = runtime.sessions.create("t", cwd=project)

    await runtime.start_session(session)
    assert runtime.tool_names() == ["read"]              # 覆盖已生效

    async for _event in runtime.stream("随便说点什么", session):
        pass
    assert llm.seen_tools == ["read"]                    # 真的只给了 read
