"""`qi install` / `remove` / `uninstall` / `update`(对齐 pi;判决 C 已撤销)。

pi 的语义:
  - `install <source> [-l]`:装 **并**写进设置;
  - `remove` / `uninstall <source> [-l]`:**只**从设置里移除(pi 不替你卸包);
  - `update [source|self|pi] [--self|--extensions|--models|--all|--extension X|--force]`,
    无目标时**只更新自己**。

三条不变量:

1. **先装后记** —— pip 失败就不改声明(否则 `qi doctor` 会报告一条"声明了但没装",
   而那是命令自己刚制造出来的);
2. **本地目录不调 pip**(目录通道的意义就是"不装也能用");
3. 认不出的来源**报错并给出写法**,不静默建条假声明。

全部用假的 `_run_pip`:测试里绝不真跑 pip。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from qi_agent import cli as cli_mod  # noqa: E402
from qi_agent import paths  # noqa: E402
from qi_agent.cli import app  # noqa: E402

runner = CliRunner()


def _env(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        '{"defaultProvider": "ollama", "defaultModel": "x"}', encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir(exist_ok=True)          # 项目级设置的根
    return home


def _packages(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return list(json.loads(path.read_text(encoding="utf-8")).get("packages") or [])


@pytest.fixture
def pip(monkeypatch):
    """记录 pip 调用,**不真跑**;默认成功。"""
    calls: list[list[str]] = []
    codes: list[int] = []

    def fake(args: list[str]) -> int:
        calls.append(list(args))
        return codes.pop(0) if codes else 0

    monkeypatch.setattr(cli_mod, "_run_pip", fake)
    fake.calls = calls          # type: ignore[attr-defined]
    fake.codes = codes          # type: ignore[attr-defined]
    return fake


def _local_ext(tmp_path: Path, name: str = "my-ext") -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "extension.py").write_text("def register(api):\n    pass\n", encoding="utf-8")
    return d


# ── install ────────────────────────────────────────────────────────────


def test_install_pip_package_records_it(tmp_path, monkeypatch, pip):
    home = _env(tmp_path, monkeypatch)
    res = runner.invoke(app, ["install", "qi-mcp"])
    assert res.exit_code == 0, res.output
    assert pip.calls == [["install", "qi-mcp"]]
    assert _packages(home / "settings.json") == ["qi-mcp"]


def test_install_accepts_a_requirement_with_version(tmp_path, monkeypatch, pip):
    home = _env(tmp_path, monkeypatch)
    assert runner.invoke(app, ["install", "pip:qi-mcp>=0.2"]).exit_code == 0
    assert pip.calls == [["install", "qi-mcp>=0.2"]]
    assert _packages(home / "settings.json") == ["pip:qi-mcp>=0.2"]


def test_install_local_directory_does_not_touch_pip(tmp_path, monkeypatch, pip):
    """目录通道的意义就是"不装也能用" —— 所以本地目录不调 pip。"""
    home = _env(tmp_path, monkeypatch)
    ext = _local_ext(tmp_path)
    res = runner.invoke(app, ["install", str(ext)])
    assert res.exit_code == 0, res.output
    assert pip.calls == [], "本地目录不该调 pip"
    assert _packages(home / "settings.json") == [f"local:{ext.resolve()}"]


def test_install_local_archive_goes_through_pip(tmp_path, monkeypatch, pip):
    """本地归档(.whl / .tar.gz)走 pip —— 它没有 extension.py,不是目录通道。"""
    home = _env(tmp_path, monkeypatch)
    archive = tmp_path / "qi_mcp-0.1.1.tar.gz"
    archive.write_bytes(b"not a real sdist")
    res = runner.invoke(app, ["install", str(archive)])
    assert res.exit_code == 0, res.output
    assert pip.calls == [["install", str(archive)]]
    assert _packages(home / "settings.json") == [str(archive)]


def test_install_local_directory_without_entry_is_rejected(tmp_path, monkeypatch, pip):
    _env(tmp_path, monkeypatch)
    empty = tmp_path / "empty"
    empty.mkdir()
    res = runner.invoke(app, ["install", str(empty)])
    assert res.exit_code == 2
    assert "extension.py" in res.output


def test_install_local_writes_project_settings(tmp_path, monkeypatch, pip):
    _env(tmp_path, monkeypatch)
    ext = _local_ext(tmp_path)
    assert runner.invoke(app, ["install", "--local", str(ext)]).exit_code == 0
    assert _packages(tmp_path / ".qi" / "settings.json") == [f"local:{ext.resolve()}"]


def test_install_dedupes_by_normalized_name(tmp_path, monkeypatch, pip):
    """同名的旧声明要先去掉 —— 否则列表里会同时躺着 `Qi.MCP` 与 `qi-mcp`。"""
    home = _env(tmp_path, monkeypatch)
    (home / "settings.json").write_text(
        json.dumps({"packages": ["Qi.MCP"]}), encoding="utf-8")
    assert runner.invoke(app, ["install", "qi-mcp"]).exit_code == 0
    assert _packages(home / "settings.json") == ["qi-mcp"]


def test_install_unrecognized_source_is_reported(tmp_path, monkeypatch, pip):
    home = _env(tmp_path, monkeypatch)
    res = runner.invoke(app, ["install", "git+https://host/repo"])
    assert res.exit_code == 2
    assert "名字 @ URL" in res.output
    assert pip.calls == []
    assert _packages(home / "settings.json") == [], "认不出就不该写声明"


def test_install_does_not_record_when_pip_fails(tmp_path, monkeypatch, pip):
    """**先装后记**:装失败就不改声明,否则 doctor 会报一条自己刚制造的偏差。"""
    home = _env(tmp_path, monkeypatch)
    pip.codes.append(1)
    res = runner.invoke(app, ["install", "qi-mcp"])
    assert res.exit_code == 1
    assert _packages(home / "settings.json") == []


# ── remove / uninstall ─────────────────────────────────────────────────


def test_remove_drops_declaration_and_uninstalls(tmp_path, monkeypatch, pip):
    """移除声明 = **真卸包**(对齐 pi);不再只打印 pip uninstall 命令。"""
    home = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_mod, "_installed_dist_version", lambda name: "1.0")
    (home / "settings.json").write_text(
        json.dumps({"packages": ["qi-mcp", "qi-agents"]}), encoding="utf-8")
    res = runner.invoke(app, ["remove", "qi-mcp"])
    assert res.exit_code == 0, res.output
    assert _packages(home / "settings.json") == ["qi-agents"]
    assert pip.calls == [["uninstall", "-y", "qi-mcp"]]
    assert "已卸载" in res.output


def test_remove_keeps_package_when_another_scope_declares_it(tmp_path, monkeypatch, pip):
    """pip 只有一个环境:另一作用域还声明着,就只删声明、留包。"""
    home = _env(tmp_path, monkeypatch)
    (home / "settings.json").write_text(json.dumps({"packages": ["qi-mcp"]}), encoding="utf-8")
    (tmp_path / ".qi").mkdir(exist_ok=True)
    (tmp_path / ".qi" / "settings.json").write_text(
        json.dumps({"packages": ["qi-mcp"]}), encoding="utf-8")
    res = runner.invoke(app, ["remove", "qi-mcp"])
    assert res.exit_code == 0, res.output
    assert _packages(home / "settings.json") == []
    assert pip.calls == [], "project 还声明着,不该卸包"
    assert "仍被 project" in res.output


def test_remove_force_uninstalls_despite_other_scope(tmp_path, monkeypatch, pip):
    home = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_mod, "_installed_dist_version", lambda name: "1.0")
    (home / "settings.json").write_text(json.dumps({"packages": ["qi-mcp"]}), encoding="utf-8")
    (tmp_path / ".qi").mkdir(exist_ok=True)
    (tmp_path / ".qi" / "settings.json").write_text(
        json.dumps({"packages": ["qi-mcp"]}), encoding="utf-8")
    res = runner.invoke(app, ["remove", "qi-mcp", "--force"])
    assert res.exit_code == 0, res.output
    assert pip.calls == [["uninstall", "-y", "qi-mcp"]]


def test_remove_project_scope(tmp_path, monkeypatch, pip):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_mod, "_installed_dist_version", lambda name: "1.0")
    (tmp_path / ".qi").mkdir(exist_ok=True)
    (tmp_path / ".qi" / "settings.json").write_text(
        json.dumps({"packages": ["qi-mcp"]}), encoding="utf-8")
    res = runner.invoke(app, ["remove", "-l", "qi-mcp"])
    assert res.exit_code == 0, res.output
    assert _packages(tmp_path / ".qi" / "settings.json") == []
    assert pip.calls == [["uninstall", "-y", "qi-mcp"]]


def test_remove_unknown_is_a_nonzero_exit(tmp_path, monkeypatch, pip):
    _env(tmp_path, monkeypatch)
    res = runner.invoke(app, ["remove", "nope"])
    assert res.exit_code == 1
    assert "没有" in res.output


def test_uninstall_is_an_alias(tmp_path, monkeypatch, pip):
    home = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_mod, "_installed_dist_version", lambda name: "1.0")
    (home / "settings.json").write_text(json.dumps({"packages": ["qi-mcp"]}), encoding="utf-8")
    assert runner.invoke(app, ["uninstall", "qi-mcp"]).exit_code == 0
    assert _packages(home / "settings.json") == []
    assert pip.calls == [["uninstall", "-y", "qi-mcp"]]


# ── update ─────────────────────────────────────────────────────────────


def test_update_without_target_updates_qi_itself(tmp_path, monkeypatch, pip):
    """pi 的默认:没有目标时只更新自己。"""
    _env(tmp_path, monkeypatch)
    res = runner.invoke(app, ["update"])
    assert res.exit_code == 0, res.output
    assert pip.calls == [["install", "--upgrade", "qi-coding-agent"]]


def test_update_self_and_all(tmp_path, monkeypatch, pip):
    _env(tmp_path, monkeypatch)
    runner.invoke(app, ["update", "self"])
    assert pip.calls == [["install", "--upgrade", "qi-coding-agent"]]


def test_update_extensions_upgrades_declared_pip_packages(tmp_path, monkeypatch, pip):
    home = _env(tmp_path, monkeypatch)
    (home / "settings.json").write_text(
        json.dumps({"packages": ["qi-mcp", "local:/tmp/somewhere"]}), encoding="utf-8")
    res = runner.invoke(app, ["update", "--extensions"])
    assert res.exit_code == 0, res.output
    assert pip.calls == [["install", "--upgrade", "qi-mcp"]], "目录通道不该进 pip"


def test_update_one_extension(tmp_path, monkeypatch, pip):
    home = _env(tmp_path, monkeypatch)
    (home / "settings.json").write_text(
        json.dumps({"packages": ["qi-mcp", "qi-agents"]}), encoding="utf-8")
    res = runner.invoke(app, ["update", "--extension", "qi-agents"])
    assert res.exit_code == 0, res.output
    assert pip.calls == [["install", "--upgrade", "qi-agents"]]


def test_update_unknown_extension_is_reported(tmp_path, monkeypatch, pip):
    _env(tmp_path, monkeypatch)
    res = runner.invoke(app, ["update", "--extension", "nope"])
    assert res.exit_code == 1
    assert pip.calls == []


def test_update_force_passes_force_reinstall(tmp_path, monkeypatch, pip):
    _env(tmp_path, monkeypatch)
    runner.invoke(app, ["update", "--self", "--force"])
    assert pip.calls == [["install", "--upgrade", "--force-reinstall", "qi-coding-agent"]]


def test_update_models_says_qi_has_no_catalogs(tmp_path, monkeypatch, pip):
    """qi 的模型全在 models.json(自己维护),没有"远端目录"可取 —— 说清楚,不假装做了。"""
    _env(tmp_path, monkeypatch)
    res = runner.invoke(app, ["update", "--models"])
    assert res.exit_code == 0, res.output
    assert "模型目录" in res.output
    assert pip.calls == []


def test_update_with_no_direction_is_an_error(tmp_path, monkeypatch, pip):
    _env(tmp_path, monkeypatch)
    res = runner.invoke(app, ["update", "--extensions", "--force"])
    assert res.exit_code == 0, res.output          # --extensions 给了方向 → 正常


# ── 安装器选择:uv tool 环境没有 pip ───────────────────────────────────
#
# 目标位置始终是 `sys.executable`(qi 自己的解释器),这里只锁"用哪个执行器":
# uv tool / 无 pip 的环境走 `uv pip <sub> --python <sys.executable>` —— 目标不变,
# 只是不需要 pip。非 uv 环境维持 `python -m pip`。


def _fake_uv(monkeypatch, path: str = "/usr/bin/uv") -> None:
    monkeypatch.setattr(cli_mod.shutil, "which",
                        lambda name: path if name == "uv" else None)


def test_in_uv_tool_env_detects_the_receipt(tmp_path, monkeypatch):
    root = tmp_path / "tools" / "qi-coding-agent"
    root.mkdir(parents=True)
    monkeypatch.setattr(cli_mod.sys, "prefix", str(root))
    assert not cli_mod._in_uv_tool_env()
    (root / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")
    assert cli_mod._in_uv_tool_env()


def test_installer_cmd_uses_uv_in_a_uv_tool_env(tmp_path, monkeypatch):
    root = tmp_path / "tools" / "qi-coding-agent"
    root.mkdir(parents=True)
    (root / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")
    py = root / "bin" / "python"
    monkeypatch.setattr(cli_mod.sys, "prefix", str(root))
    monkeypatch.setattr(cli_mod.sys, "executable", str(py))
    _fake_uv(monkeypatch)

    cmd = cli_mod._installer_cmd(["install", "qi-mcp"])
    assert cmd == ["/usr/bin/uv", "pip", "install", "--python", str(py), "qi-mcp"]


def test_installer_cmd_falls_back_to_uv_when_pip_is_missing(monkeypatch):
    """不是 uv tool,但这个解释器里没有 pip —— 也走 uv(否则只能失败)。"""
    monkeypatch.setattr(cli_mod, "_in_uv_tool_env", lambda: False)
    monkeypatch.setattr(cli_mod, "_has_pip", lambda: False)
    _fake_uv(monkeypatch)
    cmd = cli_mod._installer_cmd(["install", "x"])
    assert cmd[:3] == ["/usr/bin/uv", "pip", "install"]


def test_installer_cmd_keeps_pip_when_it_is_available(monkeypatch):
    monkeypatch.setattr(cli_mod, "_in_uv_tool_env", lambda: False)
    monkeypatch.setattr(cli_mod, "_has_pip", lambda: True)
    _fake_uv(monkeypatch)
    assert cli_mod._installer_cmd(["install", "x"]) is None


def test_run_pip_actually_calls_uv_when_selected(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(cli_mod, "_installer_cmd", lambda args: ["/usr/bin/uv", "pip", *args])
    monkeypatch.setattr("subprocess.call", lambda cmd: seen.append(list(cmd)) or 0)
    assert cli_mod._run_pip(["install", "qi-mcp"]) == 0
    assert seen == [["/usr/bin/uv", "pip", "install", "qi-mcp"]]


def test_install_hints_durable_command_in_uv_tool_env(tmp_path, monkeypatch, pip):
    """uv tool 里装完要提醒 `--with` 那条 —— pip/uv 就地装的会被环境重建抹掉。"""
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_mod, "_in_uv_tool_env", lambda: True)
    res = runner.invoke(app, ["install", "qi-mcp"])
    assert res.exit_code == 0, res.output
    assert "uv tool install qi-coding-agent --with" in res.output


# ── sync:按声明对账(qi 版 `uv sync`,只补不删)──────────────────────


def _env_pkgs(tmp_path, monkeypatch, packages: list[str]) -> Path:
    home = _env(tmp_path, monkeypatch)
    (home / "settings.json").write_text(
        json.dumps({"defaultProvider": "ollama", "defaultModel": "x",
                    "packages": packages}), encoding="utf-8")
    return home


def test_sync_installs_missing_declared_packages(tmp_path, monkeypatch, pip):
    _env_pkgs(tmp_path, monkeypatch, ["qi-mcp"])
    res = runner.invoke(app, ["sync"])
    assert res.exit_code == 0, res.output
    assert pip.calls == [["install", "qi-mcp"]], "缺失的声明要被补装"
    assert "已补装" in res.output


def test_sync_keeps_the_declaration_untouched(tmp_path, monkeypatch, pip):
    """sync 只装包,不该改 settings.packages(写声明是 `qi install` 的事)。"""
    home = _env_pkgs(tmp_path, monkeypatch, ["pip:qi-mcp>=0.2"])
    assert runner.invoke(app, ["sync"]).exit_code == 0
    assert pip.calls == [["install", "qi-mcp>=0.2"]]
    assert _packages(home / "settings.json") == ["pip:qi-mcp>=0.2"]


def test_sync_without_declarations_is_a_noop(tmp_path, monkeypatch, pip):
    _env(tmp_path, monkeypatch)
    res = runner.invoke(app, ["sync"])
    assert res.exit_code == 0, res.output
    assert pip.calls == []
    assert "没有声明" in res.output


def test_sync_dry_run_does_not_install(tmp_path, monkeypatch, pip):
    _env_pkgs(tmp_path, monkeypatch, ["qi-mcp"])
    res = runner.invoke(app, ["sync", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert pip.calls == [], "--dry-run 只说不做"
    assert "会补装" in res.output


def test_sync_failure_is_a_nonzero_exit(tmp_path, monkeypatch, pip):
    _env_pkgs(tmp_path, monkeypatch, ["qi-mcp"])
    pip.codes.append(1)
    res = runner.invoke(app, ["sync"])
    assert res.exit_code == 1
    assert "同步未完成" in res.output


def test_sync_reports_local_declaration_without_extension(tmp_path, monkeypatch, pip):
    _env_pkgs(tmp_path, monkeypatch, [f"local:{tmp_path / 'nope'}"])
    res = runner.invoke(app, ["sync"])
    assert res.exit_code == 1
    assert "找不到" in res.output
    assert pip.calls == [], "目录通道没有'安装'这回事"


def test_sync_reports_unparsable_declaration(tmp_path, monkeypatch, pip):
    _env_pkgs(tmp_path, monkeypatch, ["git+https://host/repo"])
    res = runner.invoke(app, ["sync"])
    assert res.exit_code == 0, res.output
    assert "无法解析" in res.output
    assert pip.calls == []


def test_installer_cmd_strips_yes_for_uv_uninstall(tmp_path, monkeypatch):
    """`uv pip uninstall` 不问也不收 `-y` —— 转成 uv 时要剥掉(pip 那条要留着)。"""
    root = tmp_path / "tools" / "qi-coding-agent"
    root.mkdir(parents=True)
    (root / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")
    monkeypatch.setattr(cli_mod.sys, "prefix", str(root))
    monkeypatch.setattr(cli_mod.sys, "executable", str(root / "bin" / "python"))
    _fake_uv(monkeypatch)
    cmd = cli_mod._installer_cmd(["uninstall", "-y", "qi-mcp"])
    assert cmd == ["/usr/bin/uv", "pip", "uninstall", "--python",
                   str(root / "bin" / "python"), "qi-mcp"]
