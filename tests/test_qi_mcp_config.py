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


def test_active_excludes_disabled(tmp_path, monkeypatch):
    """连接/注册只看 `active()`;disabled 的**留在声明表里**(状态面板要能看见它)。"""
    home, project = _env(tmp_path, monkeypatch)
    _write(home / "mcp.json", {"on": {"command": "a"}, "off": {"command": "b", "disabled": True}})

    assert set(resolve(project)) == {"on", "off"}
    assert set(active(project)) == {"on"}


def test_unknown_fields_are_kept_and_reported_not_rejected(tmp_path, monkeypatch):
    """MCP 生态字段还在长(`oauth`/`caFile`/`lifecycle`…)→ 不认识的**留着并提示**,不判死。"""
    home, project = _env(tmp_path, monkeypatch)
    _write(home / "mcp.json", {"s": {"url": "https://x/mcp", "oauth": {"clientId": "c"},
                                     "caFile": "~/certs/ca.pem", "lifecycle": "eager"}})

    spec = resolve(project)["s"]
    assert spec.config["oauth"] == {"clientId": "c"}        # 原样保留
    assert unknown_fields(spec) == ["caFile", "oauth"]      # lifecycle 是认识的
    assert spec.transport == "http"


# ── /mcp 面板(切片 1 唯一的长出来的东西)─────────────────

def test_mcp_command_lists_both_layers_and_flags(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from typing import Any

    from qi_agent.extensions import ExtensionApi, ExtensionBus
    from qi_agent.registry import CommandRegistry, ToolCatalog

    from qi_mcp import register

    home, project = _env(tmp_path, monkeypatch)
    _write(home / "mcp.json", {"gh": {"command": "npx", "args": ["-y", "gh"]}})
    _write(project / ".qi" / "mcp.json",
           {"gh": {"command": "project-gh"}, "off": {"command": "x", "disabled": True}})

    registry = CommandRegistry()
    register(ExtensionApi(catalog=ToolCatalog(), bus=ExtensionBus(),
                          _commands=registry, _name="mcp"))
    command = registry.find("mcp")
    assert command is not None, "`/mcp` 没登记上"

    class _Ui:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def notify(self, message: str, *, level: str = "info") -> None:
            self.messages.append(message)

    ui = _Ui()
    ctx: Any = SimpleNamespace(cwd=project, ui=ui)
    command.handler("", ctx)

    text = "\n".join(ui.messages)
    assert "共 2 个" in text                     # gh(项目覆盖全局)+ off
    assert "gh  (project)" in text               # 来源层标出来
    assert "stdio: project-gh" in text           # 用的是项目那份
    assert "off" in text and "disabled" in text  # 关掉的仍可见
    assert "尚未连接 server" in text             # 别让用户以为已经能用了
