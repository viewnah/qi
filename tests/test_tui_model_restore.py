"""TUI 启动时的**设置还原**:footer 显示的模型必须与 runtime 真正在用的那个一致。

覆盖的一条真实回归:`on_mount` 里 `_select_session()` 之后紧跟 `_sync_model_from_runtime()`
(同步),而会话绑定原先只发生在 `_notify_session_start()` 调度的 **async worker** 里
(`start_session` → `bind_session`)。于是续会话时:

  * runtime 用会话里记的 `beta/m3` 发请求,
  * 而 footer 读到 bind **之前**的值 → 显示 settings 默认的 `alpha/m1`。

这类"看得见的与发出去的不一致"最难查(用户看到 A、账单上却是 B),所以锁死它 ——
`_select_session()` 现在会**同步** bind(`_bind_current_session`)。

真行为(还原逻辑本身)由 `tests/test_session_model_entries.py` 覆盖;这里只查 TUI 接线。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qi_agent import paths
from qi_agent import tui as tui_mod
from qi_agent.runtime import QiRuntime
from qi_agent.theme import load_palette

_MODELS = {"providers": {
    "alpha": {"api": "openai-completions", "baseUrl": "http://127.0.0.1:1/v1",
              "apiKey": "k-alpha",
              # reasoning: true —— footer 只在会思考的模型上附 `• <级别>`
              # (见 `_refresh_footer`),不开的话这条断言就只是一句空话。
              "models": [{"id": "m1", "reasoning": True}, {"id": "m2"}]},
    "beta": {"api": "openai-completions", "baseUrl": "http://127.0.0.1:1/v1",
             "apiKey": "k-beta", "models": [{"id": "m3"}]}}}
_SETTINGS = {"defaultProvider": "alpha", "defaultModel": "m1",
             "defaultThinkingLevel": "low"}


def _env(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "models.json").write_text(json.dumps(_MODELS), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps(_SETTINGS), encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(project)
    return project


def _session_with_a_model_switch(project: Path):
    """造一个「起点 alpha/m1、之后切到 beta/m3」的会话(模拟上次退出前的状态)。"""
    seed = QiRuntime(cwd=project, approve_project=True)
    session = seed.sessions.create("t", cwd=project)
    seed.bind_session(session)
    seed.set_model("beta", "m3")
    return session


@pytest.mark.asyncio
async def test_startup_restores_the_model_shown_in_the_footer(tmp_path, monkeypatch):
    """`--session <id>` 续接时,footer 的模型 = 会话里记的那个(不是 settings 默认)。"""
    project = _env(tmp_path, monkeypatch)
    session = _session_with_a_model_switch(project)

    app = tui_mod.QiTui(palette=load_palette("dark"),
                        approve_project=True, session_id=session.id)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        assert (app._model.provider, app._model.model) == ("beta", "m3")
        # footer 是用户唯一能看见的真相:它必须与 runtime 真正在用的那个一致
        assert "beta/m3" in app.footer_text.plain
        assert getattr(app._rt.llm_exec, "spec").model == "m3"


@pytest.mark.asyncio
async def test_startup_restores_the_thinking_level(tmp_path, monkeypatch):
    """思考级别同理:`-c` 续接要把 footer 的级别也对齐到会话里记的那个。"""
    project = _env(tmp_path, monkeypatch)
    seed = QiRuntime(cwd=project, approve_project=True)
    session = seed.sessions.create("t", cwd=project)
    seed.bind_session(session)
    seed.set_thinking_level("high")

    app = tui_mod.QiTui(palette=load_palette("dark"),
                        approve_project=True, session_id=session.id)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        assert app._thinking_level == "high"
        assert "high" in app.footer_text.plain
