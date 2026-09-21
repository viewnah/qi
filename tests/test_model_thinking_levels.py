"""按模型配思考级别(pi 的 `modelThinkingLevels`)。

四级优先(pi 的口径,你已定):

    --thinking  >  --model provider/id:<级别>  >  modelThinkingLevels[该模型]  >  defaultThinkingLevel

两条容易漏的边界:

* **换模型时也要联动**:`/model` 切到配了档的模型,该档自动生效 —— 但必须在**重建客户端之前**
  改级别,否则客户端拿着旧值(footer 显示 high、请求里却没有,这类最难查);
* **显式选择 > 配置**:本会话里用 `/thinking`(或 shift+tab、`api.setThinkingLevel`)设过之后,
  换模型不再按表覆盖它。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qi_agent import paths  # noqa: E402
from qi_agent.settings import QiSettings, model_thinking_level  # noqa: E402

_MODELS = json.dumps({"providers": {
    "ollama": {"api": "openai-completions", "models": [{"id": "x"}, {"id": "y"}]},
    "beta": {"api": "openai-completions", "models": [{"id": "m3"}]}}})
_SETTINGS = '{"defaultProvider": "ollama", "defaultModel": "x"}'


def _env(tmp_path: Path, monkeypatch, extra: dict | None = None) -> None:
    (tmp_path / "models.json").write_text(_MODELS, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"defaultProvider": "ollama", "defaultModel": "x", **(extra or {})}),
        encoding="utf-8")
    monkeypatch.setenv(paths.QI_AGENT_CONFIG, str(tmp_path / "models.json"))
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))


def _runtime(tmp_path, monkeypatch, extra: dict | None = None, **kw):
    from qi_agent.runtime import QiRuntime

    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    _env(tmp_path, monkeypatch, extra)
    return QiRuntime(cwd=project, approve_project=True, **kw)


# ── 纯函数 ────────────────────────────────────────────────────────────


def _model(provider: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(provider=provider, model=name)


def test_full_key_beats_bare_id():
    """`provider/模型` 更具体 → 它赢;裸 id 是"任何 provider 下的同名模型"。"""
    s = QiSettings(modelThinkingLevels={"ollama/x": "high", "x": "low"})
    assert model_thinking_level(s, _model("ollama", "x")) == "high"
    assert model_thinking_level(s, _model("beta", "x")) == "low", "别的 provider 命中裸 id"


def test_unknown_or_missing_is_none():
    """写歪的值(不在合法级别里)按"没配"处理 —— 不该让启动失败。"""
    s = QiSettings(modelThinkingLevels={"ollama/x": "很快", "ollama/y": "high"})
    assert model_thinking_level(s, _model("ollama", "x")) is None
    assert model_thinking_level(s, _model("ollama", "nope")) is None
    assert model_thinking_level(QiSettings(), _model("ollama", "y")) is None


# ── 运行期:四级优先 ──────────────────────────────────────────────────


def test_table_applies_at_startup(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, {"modelThinkingLevels": {"ollama/x": "high"}})
    assert rt.thinking_level == "high"


def test_cli_thinking_beats_the_table(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, {"modelThinkingLevels": {"ollama/x": "high"}},
                  thinking_level="low")
    assert rt.thinking_level == "low", "--thinking 是最高一档"


def test_model_flag_suffix_beats_the_table(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, {"modelThinkingLevels": {"beta/m3": "high"}},
                  model_override="beta/m3:minimal")
    assert rt.thinking_level == "minimal", "`--model :级别` 压过表"


def test_global_default_is_the_last_resort(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, {"defaultThinkingLevel": "low"})
    assert rt.thinking_level == "low"
    assert rt.thinking_level != "off"


def test_unset_falls_back_to_pi_default_medium(tmp_path, monkeypatch):
    """什么都没配时默认 `medium`(pi `core/defaults.js` 的 `DEFAULT_THINKING_LEVEL`)。

    以前退 `off`,footer 右边就写 `• thinking off`;pi 写的是 `• medium`。
    """
    from qi_agent.llm import DEFAULT_THINKING_LEVEL, LiteLLMClient

    rt = _runtime(tmp_path, monkeypatch)
    assert DEFAULT_THINKING_LEVEL == "medium"
    assert rt.thinking_level == "medium"
    assert isinstance(rt.llm_exec, LiteLLMClient)     # 窄化后才能读 client 上的级别
    assert rt.llm_exec.thinking_level == "medium"     # 客户端也拿到(不只是字段)


def test_explicit_off_is_not_pushed_back_to_medium(tmp_path, monkeypatch):
    """显式 `--thinking off` 压过默认值(`off` 是合法档,不能被 `or 默认` 吃掉)。"""
    rt = _runtime(tmp_path, monkeypatch, thinking_level="off")
    assert rt.thinking_level == "off"


def test_settings_panel_unset_display_matches_the_default():
    """/settings 面板未设时显示的值不能与生效值拄开(两边不 import 对方,所以用测锁住)。"""
    from qi_agent.llm import DEFAULT_THINKING_LEVEL
    from qi_agent.settings import UNSET_DISPLAY

    assert UNSET_DISPLAY["defaultThinkingLevel"] == DEFAULT_THINKING_LEVEL


# ── 运行期:换模型联动 ────────────────────────────────────────────────


def test_switching_model_adopts_its_level(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, {"modelThinkingLevels": {"beta/m3": "high"}})
    assert rt.thinking_level != "high"
    rt.set_model("beta", "m3")
    assert rt.thinking_level == "high", "换到配了档的模型该自动生效"
    from qi_agent.llm import LiteLLMClient

    assert isinstance(rt.llm_exec, LiteLLMClient)
    assert rt.llm_exec.thinking_level == "high", "客户端也得拿到新级别(不是只改了字段)"


def test_switching_to_unconfigured_model_keeps_the_level(tmp_path, monkeypatch):
    """没配的模型**不动**当前级别(只有配了才采纳)—— 免得切一次模型就把用户的档洗掉。"""
    rt = _runtime(tmp_path, monkeypatch, {"modelThinkingLevels": {"ollama/x": "high"}})
    assert rt.thinking_level == "high"
    rt.set_model("beta", "m3")
    assert rt.thinking_level == "high"


def test_explicit_choice_is_not_overridden_by_a_model_switch(tmp_path, monkeypatch):
    """显式选择 > 配置:本会话用 `/thinking` 设过之后,换模型不覆盖它。"""
    rt = _runtime(tmp_path, monkeypatch, {"modelThinkingLevels": {"beta/m3": "high"}})
    rt.set_thinking_level("max")
    rt.set_model("beta", "m3")
    assert rt.thinking_level == "max", rt.thinking_level
