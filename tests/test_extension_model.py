"""P-E3d-1:模型 / 思考级别 / 会话名的**运行时行为**(真 `QiRuntime`)。

为什么单独一个文件:这三件事在 P-E3d 从 TUI **搬到了 runtime** —— 事件必须从状态所在的
那一处发,否则两个入口(TUI 的 `/model` 与扩展的 `api.setModel`)迟早一个漏发、一个重发。

而 TUI 测试里那些运行时是**替身**,断言替身的行为等于没断言 qi。所以:
* `tests/test_tui_style.py`:断言 TUI 用**对的参数与来源**调 runtime(它自己的职责);
* 本文件:断言 runtime **真的**换了客户端、发了事件(它的职责)。

三个通知型事件(`model_select` / `thinking_level_select` / `session_info_changed`)走
`_emit_notice` → 后台任务,所以测试里要给事件循环一次机会(`await asyncio.sleep(0)`)。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent import paths  # noqa: E402
from qi_agent.llm import LiteLLMClient, ThinkingLLMClient  # noqa: E402
from qi_agent.registry import EXTENSION_ENTRY_FILE  # noqa: E402

_MODELS = json.dumps({
    "providers": {
        "alpha": {"api": "openai-completions", "baseUrl": "http://127.0.0.1:1/v1",
                  "models": [{"id": "m1"}, {"id": "m2"}]},
        "beta": {"api": "openai-completions", "models": [{"id": "m3"}]},
    },
})
_SETTINGS = '{"defaultProvider": "alpha", "defaultModel": "m1", "defaultThinkingLevel": "low",' \
            ' "retry": {"provider": {"timeoutMs": 1234, "maxRetries": 2}}}'


def _env(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.delenv("QI_THEME", raising=False)


def _install(tmp_path: Path, name: str, body: str) -> None:
    d = tmp_path / "proj" / ".qi" / paths.EXTENSIONS_DIR_NAME / name
    d.mkdir(parents=True, exist_ok=True)
    (d / EXTENSION_ENTRY_FILE).write_text(body, encoding="utf-8")


#: 把三个事件原样记到文件里(payload 全收,便于断言 source / previous)
_RECORDER = """
import json
from pathlib import Path

LOG = Path({log!r})


def register(api):
    def note(kind):
        def handler(payload, ctx):
            with LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps({{"kind": kind, "payload": payload}},
                                   ensure_ascii=False) + "\\n")
        return handler
    api.on("model_select", note("model"))
    api.on("thinking_level_select", note("thinking"))
    api.on("session_info_changed", note("title"))
