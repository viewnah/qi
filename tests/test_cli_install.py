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


def test_remove_drops_the_declaration_and_hints_uninstall(tmp_path, monkeypatch, pip):
    home = _env(tmp_path, monkeypatch)
    (home / "settings.json").write_text(
        json.dumps({"packages": ["qi-mcp", "qi-agents"]}), encoding="utf-8")
    res = runner.invoke(app, ["remove", "qi-mcp"])
    assert res.exit_code == 0, res.output
    assert _packages(home / "settings.json") == ["qi-agents"]
    assert "uninstall" in res.output and "qi-mcp" in res.output   # 只移除声明;卸包给命令
    assert pip.calls == [], "remove 不调 pip(与 pi 同义)"


def test_remove_unknown_is_a_nonzero_exit(tmp_path, monkeypatch, pip):
    _env(tmp_path, monkeypatch)
    res = runner.invoke(app, ["remove", "nope"])
    assert res.exit_code == 1
    assert "没有" in res.output


def test_uninstall_is_an_alias(tmp_path, monkeypatch, pip):
    home = _env(tmp_path, monkeypatch)
    (home / "settings.json").write_text(json.dumps({"packages": ["qi-mcp"]}), encoding="utf-8")
    assert runner.invoke(app, ["uninstall", "qi-mcp"]).exit_code == 0
    assert _packages(home / "settings.json") == []


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
