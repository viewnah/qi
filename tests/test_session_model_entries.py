"""设置类 entry:`model_change` / `thinking_level_change`(对齐 pi)。

覆盖三件事,它们是一套的(只做一半就等于没做):
  1. **落盘** —— 换模型 / 换级别在会话文件里留一条(回合内、回合外都要);
  2. **还原** —— 续/切会话时按它恢复模型与级别(否则"上次切到的模型"回不来);
  3. **不进上下文** —— `Runtime._history()` 只读 `message`,设置类 entry 不能进。

为什么值得单独一个文件:这三条都是"跨模块的约定"(session ↔ runtime ↔ tui),而
每一条坏掉的症状都很隐蔽 —— 不落盘是"回放里看不出切换点",不还原是"切换重启就丢",
进了上下文是零提示词回归。见 docs/session-format.md §5 / docs/sessions.md §6。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.session import (  # noqa: E402
    MODEL_CHANGE,
    THINKING_LEVEL_CHANGE,
    SessionStore,
    context_settings,
)

_MODELS = json.dumps({
    "providers": {
        "alpha": {"api": "openai-completions", "baseUrl": "http://127.0.0.1:1/v1",
                  "apiKey": "k-alpha", "models": [{"id": "m1"}, {"id": "m2"}]},
        "beta": {"api": "openai-completions", "apiKey": "k-beta",
                 "models": [{"id": "m3"}]},
    },
})
_SETTINGS = '{"defaultProvider": "alpha", "defaultModel": "m1", "defaultThinkingLevel": "low"}'


def _env(tmp_path: Path, monkeypatch, settings: dict | None = None,
         models: dict | None = None) -> None:
    (tmp_path / "models.json").write_text(json.dumps(models or json.loads(_MODELS)),
                                          encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    base = json.loads(_SETTINGS)
    base.update(settings or {})
    (home / "settings.json").write_text(json.dumps(base), encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))


def _runtime(tmp_path, monkeypatch, *, settings: dict | None = None,
             models: dict | None = None):
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    _env(tmp_path, monkeypatch, settings=settings, models=models)
    from qi_agent.runtime import QiRuntime

    return QiRuntime(cwd=project, approve_project=True)


def _entries(session) -> list[dict]:
    return [e for e in session.branch()]


def _kinds(session) -> list[str]:
    return [str(e.get("type")) for e in _entries(session)]


# ── 1. 落盘 ─────────────────────────────────────────────

def test_new_session_records_the_starting_model_and_level(tmp_path, monkeypatch):
    """新会话在 `start_session` 时把**起点**记下来(pi 的 `appendModelChange` 只在
    新会话写;老会话有历史,不该被塞一条"初始值")。"""
    runtime = _runtime(tmp_path, monkeypatch)
    session = runtime.sessions.create("t", cwd=runtime.cwd)

    asyncio.run(runtime.start_session(session))

    assert _kinds(session) == [MODEL_CHANGE, THINKING_LEVEL_CHANGE]
    model = _entries(session)[0]
    assert (model["provider"], model["model_id"]) == ("alpha", "m1")
    assert _entries(session)[1]["thinking_level"] == "low"


def test_set_model_records_even_outside_a_turn(tmp_path, monkeypatch):
    """回合外换模型也要落盘 —— 靠 `bind_session`,而不是"猜当前会话"。

    TUI 的 `/model`、扩展在 handler 里 `api.setModel` 都发生在两次提问之间;
    那时 `_active_session` 是 None(`stream()` 的 `finally` 已经清掉)。
    """
    runtime = _runtime(tmp_path, monkeypatch)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    runtime.bind_session(session)                     # 前端选完会话就绑

    runtime.set_model("beta", "m3", source="set")

    recorded = [e for e in _entries(session) if e.get("type") == MODEL_CHANGE]
    assert recorded[-1]["provider"] == "beta"
    assert recorded[-1]["model_id"] == "m3"


def test_identical_value_is_not_recorded_twice(tmp_path, monkeypatch):
    """同值不重写(pi 的 `isChanging`)。否则每次启动都会给每个会话加两行。"""
    runtime = _runtime(tmp_path, monkeypatch)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    runtime.bind_session(session)

    runtime.set_model("alpha", "m1")                  # 与起点相同
    runtime.set_thinking_level("low")                 # 与起点相同

    assert _kinds(session) == [MODEL_CHANGE, THINKING_LEVEL_CHANGE]


def test_thinking_level_records_only_when_it_changes(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    runtime.bind_session(session)

    runtime.set_thinking_level("high")
    runtime.set_thinking_level("high")                # 第二次不该再写

    levels = [e["thinking_level"] for e in _entries(session)
              if e.get("type") == THINKING_LEVEL_CHANGE]
    assert levels == ["low", "high"]


def test_switching_the_model_also_records_the_adopted_level(tmp_path, monkeypatch):
    """换模型会按 `modelThinkingLevels` 自动改级别 —— 那次改动同样是一条真实切换,
    必须一起落盘(pi 走的是同一个 `setThinkingLevel`)。"""
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    _env(tmp_path, monkeypatch, {"modelThinkingLevels": {"beta/m3": "high"}})
    from qi_agent.runtime import QiRuntime

    runtime = QiRuntime(cwd=project, approve_project=True)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    runtime.bind_session(session)

    runtime.set_model("beta", "m3")

    assert runtime.thinking_level == "high"
    assert [e["thinking_level"] for e in _entries(session)
            if e.get("type") == THINKING_LEVEL_CHANGE] == ["low", "high"]


# ── 2. 还原 ─────────────────────────────────────────────

def test_binding_restores_the_model_recorded_in_the_session(tmp_path, monkeypatch):
    """续会话要恢复**会话里记的**模型,而不是 settings 默认(pi 的 `restoredModel`)。

    这就是"落盘"存在的理由:只写不读等于只对齐了一半。
    """
    runtime = _runtime(tmp_path, monkeypatch)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    runtime.bind_session(session)
    runtime.set_model("beta", "m3")
    assert runtime.llm_exec.spec.model == "m3"

    # 换一个 runtime(模拟重启):settings 默认还是 alpha/m1,但会话里记着 beta/m3
    fresh = _runtime(tmp_path, monkeypatch)
    reopened = fresh.sessions.open_file(session.path)
    fresh.bind_session(reopened)

    assert fresh.llm_exec.spec.provider == "beta"
    assert fresh.llm_exec.spec.model == "m3"


def test_binding_restores_the_thinking_level(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    runtime.bind_session(session)
    runtime.set_thinking_level("high")

    fresh = _runtime(tmp_path, monkeypatch)
    fresh.bind_session(fresh.sessions.open_file(session.path))

    assert fresh.thinking_level == "high"
    assert fresh.llm_exec.thinking_level == "high"        # 也写到 client 上


def test_command_line_model_wins_over_the_recorded_one(tmp_path, monkeypatch):
    """`--model` 是当次的**显式**覆盖,不该被历史里的旧值顶掉。"""
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    _env(tmp_path, monkeypatch)
    from qi_agent.runtime import QiRuntime

    runtime = QiRuntime(cwd=project, approve_project=True)
    session = runtime.sessions.create("t", cwd=project)
    runtime.bind_session(session)
    runtime.set_model("beta", "m3")

    pinned = QiRuntime(cwd=project, approve_project=True, model_override="alpha/m2")
    pinned.bind_session(pinned.sessions.open_file(session.path))

    assert pinned.llm_exec.spec.model == "m2"             # 命令行赢


def test_unresolvable_recorded_model_falls_back_with_a_note(tmp_path, monkeypatch):
    """会话里记的 provider 已经不存在了(或密钥没了):**退回默认 + 记一条 note**。

    静默用默认会让人以为"切换没生效";直接报错会让会话打不开 —— pi 的
    `Could not restore model …` 也是这个取舍。
    """
    runtime = _runtime(tmp_path, monkeypatch)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    runtime.bind_session(session)
    runtime.set_model("beta", "m3")

    # 重开一个 runtime,但把 beta 从 models.json 里删掉
    alpha_only = {"providers": {"alpha": {"api": "openai-completions",
                                          "baseUrl": "http://127.0.0.1:1/v1",
                                          "apiKey": "k-alpha",
                                          "models": [{"id": "m1"}, {"id": "m2"}]}}}
    fresh = _runtime(tmp_path, monkeypatch, models=alpha_only)
    fresh.bind_session(fresh.sessions.open_file(session.path))

    assert fresh.llm_exec.spec.model == "m1"              # 退回 settings 默认
    assert any("beta/m3" in note for note in fresh.notes)


def test_recorded_model_without_credentials_falls_back(tmp_path, monkeypatch):
    """provider 还在,但**密钥没了**(退出登录了):也不该拿着它去发一次注定 401 的请求。

    pi 在还原前会 `hasConfiguredAuth(model.provider)` 先问一句(`restoredModel` 分支)。
    """
    runtime = _runtime(tmp_path, monkeypatch)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    runtime.bind_session(session)
    runtime.set_model("beta", "m3")

    # beta 的 apiKey 引用指向一个不存在的环境变量 → `resolve_key` 判为未解析
    models = {"providers": {
        "alpha": {"api": "openai-completions", "baseUrl": "http://127.0.0.1:1/v1",
                  "apiKey": "k-alpha", "models": [{"id": "m1"}]},
        "beta": {"api": "openai-completions", "apiKey": "$QI_TEST_MISSING_KEY",
                 "models": [{"id": "m3"}]}}}
    monkeypatch.delenv("QI_TEST_MISSING_KEY", raising=False)
    fresh = _runtime(tmp_path, monkeypatch, models=models)
    fresh.bind_session(fresh.sessions.open_file(session.path))

    assert fresh.llm_exec.spec.model == "m1"
    assert any("beta/m3" in note for note in fresh.notes)


def test_rebinding_the_same_session_does_not_undo_a_later_switch(tmp_path, monkeypatch):
    """反复绑定同一会话不该把用户刚切的模型顶回去(同级重绑是常态:切会话、重载…)。"""
    runtime = _runtime(tmp_path, monkeypatch)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    runtime.bind_session(session)
    runtime.bind_session(session)                     # 多绑几次
    runtime.set_model("beta", "m3")
    runtime.bind_session(session)                     # 切回来

    assert runtime.llm_exec.spec.model == "m3"


def test_empty_session_gets_the_starting_entries_on_bind(tmp_path, monkeypatch):
    """绑定一个**没有任何 entry** 的会话(刚 `create` 出来):补上起点两条。"""
    runtime = _runtime(tmp_path, monkeypatch)
    session = runtime.sessions.create("t", cwd=runtime.cwd)

    runtime.bind_session(session)

    assert _kinds(session) == [MODEL_CHANGE, THINKING_LEVEL_CHANGE]


def test_legacy_session_without_a_level_entry_gets_one(tmp_path, monkeypatch):
    """本功能上线前建的老会话:没有级别 entry → 补一条当前值,而不是每次都被当"没记过"。"""
    runtime = _runtime(tmp_path, monkeypatch)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    runtime.sessions.append(session, {"type": "message", "role": "user",
                                      "content": "老会话", "agent_id": "qi"})

    runtime.bind_session(session)

    assert THINKING_LEVEL_CHANGE in _kinds(session)
    assert [e for e in _entries(session) if e.get("type") == THINKING_LEVEL_CHANGE][-1][
        "thinking_level"] == "low"


# ── 3. 不进上下文 ────────────────────────────────────────

def test_setting_entries_never_reach_the_llm_context(tmp_path, monkeypatch):
    """设置类 entry 只写不读**上下文**:`_history()` 只认 `message`。

    这条是安全性所在:让它们进上下文就等于每轮往提示词里灌"模型: x/y"(零提示词回归)。
    """
    runtime = _runtime(tmp_path, monkeypatch)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    runtime.bind_session(session)
    runtime.set_model("beta", "m3")
    runtime.set_thinking_level("high")
    runtime.sessions.append(session, {"type": "message", "role": "user",
                                      "content": "你好", "agent_id": "qi"})

    history = runtime._history(session)

    assert [m.role for m in history] == ["user"]
    assert history[0].content == "你好"


def test_context_settings_takes_the_last_value_and_survives_junk():
    """`context_settings` 取**最后一条**(后者胜),脏数据当没有 —— 一行坏数据不该让会话打不开。"""
    branch = [
        {"type": MODEL_CHANGE, "provider": "alpha", "model_id": "m1"},
        {"type": THINKING_LEVEL_CHANGE, "thinking_level": "low"},
        {"type": "message", "role": "user", "content": "x"},
        {"type": MODEL_CHANGE, "provider": "", "model_id": ""},      # 坏值:不算数
        {"type": THINKING_LEVEL_CHANGE},                              # 缺键:不算数
        {"type": MODEL_CHANGE, "provider": "beta", "model_id": "m3"},
    ]

    settings = context_settings(branch)

    assert settings["model"] == ("beta", "m3")
    assert settings["thinking_level"] == "low"            # 坏的没顶掉好的


def test_context_settings_on_an_empty_branch():
    assert context_settings([]) == {"model": None, "thinking_level": None}


# ── 4. 环境变量:agent 自查当前模型的唯一通道 ──────────────

def test_tool_ctx_carries_the_session_environment(tmp_path, monkeypatch):
    """bash/powershell 的 `ToolContext` 要带上会话环境变量(pi 的
    `exposeSessionEnvironment`):换模型是界面状态、不进上下文,所以这是 agent
    唯一能**自证**"现在跑的是什么"的通道。"""
    runtime = _runtime(tmp_path, monkeypatch)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    runtime.bind_session(session)
    runtime.set_model("beta", "m3")
    runtime.set_thinking_level("high")

    env = runtime._tool_ctx("qi").session_env

    assert env["QI_PROVIDER"] == "beta"
    assert env["QI_MODEL"] == "m3"
    assert env["QI_REASONING_LEVEL"] == "high"
    assert env["QI_SESSION_ID"] == session.id
    assert env["QI_SESSION_FILE"] == str(session.path)


def test_child_run_env_follows_the_childs_own_model(tmp_path, monkeypatch):
    """子运行(`runAgent` 给了 `model`)的环境变量要写**子运行的**模型:照抄父的话,
    子 agent 自查会得到"我在用别的模型"这种假答案。"""
    runtime = _runtime(tmp_path, monkeypatch)
    session = runtime.sessions.create("t", cwd=runtime.cwd)
    runtime.bind_session(session)

    env = runtime._child_session_env("beta/m3")

    assert (env["QI_PROVIDER"], env["QI_MODEL"]) == ("beta", "m3")
    assert env["QI_SESSION_ID"] == session.id          # 会话照旧继承


# ── 5. store 层的约定 ────────────────────────────────────

def test_store_rejects_an_unknown_setting_kind(tmp_path):
    """`kind` 只认这两类:静默落一个客户端不认识的类型比报错更难查。"""
    store = SessionStore(root=tmp_path / "sessions")
    session = store.create("t", cwd=tmp_path)

    assert store.set_context_setting(session, "not_a_kind", {}) is False
    assert session.tree_entries == []


def test_store_writes_a_new_node_with_parent_id(tmp_path):
    """走的是 `append`:新节点挂在当前节点下(树语义与其它 entry 一致)。"""
    store = SessionStore(root=tmp_path / "sessions")
    session = store.create("t", cwd=tmp_path)
    store.append(session, {"type": "message", "role": "user", "content": "x"})

    assert store.set_context_setting(session, MODEL_CHANGE,
                                     {"provider": "alpha", "model_id": "m1"}) is True

    entry = session.tree_entries[-1]
    assert entry["type"] == MODEL_CHANGE
    assert entry["parentId"] == session.tree_entries[-2]["id"]
    assert entry["id"]
    # 落盘的与内存里的一致(不是只改了内存)
    on_disk = [json.loads(x) for x in session.path.read_text(encoding="utf-8").splitlines()]
    assert on_disk[-1]["model_id"] == "m1"
