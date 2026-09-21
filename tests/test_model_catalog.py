"""`qi init --refresh`(从 `/models` 拉模型列表)+ 拉取与合并的规矩。

为什么单开一个文件:这条路的**重点是"以厂商接口为准"** —— 预置表只是离线种子,
模型 id 会漂(实例:DeepSeek 把 `deepseek-v4-flash` 换成 `deepseek-flash`)。
所以这里断言的是:返回体解析(三种形状)、失败一律报清楚、合并**只加不删**、
以及 CLI 真的把结果写进 `models.json`(缺 key / 网络失败时不写)。
"""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from qi_agent import model_catalog, paths  # noqa: E402
from qi_agent.auth import AuthStore  # noqa: E402
from qi_agent.model_catalog import (  # noqa: E402
    CatalogError,
    fetch_model_ids,
    merge_model_ids,
    model_ids_from_payload,
    models_url,
)

runner = CliRunner()


# ── 纯函数 ──────────────────────────────────────────────


def test_models_url_keeps_the_api_prefix():
    assert models_url("https://api.deepseek.com") == "https://api.deepseek.com/models"
    assert models_url("https://api.deepseek.com/") == "https://api.deepseek.com/models"
    assert models_url("https://dashscope.aliyuncs.com/compatible-mode/v1") == \
        "https://dashscope.aliyuncs.com/compatible-mode/v1/models"


@pytest.mark.parametrize("payload, expected", [
    ({"object": "list", "data": [{"id": "a"}, {"id": "b"}]}, ["a", "b"]),   # OpenAI 形状
    ([{"id": "a"}], ["a"]),                                                 # 裸列表
    ({"data": ["a", "b"]}, ["a", "b"]),                                     # 少数网关给字符串
    ({"data": [{"id": "a"}, {"id": "a"}, {"model": "b"}]}, ["a", "b"]),     # 去重 + 认 model 键
])
def test_model_ids_from_payload_accepts_the_shapes_we_see(payload, expected):
    assert model_ids_from_payload(payload) == expected


@pytest.mark.parametrize("payload", [
    {}, {"object": "list"}, {"data": []}, {"data": "nope"}, {"data": [{}]},
])
def test_model_ids_from_payload_refuses_to_silently_return_nothing(payload):
    """取不到 id 就报错 —— 静默返回空列表会让人以为“厂商没有模型”。"""
    with pytest.raises(CatalogError):
        model_ids_from_payload(payload)


def test_merge_model_ids_adds_and_reports_stale():
    entry = {"models": [{"id": "old", "contextWindow": 1000, "maxTokens": 10}]}
    remote = ["deepseek-flash", "deepseek-v4-pro"]
    added, stale = merge_model_ids(entry, remote)
    assert added == ["deepseek-flash", "deepseek-v4-pro"]
    assert stale == ["old"]                       # 本地有、接口没返回 → 保留并报出来
    assert entry["models"][0] == {"id": "old", "contextWindow": 1000, "maxTokens": 10}
    assert entry["models"][1] == {"id": "deepseek-flash"}     # 新加的只写 id
    # 幂等(第二次跑:没有新增,`old` 仍然是“本地有但接口没返回”)
    assert merge_model_ids(entry, remote) == ([], ["old"])


# ── fetch（把 urlopen 换掉）────────────────────────────


