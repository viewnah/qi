"""没有 `qi init` 就进 TUI:界面要能起来,并指引 `/login`,而不是“启动失败”。

真实回归:没有默认模型时 `QiRuntime.__init__` 直接抛 `ConfigError`,TUI 的 `on_mount`
把它接成“启动失败”并把 `self._rt` 置空 —— 于是 `/login` 也一起不可用(命令分发要求
runtime 可用),用户被锁在一个既没有模型、也登不了的界面里。

对齐 pi:`interactive` 模式**没有模型也照常启动**,启动时给一句
`Use /login to log into a provider…` 的提示(pi 的 `formatNoModelsAvailableMessage`),
真正提交消息时才报“没有模型”。无头路径仍然照旧报错退出。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qi_agent import paths
from qi_agent import tui as tui_mod
from qi_agent.config import ConfigError
from qi_agent.runtime import QiRuntime
from qi_agent.theme import load_palette

PALETTE = load_palette("dark")
_MODELS = {"providers": {"deepseek": {
    "api": "openai-completions", "baseUrl": "http://127.0.0.1:1/v1",
    "models": [{"id": "deepseek-chat", "contextWindow": 128000}]}}}


def _env(tmp_path: Path, monkeypatch) -> Path:
    """一个 provider 定义 + **空** settings(没有 defaultProvider/defaultModel)。"""
    (tmp_path / "models.json").write_text(json.dumps(_MODELS), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(project)
    return project


def _log_text(app) -> str:
    """把 transcript 里每个组件渲染出来拼成一段文本(找提示用)。"""
    out: list[str] = []
    for widget in app.query_one("#log").children:
        try:
            out.append(str(widget.render()))
        except Exception:  # noqa: BLE001 组件渲染失败不该让这条断言挂掉
            continue
    return "\n".join(out)


@pytest.mark.asyncio
async def test_tui_starts_without_a_default_model_and_says_how_to_login(tmp_path, monkeypatch):
    """缺默认模型不再“启动失败”:runtime 起来、footer 显示 `no-model` / `0/0`、给出指引。"""
    _env(tmp_path, monkeypatch)

    app = tui_mod.QiTui(palette=PALETTE, approve_project=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)

        assert app._rt is not None                    # 关键:没被置空
        assert app._model is None
        assert "启动失败" not in _log_text(app)
        # 指引里必须同时有 `/login` 与终端里的 `qi init`
        assert "login" in _log_text(app)
        assert app._rt.model_fallback_message and "qi init" in app._rt.model_fallback_message
        # footer 的“用量”是 `0/0` 而不是两个问号,模型位是 `no-model`
        stats = app.footer_text.plain.splitlines()[-1]
        assert "0/0" in stats and "?/?" not in stats
        assert "no-model" in stats


@pytest.mark.asyncio
async def test_prompt_without_a_model_reports_instead_of_crashing(tmp_path, monkeypatch):
    """没有模型时提交消息:报一句可照做的错,而不是抛异常/落一条注定 401 的 user。"""
    _env(tmp_path, monkeypatch)

    app = tui_mod.QiTui(palette=PALETTE, approve_project=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        app._submit("你好")
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)

        assert "login" in _log_text(app)
        # 用户消息没落盘 —— 与 pi 一样,没模型时那一轮根本没开始
        entries = app._session.entries if app._session is not None else []
        assert not [e for e in entries if e.get("role") == "user"]


@pytest.mark.asyncio
async def test_login_command_is_usable_without_a_model(tmp_path, monkeypatch):
    """`/login <provider>` 在没有模型时可用,并在登完自动选中该 provider 的模型。"""
    _env(tmp_path, monkeypatch)

    app = tui_mod.QiTui(palette=PALETTE, approve_project=True)

    async def fake_screen(screen):                    # 冒充遮罩输入框
        return "sk-test-123"

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        assert app._rt is not None
        app.await_screen = fake_screen                 # type: ignore[method-assign]
        app._command("/login deepseek")                # 走真正的命令分发,不直调私有流程
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)

        assert app._model is not None
        assert app._model.provider == "deepseek"
        assert "deepseek" in app.footer_text.plain
        assert "no-model" not in app.footer_text.plain


@pytest.mark.asyncio
async def test_login_works_on_a_totally_fresh_install(tmp_path, monkeypatch):
    """连 `models.json` 都没有(只有预置兜底)时,`/login` 也要能登并选中模型。

    这是“没跑过 `qi init`”的最极端形态:provider 全来自 qi 的预置表。
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))            # 没有 settings.json
    monkeypatch.delenv(paths.QI_AGENT_CONFIG, raising=False)      # 也没有 models.json
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(project)

    app = tui_mod.QiTui(palette=PALETTE, approve_project=True)

    async def fake_screen(screen):
        return "sk-test-123"

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        app.await_screen = fake_screen                 # type: ignore[method-assign]
        app._command("/login deepseek")                # deepseek 来自预置表
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)

        assert app._model is not None
        assert app._model.provider == "deepseek"
        assert "deepseek" in app.footer_text.plain


def test_headless_runtime_still_requires_a_default_model(tmp_path, monkeypatch):
    """无头(非交互式)路径不受影响:缺默认模型照旧报错退出。"""
    project = _env(tmp_path, monkeypatch)
    with pytest.raises(ConfigError):
        QiRuntime(cwd=project)                         # has_ui 默认 False