"""


def _runtime(tmp_path, monkeypatch, log: Path | None = None):
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    _env(tmp_path, monkeypatch)
    if log is not None:
        _install(tmp_path, "recorder", _RECORDER.format(log=str(log)))
    from qi_agent.runtime import QiRuntime

    return QiRuntime(cwd=project, approve_project=True)


def _events(log: Path | None) -> list[dict]:
    if log is None or not log.exists():
        return []
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]


def _of(events: list[dict], kind: str) -> list[dict]:
    return [e["payload"] for e in events if e["kind"] == kind]


# ── set_model ───────────────────────────────────────────

#: 本文件里 `LiteLLMClient` 构造函数收到的参数(autouse fixture 清空)。
#: 断言“切换时带了什么”比去翻客户端的私有字段(`_provider_retry` 是转换后的形状)稳得多。
BUILT: list[dict] = []


@pytest.fixture(autouse=True)
def _clear_built():
    BUILT.clear()
    yield
    BUILT.clear()


class _SpyClient(LiteLLMClient):
    """记账 + 委托真类:换模型时到底把哪些参数传下去了,一看就知道。"""

    def __init__(self, spec, auth_store=None, thinking_level="off", retry=None, **_ignored: object):
        BUILT.append({"model": spec.model, "thinking_level": thinking_level, "retry": retry})
        super().__init__(spec, auth_store, thinking_level=thinking_level, retry=retry)


@pytest.mark.asyncio
async def test_set_model_replaces_the_client_and_keeps_its_settings(tmp_path, monkeypatch):
    """换模型要重建客户端,而 `thinking_level` 与 `retry` **必须带过去** ——
    否则切模型会默默丢掉它们(footer 显示 high、请求里却没有)。"""
    from qi_agent import runtime as runtime_mod

    log = tmp_path / "events.jsonl"
    runtime = _runtime(tmp_path, monkeypatch, log)
    monkeypatch.setattr(runtime_mod, "LiteLLMClient", _SpyClient)

    resolved = runtime.set_model("beta", "m3", source="set")
    assert (resolved.provider, resolved.model) == ("beta", "m3")
    client = runtime.llm_exec
    assert isinstance(client, _SpyClient)                 # 真的换到运行期
    assert client.spec.model == "m3"
    assert client.thinking_level == "low"                 # 来自 settings,没被丢掉
    assert BUILT[-1] == {"model": "m3", "thinking_level": "low",
                         "retry": {"provider": {"timeoutMs": 1234, "maxRetries": 2}}}

    await asyncio.sleep(0)                               # 通知型事件走后台任务
    payloads = _of(_events(log), "model")
    assert len(payloads) == 1
    assert payloads[0]["model"] == "beta/m3"
    assert payloads[0]["previous"] == "alpha/m1"
    assert payloads[0]["source"] == "set"
    assert payloads[0]["session"] is None                # 回合外 → 没有会话


@pytest.mark.asyncio
async def test_set_model_reports_the_source(tmp_path, monkeypatch):
    """来源进 payload:UI 显式切是 `set`,Ctrl+P 轮转是 `cycle` —— 扩展要能区分。"""
    log = tmp_path / "events.jsonl"
    runtime = _runtime(tmp_path, monkeypatch, log)
    runtime.set_model("beta", "m3", source="cycle")
    await asyncio.sleep(0)
    assert _of(_events(log), "model")[0]["source"] == "cycle"


@pytest.mark.asyncio
async def test_set_model_accepts_a_model_not_listed_in_models_json(tmp_path, monkeypatch):
    """未列在 `models.json` 里的模型 id **也能用** —— 这是刻意保留的宽容。

    依据:`resolve_model` 的 docstring 写着“模型条目可缺省”,而且
    `tests/test_config.py` 里已经有对照测试(`resolve_model(cfg, "my-llm", "nope")`
    不报错)。OpenAI 兼容网关会接受任意 id,强制校验会把“用个还没登记的模型”变成不可能。

    所以扩展把模型名写错时,代价是**provider 层报错**(而不是在这里被拦)。
    我一开始把这条写成了“应该抛错”的测试 —— 是**测试期望错了**,不是代码。
    """
    runtime = _runtime(tmp_path, monkeypatch)
    resolved = runtime.set_model("alpha", "some-new-model")
    assert resolved.model == "some-new-model"
    assert resolved.entry is None                 # 没有条目 → 用 provider 的默认
    assert isinstance(runtime.llm_exec, LiteLLMClient)


# ── set_thinking_level ──────────────────────────────────

@pytest.mark.asyncio
async def test_set_thinking_level_normalizes_writes_and_notifies(tmp_path, monkeypatch):
    log = tmp_path / "events.jsonl"
    runtime = _runtime(tmp_path, monkeypatch, log)
    assert runtime.thinking_level == "low"               # 来自 settings

    assert runtime.set_thinking_level("high", source="set") == "high"
    client = runtime.llm_exec
    assert isinstance(client, ThinkingLLMClient)
    assert client.thinking_level == "high"                # 写到 client 上(下一回合生效)

    await asyncio.sleep(0)
    payloads = _of(_events(log), "thinking")
    assert payloads[0]["level"] == "high"
    assert payloads[0]["previous_level"] == "low"
    assert payloads[0]["source"] == "set"


@pytest.mark.asyncio
async def test_set_thinking_level_accepts_an_alias(tmp_path, monkeypatch):
    """`normalize_thinking_level` 的归一仍在(pi 的 xhigh/max → 本仓收敛到 high)。"""
    runtime = _runtime(tmp_path, monkeypatch)
    assert runtime.set_thinking_level("XHIGH") in ("high", "xhigh")
    client = runtime.llm_exec
    assert isinstance(client, ThinkingLLMClient)          # 可选能力:窄化后再读
    assert runtime.thinking_level == client.thinking_level


# ── set_session_title ───────────────────────────────────

@pytest.mark.asyncio
async def test_set_session_title_persists_to_all_three_places(tmp_path, monkeypatch):
    """标题在会话文件里有**两份**(`session.title` 与 header entry)—— 只改一份就会
    “列表里是新名、重开又变回旧的”。"""
    log = tmp_path / "events.jsonl"
    runtime = _runtime(tmp_path, monkeypatch, log)
    session = runtime.sessions.create("原名", cwd=runtime.cwd)

    runtime.set_session_title(session, "新名字", source="user")
    assert session.title == "新名字"
    header = json.loads(session.path.read_text(encoding="utf-8").splitlines()[0])
    assert header.get("title") == "新名字"                # 磁盘上那份也改了

    await asyncio.sleep(0)
    payloads = _of(_events(log), "title")
    assert payloads[0]["name"] == "新名字"
    assert payloads[0]["source"] == "user"


@pytest.mark.asyncio
async def test_blank_title_changes_nothing_and_notifies_nobody(tmp_path, monkeypatch):
    log = tmp_path / "events.jsonl"
    runtime = _runtime(tmp_path, monkeypatch, log)
    session = runtime.sessions.create("原名", cwd=runtime.cwd)

    runtime.set_session_title(session, "   ", source="user")
    assert session.title == "原名"
    await asyncio.sleep(0)
    assert _of(_events(log), "title") == []              # 空标题不发事件


@pytest.mark.asyncio
async def test_auto_title_path_goes_through_the_same_door(tmp_path, monkeypatch):
    """自动命名也是 `session_info_changed` 的一条来源(只是 source 不同)。

    两条来源各发一次、都不漏 —— 这正是把落盘收进 `set_session_title` 的理由。
    """
    log = tmp_path / "events.jsonl"
    runtime = _runtime(tmp_path, monkeypatch, log)
    session = runtime.sessions.create("", cwd=runtime.cwd)

    runtime._write_title(session, "模型取的名字")         # 自动命名那条路径
    assert session.title == "模型取的名字"
    await asyncio.sleep(0)
    payloads = _of(_events(log), "title")
    assert payloads[0]["name"] == "模型取的名字"
    assert payloads[0]["source"] == "auto"

    # 用户已经手动改过 → 自动命名不再覆盖(原有规则仍然生效)
    runtime._write_title(session, "又取了一个")
    assert session.title == "模型取的名字"


# ── reload_credentials(登录 / 退出登录后让新 key 生效)─────────────


def test_reload_credentials_rebuilds_client_with_the_new_key(tmp_path, monkeypatch):
    """`LiteLLMClient` 构造时就解析好 key —— 写完 auth.json **必须重建**才生效。

    不重建的症状最难查:界面上说"已登录",请求却拿旧(空)key 去 401。
    """
    from qi_agent.auth import AuthStore

    runtime = _runtime(tmp_path, monkeypatch)
    client = runtime.llm_exec
    assert isinstance(client, LiteLLMClient)
    assert "api_key" not in client._base_kwargs([], None, None)   # 还没有凭证

    AuthStore().set_key("alpha", "sk-late")
    assert runtime.reload_credentials() is True

    assert runtime.llm_exec is not client                          # 真的换了客户端
    fresh = runtime.llm_exec
    assert isinstance(fresh, LiteLLMClient)
    assert fresh._base_kwargs([], None, None)["api_key"] == "sk-late"


def test_reload_credentials_honours_removal(tmp_path, monkeypatch):
    """退出登录同样要重建:删掉 auth.json 那条后不再带 key(而不是继续用旧客户端)。

    注意先建 runtime 再写凭证 —— `AuthStore()` 的路径来自 `QI_AGENT_HOME`,而它由
    `_runtime` 指到临时目录(先写会落到**真的** `~/.qi/agent/auth.json`)。
    """
    runtime = _runtime(tmp_path, monkeypatch)
    from qi_agent.auth import AuthStore

    AuthStore().set_key("alpha", "sk-first")
    assert runtime.reload_credentials() is True
    with_key = runtime.llm_exec
    assert isinstance(with_key, LiteLLMClient)
    assert with_key._base_kwargs([], None, None)["api_key"] == "sk-first"

    AuthStore().remove("alpha")
    assert runtime.reload_credentials() is True
    without_key = runtime.llm_exec
    assert isinstance(without_key, LiteLLMClient)
    assert without_key is not with_key
    assert "api_key" not in without_key._base_kwargs([], None, None)


def test_reload_credentials_without_a_client_is_a_noop(tmp_path, monkeypatch):
    """没客户端(测试替身 / 未装配)时不炸,返回 False 让调用方自己决定要不要提示。"""
    runtime = _runtime(tmp_path, monkeypatch)
    runtime.llm_exec = None                                        # type: ignore[assignment]
    assert runtime.reload_credentials() is False
