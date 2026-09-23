"""qi-mcp 切片 1:两层声明表(全局 → 项目,项目同名覆盖)。

切片 1 **不碰 MCP 协议** —— 只做"把声明读出来、合并、标出来源",所以这里能完整测它。
协议部分(代理工具 / lazy 连接)在切片 2,那时才需要一个真的或假的 client。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "extensions" / "qi-mcp"))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_mcp import active, load_layers, resolve, scope_files, unknown_fields  # noqa: E402
from qi_mcp.config import ConfigError, ServerSpec, read_file  # noqa: E402


def _env(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """造一个"全局 home + 项目"的环境;返回 (global_home, project_root)。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    return home, project


def _write(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


# ── 两层与覆盖 ──────────────────────────────────────────

def test_project_overrides_global_by_name(tmp_path, monkeypatch):
    """同名时**项目覆盖全局**;被覆盖的那个不出现在结果里(覆盖是合并的规矩)。"""
    home, project = _env(tmp_path, monkeypatch)
    _write(home / "mcp.json", {"github": {"command": "global-gh"}})
    _write(project / ".qi" / "mcp.json",
           {"github": {"command": "project-gh"}, "figma": {"url": "https://f/mcp"}})

    specs = resolve(project)
    assert set(specs) == {"github", "figma"}
    assert specs["github"].config["command"] == "project-gh"
    assert specs["github"].scope == "project"          # 来源层看得到(诊断要用)
    assert specs["figma"].scope == "project"


def test_layers_are_global_then_project_low_to_high(tmp_path, monkeypatch):
    home, project = _env(tmp_path, monkeypatch)
    _write(home / "mcp.json", {"a": {"command": "x"}})

    layers = load_layers(project)
    assert [scope for scope, _p, _s in layers] == ["global", "project"]
    assert layers[0][2] == {"a": {"command": "x"}}
    assert layers[1][2] == {}                          # 项目没配 → 空,不是缺项


def test_missing_files_are_reported_as_empty_not_missing(tmp_path, monkeypatch):
    """**不存在的文件也回三个值** —— 设置页/`/mcp` 要能区分"没这个文件"与"文件在但没写 server"。"""
    home, project = _env(tmp_path, monkeypatch)
    layers = load_layers(project)
    assert len(layers) == 2
    for scope, path, servers in layers:
        assert servers == {}
        assert path.name == "mcp.json"
        assert not path.is_file()
    assert [p for _s, p in scope_files(project)] == [home / "mcp.json",
                                                     project / ".qi" / "mcp.json"]


# ── 坏文件:严格并指到文件 ───────────────────────────────

def test_bad_json_points_at_the_file(tmp_path, monkeypatch):
    home, _project = _env(tmp_path, monkeypatch)
    (home / "mcp.json").write_text("{ 这不是 JSON", encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON 解析失败"):
        read_file(home / "mcp.json")


def test_mcpservers_must_be_an_object(tmp_path, monkeypatch):
    home, _project = _env(tmp_path, monkeypatch)
    (home / "mcp.json").write_text('{"mcpServers": ["nope"]}', encoding="utf-8")
    with pytest.raises(ConfigError, match="mcpServers 必须是对象"):
        read_file(home / "mcp.json")


def test_server_value_must_be_an_object(tmp_path, monkeypatch):
    home, _project = _env(tmp_path, monkeypatch)
    (home / "mcp.json").write_text('{"mcpServers": {"x": "字符串不行"}}', encoding="utf-8")
    with pytest.raises(ConfigError, match="必须是对象"):
        read_file(home / "mcp.json")


def test_missing_file_raises_so_the_caller_decides(tmp_path, monkeypatch):
    """`read_file` 对"文件不存在"也是抛 —— 判"没配"是 `load_layers` 的事,不是它的。"""
    home, _project = _env(tmp_path, monkeypatch)
    with pytest.raises(ConfigError, match="读取失败"):
        read_file(home / "mcp.json")


# ── 字段语义 ────────────────────────────────────────────

def test_transport_and_direct_tools_and_disabled():
    assert ServerSpec("s", {"command": "npx", "args": ["-y", "x"]}).transport == "stdio"
    assert ServerSpec("s", {"url": "https://x/mcp"}).transport == "http"
    assert ServerSpec("s", {"socket": "/tmp/mcp.sock"}).transport == "socket"
    assert ServerSpec("s", {}).transport == "unknown"
    assert "缺 command / url / socket" in ServerSpec("s", {}).summary()
    assert ServerSpec("s", {"command": "npx", "args": ["-y", "x"]}).summary() == "stdio: npx -y x"

    assert ServerSpec("s", {}).direct_tools is False          # 默认走代理(E25)
    assert ServerSpec("s", {"directTools": True}).direct_tools is True
    assert ServerSpec("s", {"directTools": ["a", "b"]}).direct_tools == ["a", "b"]

    assert ServerSpec("s", {"disabled": True}).disabled is True
    assert ServerSpec("s", {"disabled": "true"}).disabled is False   # 只有字面 true(照 pi)


# ── Agent Plugins 1.0 写法:顶层 $schema + 每 server 的 `type` ──────

def test_agent_plugins_type_drives_transport(tmp_path, monkeypatch):
    """`type` 是**传输的首选判定**(Agent Plugins 1.0 里它是必填)。"""
    assert ServerSpec("s", {"type": "stdio", "command": "npx"}).transport == "stdio"
    assert ServerSpec("s", {"type": "streamable-http", "url": "https://x/mcp"}).transport \
        == "http"
    assert ServerSpec("s", {"type": "sse", "url": "https://x/sse"}).transport == "sse"

    # 大小写/空白容错(手写 JSON),但不做同义词猜测
    assert ServerSpec("s", {"type": " Streamable-HTTP ", "url": "https://x/mcp"}) \
        .transport == "http"
    assert ServerSpec("s", {"type": "websocket", "url": "wss://x"}).transport == "websocket"


def test_declared_type_wins_over_field_inference():
    """`type` 在时**不听字段推断** —— 否则一份写错的声明会被猜成能跑的传输。"""
    # 两个字段都在(手写时常见):`type` 说了算
    assert ServerSpec("s", {"type": "stdio", "command": "npx",
                            "url": "https://x/mcp"}).transport == "stdio"
    # 没有 `type` 的老声明仍按字段推(向后兼容,不然旧 mcp.json 全废)
    assert ServerSpec("s", {"command": "npx"}).transport == "stdio"


def test_type_is_known_and_schema_is_ignored(tmp_path, monkeypatch):
    """顶层 `$schema` 被自然忽略(只读 `mcpServers`);`type` **不是**未识别字段。"""
    home, project = _env(tmp_path, monkeypatch)
    (project / ".qi").mkdir(parents=True, exist_ok=True)
    (project / ".qi" / "mcp.json").write_text(json.dumps({
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        "mcpServers": {"docs": {"type": "streamable-http", "url": "https://x/mcp"}},
    }), encoding="utf-8")

    spec = resolve(project)["docs"]
    assert spec.transport == "http"
    assert spec.transport_label == "streamable-http"      # 展示名与配置文件对得上
    assert spec.summary() == "streamable-http: https://x/mcp"
    assert unknown_fields(spec) == []                     # `type` 认得,$schema 不在 server 里


def test_sse_summary_says_unimplemented_not_incomplete():
    """写了 `type: sse` → 说"未实现",不说"声明不完整"(后者会把用户引去查错地方)。"""
    summary = ServerSpec("s", {"type": "sse", "url": "https://x/sse"}).summary()
    assert "sse" in summary and "尚未实现" in summary


def test_active_excludes_disabled(tmp_path, monkeypatch):
    """连接/注册只看 `active()`;disabled 的**留在声明表里**(状态面板要能看见它)。"""
    home, project = _env(tmp_path, monkeypatch)
    _write(home / "mcp.json", {"on": {"command": "a"}, "off": {"command": "b", "disabled": True}})

    assert set(resolve(project)) == {"on", "off"}
    assert set(active(project)) == {"on"}


def test_unknown_fields_are_kept_and_reported_not_rejected(tmp_path, monkeypatch):
    """不是“会的才留”:不认识的字段**原样保留 + 报出来**,不判死(只提示)。

    口径见 `KNOWN_FIELDS`。两种“不认识”都该出现在这份提示里:
    `oauth` / `caFile` = 生态有、我们**不做**;`lifecycle` = **声明了但没实现**。
    它们都意味着“你写了它不会生效”—— 那正是该让用户看到的事。
    """
    home, project = _env(tmp_path, monkeypatch)
    _write(home / "mcp.json", {"s": {"url": "https://x/mcp", "oauth": {"clientId": "c"},
                                     "caFile": "~/certs/ca.pem", "lifecycle": "eager"}})

    spec = resolve(project)["s"]
    assert spec.config["oauth"] == {"clientId": "c"}                 # 原样保留
    assert unknown_fields(spec) == ["caFile", "lifecycle", "oauth"]  # 升序
    assert spec.transport == "http"


def test_fields_the_code_honors_are_not_reported_as_unknown():
    """反向:代码**真在读**的字段不能报成“未识别” —— 那会让用户以为自己写错了。

    回归:`auth` / `bearerToken` / `bearerTokenEnv` / `inheritEnv` / `requestTimeoutMs`
    曾经全在 `KNOWN_FIELDS` 外,于是 bearer 认证与超时正在生效,面板却说“不认识”。
    """
    spec = ServerSpec("s", {"url": "https://x/mcp", "auth": "bearer", "bearerToken": "t",
                            "bearerTokenEnv": "E", "inheritEnv": False,
                            "requestTimeoutMs": 30_000})
    assert unknown_fields(spec) == []


# ── /mcp 面板(切片 1 唯一的长出来的东西)─────────────────

class _Ui:
    """收 `ui.notify` 的**前端替身**(面板的全部输出都走它)。"""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def notify(self, message: str, *, level: str = "info") -> None:
        self.messages.append(message)


def _command(project: Path):
    """把 qi-mcp 装载到一个只含命令登记处的 api,回 `(command, registry, ui)`。

    `ctx` 用**真的** `ExtensionContext` + 真的 `ExtensionUi(frontend=_Ui())`,
    不拿 `SimpleNamespace` 冒充 —— 这样连 "`notify` 没前端就落 `notes`" 那条契约
    也一起走真路径(否则面板输出得去哪儿就是个被测掉的细节)。
    """
    from qi_agent.extensions import ExtensionApi, ExtensionBus, ExtensionContext, ExtensionUi
    from qi_agent.registry import CommandRegistry, ToolCatalog

    from qi_mcp import register

    registry = CommandRegistry()
    register(ExtensionApi(catalog=ToolCatalog(), bus=ExtensionBus(),
                          _commands=registry, _name="mcp"))
    found = registry.find("mcp")
    assert found is not None, "`/mcp` 没登记上"
    ui = _Ui()
    ctx = ExtensionContext(cwd=project, ui=ExtensionUi(frontend=ui))
    return found, registry, ui, ctx


def _ui_text(ui: _Ui) -> str:
    return "\n".join(ui.messages)


@pytest.mark.asyncio
async def test_mcp_command_lists_both_layers_and_flags(tmp_path, monkeypatch):
    home, project = _env(tmp_path, monkeypatch)
    _write(home / "mcp.json", {"gh": {"command": "npx", "args": ["-y", "gh"]}})
    _write(project / ".qi" / "mcp.json",
           {"gh": {"command": "project-gh"}, "off": {"command": "x", "disabled": True}})

    command, registry, ui, ctx = _command(project)
    # 命令名与代理**工具**名都是 `mcp` —— 两者不在同一个命名空间(命令进
    # `CommandRegistry`,工具进 `ToolCatalog`),pi 就是这个形状,不会撞。
    assert registry.find("mcp") is command
    # 上一轮的 `/mcps` 改名已作废:那条名字不该再登记着(否则补全面板会出现两行)
    assert registry.names.count("mcp") == 1
    assert registry.find("mcps") is None, "`/mcps` 是上一轮的写法,不该还在"

    await command.handler("", ctx)

    text = _ui_text(ui)
    assert "共 2 个" in text                     # gh(项目覆盖全局)+ off
    assert "gh  (project)" in text               # 来源层标出来
    assert "stdio: project-gh" in text           # 用的是项目那份
    assert "off" in text and "disabled" in text  # 关掉的仍可见
    # 切片 3 起:代理工具确实注册了、直连在会话开始时按 directTools 注册 —— 面板要说准
    assert "代理工具 `mcp` 已注册" in text
    assert "directTools" in text                 # 让用户知道直连从哪来
    # 声明表**不连 server** → 要指向那条会连的子命令
    assert "/mcp tools" in text


@pytest.mark.asyncio
async def test_mcp_bare_declaration_table_does_no_io(tmp_path, monkeypatch):
    """`/mcp` 是纯读 JSON 的 —— 声明一个**起不来**的 server 也要能立刻回。

    这是把 `tools` 拆成子命令的全部理由:并进默认输出的话,一个坏 server 就会让
    `/mcp` 卡到连接超时（60s）。
    """
    home, project = _env(tmp_path, monkeypatch)
    _write(home / "mcp.json", {"dead": {"command": "/nonexistent/qi-nope"}})

    command, _registry, ui, ctx = _command(project)
    await command.handler("", ctx)

    assert "dead" in _ui_text(ui) and "stdio" in _ui_text(ui)


@pytest.mark.asyncio
async def test_mcp_rejects_unknown_server_name(tmp_path, monkeypatch):
    """`/mcp tools <打错的名字>` → 报错 + 列出可用名,**不默默列全部**。"""
    home, project = _env(tmp_path, monkeypatch)
    _write(project / ".qi" / "mcp.json", {"gh": {"command": "npx"}})

    command, _registry, ui, ctx = _command(project)
    await command.handler("tools githbu", ctx)

    assert "没有叫 `githbu` 的 server" in _ui_text(ui)
    assert "gh" in _ui_text(ui)


@pytest.mark.asyncio
async def test_mcp_tools_without_any_server(tmp_path, monkeypatch):
    """没声明 server 时 `tools` 要指向“怎么配”,而不是空输出。"""
    _home, project = _env(tmp_path, monkeypatch)
    command, _registry, ui, ctx = _command(project)
    await command.handler("tools", ctx)
    assert "没有启用的 MCP server" in _ui_text(ui)


@pytest.mark.asyncio
async def test_mcp_unknown_subcommand_shows_usage(tmp_path, monkeypatch):
    """不认识的子命令 → 用法,而不是默默当“没参数”处理(那会让人以为自己打对了)。"""
    _home, project = _env(tmp_path, monkeypatch)
    command, _registry, ui, ctx = _command(project)
    await command.handler("tols", ctx)

    assert "用法" in _ui_text(ui) and "tools" in _ui_text(ui)


def test_mcp_argument_completions():
    """补全:`/mcp ` 后面只对第一个词给候选(宿主只对第一个参数调补全)。

    这条**不需要环境**:补全回调只看输入前缀,不读配置、不连 server。
    """
    from qi_agent.extensions import ExtensionApi, ExtensionBus
    from qi_agent.registry import CommandRegistry, ToolCatalog

    from qi_mcp import register

    registry = CommandRegistry()
    register(ExtensionApi(catalog=ToolCatalog(), bus=ExtensionBus(),
                          _commands=registry, _name="mcp"))
    found = registry.find("mcp")
    assert found is not None
    complete = found.get_argument_completions
    assert complete is not None
    assert {c["value"] for c in complete("t")} == {"tools"}
    assert {c["value"] for c in complete("")} == {"tools", "help"}
    assert complete("zzz") == []


# ── 闭环守卫:KNOWN_FIELDS 与「代码真的读了什么」必须一致 ──────

def test_known_fields_matches_what_the_code_actually_reads():
    """`KNOWN_FIELDS` 与源码里实际读取的字段**双向**必须相等。

    这条守的是两个都真出过的错(都属静默失效,不报错、只是“不对”):

    * **读了却没进表** → 面板把正在生效的字段报成“未识别字段”
      (`auth` / `bearerToken` / `bearerTokenEnv` / `inheritEnv` / `requestTimeoutMs`);
    * **进了表却没读** → 用户写了以为生效,其实什么都没发生
      (`lifecycle` / `idleTimeout` / `debug`)。

    扫描是**源码级**的:找 `*.config.get("x")` / `config["x"]` / `cfg.get("x")` 这类取值。
    它盯的是“常见写法”,谁要是把字段名写成变量,这条会漏 —— 但那种写法本身就少见,
    而漏掉的后果(少一个告警)比误报轻。`mcpServers` 是**顶层键**、不是 server 字段,排除。
    """
    import ast
    from pathlib import Path

    from qi_mcp.config import KNOWN_FIELDS

    pkg = Path(__file__).resolve().parents[1] / "extensions" / "qi-mcp" / "qi_mcp"
    read: set[str] = set()
    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "get" and node.args:
                base = ast.unparse(node.func.value)
                if base.split(".")[-1] in ("config", "cfg"):
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        read.add(arg.value)
            elif isinstance(node, ast.Subscript):
                if ast.unparse(node.value).split(".")[-1] == "config":
                    sl = node.slice
                    if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                        read.add(sl.value)

    read.discard("mcpServers")            # 顶层键,不是 server 字段
    assert read, "扫描没找到任何字段读取 —— 这条测试本身坏了"
    assert read == set(KNOWN_FIELDS), (
        "KNOWN_FIELDS 与代码实际读取的字段漂了:\n"
        f"  读了但没进表(会被误报“未识别”):{sorted(read - set(KNOWN_FIELDS))}\n"
        f"  进了表却没读(写了不生效)    :{sorted(set(KNOWN_FIELDS) - read)}")


# ── 诊断出口:manager 先被「没有 ui 的那条路」建出来时也要接得上 ──

@pytest.mark.asyncio
async def test_diagnostics_attach_even_if_manager_was_built_without_a_ui(tmp_path, monkeypatch):
    """`on_note` **后补要生效**。

    回归:manager 是"谁先要谁建"的 —— **代理工具**那条路不带 ui,可能先把 manager 建出来;
    此后 `session_start` / `/mcp tools` 带 `notify=True` 再想接上诊断就已经晚了
    (旧代码只在**创建**时绑 `on_note`)。那时连接失败/列工具失败会**静默丢掉** ——
    而那正是用户最需要看到的东西。
    """
    from qi_agent.extensions import ExtensionApi, ExtensionBus, ExtensionContext, ExtensionUi
    from qi_agent.registry import CommandRegistry, ToolCatalog

    from qi_mcp import register

    home, project = _env(tmp_path, monkeypatch)
    _write(project / ".qi" / "mcp.json", {"dead": {"command": "/nonexistent/qi-nope"}})

    catalog, registry = ToolCatalog(), CommandRegistry()
    register(ExtensionApi(catalog=catalog, bus=ExtensionBus(),
                          _commands=registry, _name="mcp"))

    # 1) 代理工具先建 manager（这条 ctx 没有前端 → 那时不接诊断）
    proxy = catalog.get("mcp")
    assert proxy is not None
    # 绑定到局部再调:本仓已知的 semgrep 误报模式(`X.execute(` 被当成动态执行 / SQL sink,
    # 见 tests/test_qi_mcp_proxy.py 里同一处理)。
    run_tool = proxy.execute
    await run_tool({}, ExtensionContext(cwd=project))

    # 2) 面板带 notify=True 进来 → 后补生效,失败要落在这个 ui 上
    ui = _Ui()
    command = registry.find("mcp")
    assert command is not None
    await command.handler("tools", ExtensionContext(cwd=project,
                                                    ui=ExtensionUi(frontend=ui)))
    text = _ui_text(ui)
    # 认「on_note 那条路」的措辞(`MCP server \`dead\` 连接失败:…`);
    # 面板自己那行是 `连接失败的:dead(…)` —— 两者不能混为一谈。
    assert "MCP server `dead` 连接失败" in text, text