class _FakeResponse:
    """`urlopen` 的替身:只用到上下文管理器 + `read()`。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def _patch_urlopen(monkeypatch, *, body: bytes | None = None, error: Exception | None = None):
    seen: dict = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        if error is not None:
            raise error
        return _FakeResponse(body or b"{}")
    monkeypatch.setattr(model_catalog.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_fetch_model_ids_sends_bearer_and_parses(monkeypatch):
    seen = _patch_urlopen(monkeypatch, body=json.dumps(
        {"data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}]}).encode())
    ids = fetch_model_ids("https://api.deepseek.com", "sk-x", timeout=3.0)
    assert ids == ["deepseek-flash", "deepseek-v4-pro"]
    assert seen["url"] == "https://api.deepseek.com/models"
    assert seen["auth"] == "Bearer sk-x"
    assert seen["timeout"] == 3.0


def test_fetch_model_ids_reports_http_and_network_errors(monkeypatch):
    _patch_urlopen(monkeypatch, error=urllib.error.HTTPError(
        "u", 401, "Unauthorized", {}, None))                      # type: ignore[arg-type]
    with pytest.raises(CatalogError, match="401"):
        fetch_model_ids("https://api.deepseek.com", "bad")

    _patch_urlopen(monkeypatch, error=urllib.error.URLError("dns"))
    with pytest.raises(CatalogError, match="连不上"):
        fetch_model_ids("https://api.deepseek.com", "sk-x")

    _patch_urlopen(monkeypatch, body=b"<html>not json")
    with pytest.raises(CatalogError, match="不是 JSON"):
        fetch_model_ids("https://api.deepseek.com", "sk-x")

    with pytest.raises(CatalogError, match="没有 baseUrl"):
        fetch_model_ids("", "sk-x")


# ── CLI ─────────────────────────────────────────────────


def _cli_env(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(paths.QI_AGENT_HOME, str(home))
    monkeypatch.delenv("QI_AGENT_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    return home


def test_models_refresh_materializes_preset_and_merges_live_ids(tmp_path, monkeypatch):
    """`qi init --refresh deepseek`:写全 provider 段 + 种子模型 + 接口回来的新 id。"""
    from qi_agent.cli import app

    home = _cli_env(tmp_path, monkeypatch)
    AuthStore().set_key("deepseek", "sk-x")
    monkeypatch.setattr(model_catalog, "fetch_model_ids",
                        lambda base_url, api_key, timeout=None: [
                            "deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash"])
    # 命令内部是 `from .model_catalog import fetch_model_ids` —— 换掉**模块属性**即可
    import qi_agent.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", cli_mod.load_config)
    monkeypatch.setattr("qi_agent.model_catalog.fetch_model_ids",
                        lambda base_url, api_key, timeout=None: [
                            "deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash"])

    res = runner.invoke(app, ["init", "--refresh", "deepseek"])
    assert res.exit_code == 0, res.output
    assert "接口返回 3 个模型" in res.output

    data = json.loads((home / "models.json").read_text(encoding="utf-8"))
    entry = data["providers"]["deepseek"]
    assert entry["baseUrl"] == "https://api.deepseek.com"          # 预置的定义被写全
    assert entry["apiKey"] == "$DEEPSEEK_API_KEY"
    ids = [m["id"] for m in entry["models"]]
    assert ids[:2] == ["deepseek-flash", "deepseek-v4-pro"]        # 种子(带 ctx/max)
    assert entry["models"][0]["contextWindow"] == 1_000_000
    assert "deepseek-v4-flash" in ids                              # 接口回来的新 id

    # 再刷一次:不该重复添加
    res = runner.invoke(app, ["init", "--refresh", "deepseek"])
    assert res.exit_code == 0 and "新增 0" in res.output


def test_models_refresh_needs_a_key_and_does_not_write(tmp_path, monkeypatch):
    from qi_agent.cli import app

    home = _cli_env(tmp_path, monkeypatch)
    res = runner.invoke(app, ["init", "--refresh", "moonshot"])
    assert res.exit_code == 1
    assert "拿不到 API key" in res.output
    assert not (home / "models.json").exists()                     # 失败不写文件


def test_models_refresh_unknown_provider_and_no_target(tmp_path, monkeypatch):
    from qi_agent.cli import app

    _cli_env(tmp_path, monkeypatch)
    res = runner.invoke(app, ["init", "--refresh", "nope"])
    assert res.exit_code == 2 and "未知 provider" in res.output

    res = runner.invoke(app, ["init", "--refresh-all"])
    assert res.exit_code == 2 and "要刷新哪个" in res.output


def test_refresh_all_only_touches_credentialed_providers(tmp_path, monkeypatch):
    """`--all` 与 `/model` 同一口径:只刷有凭证的(没登录的不动)。"""
    from qi_agent.cli import app

    home = _cli_env(tmp_path, monkeypatch)
    AuthStore().set_key("deepseek", "sk-x")
    seen: list[str] = []
    monkeypatch.setattr("qi_agent.model_catalog.fetch_model_ids",
                        lambda base_url, api_key, timeout=None: seen.append(base_url) or ["m1"])

    res = runner.invoke(app, ["init", "--refresh-all"])
    assert res.exit_code == 0, res.output
    assert seen == ["https://api.deepseek.com"]                    # 只有登录过的那家
    data = json.loads((home / "models.json").read_text(encoding="utf-8"))
    assert list(data["providers"]) == ["deepseek"]


def test_models_refresh_failure_keeps_the_file_untouched(tmp_path, monkeypatch):
    from qi_agent.cli import app

    home = _cli_env(tmp_path, monkeypatch)
    AuthStore().set_key("deepseek", "sk-x")

    def boom(base_url, api_key, timeout=None):
        raise CatalogError("连不上 https://api.deepseek.com/models: dns")

    monkeypatch.setattr("qi_agent.model_catalog.fetch_model_ids", boom)
    res = runner.invoke(app, ["init", "--refresh", "deepseek"])
    assert res.exit_code == 1
    assert "拉取失败" in res.output
    assert not (home / "models.json").exists()                      # 一个字节都没写


def test_list_models_flag_is_the_listing_surface(tmp_path, monkeypatch):
    """列清单仍然是**旗标**(`qi models` 子命令是**故意删掉**的,对齐 pi —— 见
    `tests/test_extension_cli_commands.py`);`--refresh` 也因此挂在 `qi init` 上。"""
    from qi_agent.cli import app, _core_subcommand_names

    _cli_env(tmp_path, monkeypatch)
    res = runner.invoke(app, ["--list-models"])
    assert res.exit_code == 0, res.output
    assert "provider" in res.output and "credential" in res.output
    assert "models" not in _core_subcommand_names()      # 不复活已删的子命令


@pytest.mark.parametrize("base_url", ["file:///etc", "ftp://host/x", "data:text/plain,x",
                                      "api.deepseek.com", ""])
def test_models_url_refuses_non_http_schemes(base_url):
    """`baseUrl` 来自用户的 models.json:`urlopen` 碰到 `file://` 会去读本地文件 —— 挡掉。

    这不是新的信任边界(配置本来就是用户自己的),但“照配置读盘”这件事本身没必要支持:
    写错了就该报错,而不是静默换一个东西去读。
    """
    with pytest.raises(CatalogError):
        models_url(base_url)
    with pytest.raises(CatalogError):
        fetch_model_ids(base_url, "sk-x")


def test_models_url_accepts_local_http_endpoints():
    """本地/自建代理(http + 端口)要照样能用。"""
    assert models_url("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1/models"


def test_models_refresh_writes_back_to_the_file_that_defines_the_provider(tmp_path, monkeypatch):
    """刷新写回**定义它的那个文件** —— 否则会在别处长出一份同名定义(合并取高优先级的,
    于是“刷新了却没生效”)。"""
    from qi_agent.cli import app

    home = _cli_env(tmp_path, monkeypatch)
    (tmp_path / ".git").mkdir()                       # 项目根 = 有 .git 的那层
    project_models = tmp_path / ".qi" / "models.json"
    project_models.parent.mkdir(parents=True, exist_ok=True)
    project_models.write_text(json.dumps({"providers": {"local": {
        "baseUrl": "http://127.0.0.1:9/v1", "apiKey": "sk-x",
        "models": [{"id": "seed"}],
    }}}), encoding="utf-8")
    monkeypatch.setattr("qi_agent.model_catalog.fetch_model_ids",
                        lambda base_url, api_key, timeout=None: ["seed", "live"])

    res = runner.invoke(app, ["init", "--refresh", "local"])
    assert res.exit_code == 0, res.output

    data = json.loads(project_models.read_text(encoding="utf-8"))
    assert [m["id"] for m in data["providers"]["local"]["models"]] == ["seed", "live"]
    assert not (home / "models.json").exists()        # 没在用户级另长一份


def test_models_refresh_materializes_a_preset_only_provider_at_user_level(tmp_path, monkeypatch):
    """预置兜底来的 provider(哪个文件都没定义)→ 物化到用户级。"""
    from qi_agent.cli import app

    home = _cli_env(tmp_path, monkeypatch)
    AuthStore().set_key("moonshot", "sk-m")
    monkeypatch.setattr("qi_agent.model_catalog.fetch_model_ids",
                        lambda base_url, api_key, timeout=None: ["kimi-k3", "kimi-new"])

    res = runner.invoke(app, ["init", "--refresh", "moonshot"])
    assert res.exit_code == 0, res.output
    data = json.loads((home / "models.json").read_text(encoding="utf-8"))
    entry = data["providers"]["moonshot"]
    assert entry["baseUrl"] == "https://api.moonshot.cn/v1"
    assert [m["id"] for m in entry["models"]] == ["kimi-k3", "kimi-k2.7-code", "kimi-new"]
    assert entry["models"][0]["maxTokens"] == 131_072     # 种子里核过的数还在
