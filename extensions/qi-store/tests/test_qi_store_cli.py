"""`qi store` 的 P0 四条命令。

行数不多,但每条都在钉一个**行为契约**(退出码 / 有没有写文件 / 失败时留没留下东西),
这些是脚本与后续 P1 依赖的东西。
"""

from __future__ import annotations

import io
import json
import sys
import time

import httpx
import pytest

from qi_store import cli, config, paths
from qi_store.client import StoreClient
from qi_store.errors import EXIT_AUTH, EXIT_CANCELLED, EXIT_OK, EXIT_USAGE


def jwt(exp: int, username: str = "admin", role: str = "admin") -> str:
    import base64
    payload = {"username": username, "role": role, "exp": exp}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{raw}.sig"


def factory(handler):
    """把 MockTransport 塞进 CLI 的 `http_factory` 钩子(生产路径一行不改)。"""
    def make(source, **kwargs):
        return StoreClient(source, http=httpx.Client(transport=httpx.MockTransport(handler)),
                           **kwargs)
    return make


def ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/auth/login":
        return httpx.Response(200, json={
            "access_token": jwt(int(time.time()) + 10800),
            "refresh_token": "refresh-1",
            "user": {"username": "admin", "role": "admin"},
        })
    if request.url.path == "/api/auth/me":
        return httpx.Response(200, json={"username": "admin", "role": "admin"})
    return httpx.Response(404, json={"code": 404, "message": "no"})


# ── 用法 ────────────────────────────────────────────────


def test_no_subcommand_prints_help_and_returns_usage(capsys):
    assert cli.run([]) == EXIT_USAGE
    assert "store" in capsys.readouterr().err


def test_agent_kind_is_not_a_command_yet(capsys):
    """`agent` 是**留槽未实现**的 kind:报错要说理由与可用项,而不是“P1”。"""
    assert cli.run(["agent"]) == EXIT_USAGE
    assert "格式" in capsys.readouterr().err


# ── login ───────────────────────────────────────────────


def test_login_writes_auth_to_user_layer(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("123456\n"))
    code = cli.run(["login", "http://h:6091", "-u", "admin", "--password-stdin"],
                   http_factory=factory(ok_handler))
    assert code == EXIT_OK
    stored = json.loads(paths.user_config_file().read_text(encoding="utf-8"))
    assert stored["stores"]["hikqin"]["baseUrl"] == "http://h:6091"
    assert stored["stores"]["hikqin"]["auth"]["login"] == "admin"
    assert stored["stores"]["hikqin"]["auth"]["refresh"] == "refresh-1"
    assert stored["defaultStore"] == "hikqin"      # 第一个源自动成为默认
    assert "已登录" in capsys.readouterr().out


def test_login_failure_returns_auth_code_and_writes_nothing(monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401, "message": "用户名或密码错误"})

    monkeypatch.setattr(sys, "stdin", io.StringIO("bad\n"))
    code = cli.run(["login", "http://h:6091", "-u", "admin", "--password-stdin"],
                   http_factory=factory(handler))
    assert code == EXIT_AUTH
    assert not paths.user_config_file().exists()   # 先验后记:失败不落文件
    assert "用户名或密码错误" in capsys.readouterr().err


def test_login_needs_a_url_on_first_run(monkeypatch, capsys):
    code = cli.run(["login"], http_factory=factory(ok_handler))
    assert code == EXIT_USAGE
    assert "6091" in capsys.readouterr().err


def test_login_without_password_channel_refuses_non_tty(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))       # isatty() == False
    code = cli.run(["login", "http://h:6091", "-u", "admin"],
                   http_factory=factory(ok_handler))
    assert code == EXIT_USAGE
    assert "--password-stdin" in capsys.readouterr().err


def test_login_with_token_skips_the_login_endpoint(capsys):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"username": "ci", "role": "user"})

    code = cli.run(["login", "http://h:6091", "--token", jwt(int(time.time()) + 600)],
                   http_factory=factory(handler))
    assert code == EXIT_OK
    assert calls == ["/api/auth/me"]
    stored = json.loads(paths.user_config_file().read_text(encoding="utf-8"))
    assert stored["stores"]["hikqin"]["auth"]["role"] == "user"


# ── whoami ──────────────────────────────────────────────


def _seed(exp: int, *, refresh: str | None = None) -> None:
    config.save_auth("hikqin", {"login": "admin", "role": "admin",
                                "access": jwt(exp), "refresh": refresh, "exp": exp},
                     base_url="http://h:6091")
    config.save_default("hikqin")


def test_whoami_is_offline_and_shows_remaining(capsys):
    _seed(int(time.time()) + 3600)

    def handler(request: httpx.Request) -> httpx.Response:      # pragma: no cover
        raise AssertionError("whoami 默认不发请求")

    assert cli.run(["whoami"], http_factory=factory(handler)) == EXIT_OK
    assert "admin" in capsys.readouterr().out


def test_whoami_expired_token_exits_auth(monkeypatch, capsys):
    _seed(int(time.time()) - 10)
    # 过期但没 refresh → 明确要求重新登录,而不是去好奇心发请求
    assert cli.run(["whoami"], http_factory=factory(ok_handler)) == EXIT_AUTH
    assert "过期" in capsys.readouterr().err


def test_whoami_remote_confirms_with_upstream(capsys):
    _seed(int(time.time()) + 600)
    assert cli.run(["whoami", "--remote"], http_factory=factory(ok_handler)) == EXIT_OK
    assert "远端确认" in capsys.readouterr().out


def test_whoami_without_login_exits_auth(capsys):
    assert cli.run(["whoami"], http_factory=factory(ok_handler)) == EXIT_AUTH
    assert "未登录" in capsys.readouterr().err


def test_whoami_honours_env_token(monkeypatch, capsys):
    config.save_auth("hikqin", None, base_url="http://h:6091")
    monkeypatch.setenv("QI_STORE_TOKEN", jwt(int(time.time()) + 600, username="ci"))
    assert cli.run(["whoami"], http_factory=factory(ok_handler)) == EXIT_OK
    out = capsys.readouterr().out
    assert "ci" in out and "QI_STORE_TOKEN" in out


# ── logout ──────────────────────────────────────────────


def test_logout_removes_auth_but_keeps_the_source(capsys):
    _seed(int(time.time()) + 600)
    assert cli.run(["logout"], http_factory=factory(ok_handler)) == EXIT_OK
    stored = json.loads(paths.user_config_file().read_text(encoding="utf-8"))
    assert "auth" not in stored["stores"]["hikqin"]
    assert stored["stores"]["hikqin"]["baseUrl"] == "http://h:6091"
    assert "没有吊销接口" in capsys.readouterr().out


def test_logout_is_idempotent(capsys):
    _seed(int(time.time()) + 600)
    cli.run(["logout"], http_factory=factory(ok_handler))
    assert cli.run(["logout"], http_factory=factory(ok_handler)) == EXIT_OK
    assert "幂等" in capsys.readouterr().out


def test_logout_all_clears_cache_and_every_source(capsys):
    _seed(int(time.time()) + 600)
    config.save_auth("other", {"access": "x"}, base_url="http://o:1")
    cache = paths.cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "hikqin.json").write_text("{}", encoding="utf-8")

    assert cli.run(["logout", "--all"], http_factory=factory(ok_handler)) == EXIT_OK
    stored = json.loads(paths.user_config_file().read_text(encoding="utf-8"))
    assert "auth" not in stored["stores"]["hikqin"]
    assert "auth" not in stored["stores"]["other"]
    assert not cache.exists()


# ── sources ─────────────────────────────────────────────


def test_sources_lists_default_and_login_state(capsys):
    _seed(int(time.time()) + 600)
    assert cli.run(["sources"], http_factory=factory(ok_handler)) == EXIT_OK
    out = capsys.readouterr().out
    assert "hikqin" in out and "default" in out and "已登录" in out


# ── 契约:警告与取消 ─────────────────────────────────────


def test_project_level_auth_warns_on_stderr(store_home, project, capsys):
    config.save_auth("hikqin", {"login": "me", "access": jwt(int(time.time()) + 600)},
                     base_url="http://user:6091")
    (project / ".qi").mkdir(parents=True, exist_ok=True)
    (project / ".qi" / "store.json").write_text(json.dumps({
        "stores": {"hikqin": {"auth": {"access": "leaked"}}}}), encoding="utf-8")
    assert cli.run(["whoami"], cwd=project, http_factory=factory(ok_handler)) == EXIT_OK
    err = capsys.readouterr().err
    assert "只能放用户级" in err


def test_keyboard_interrupt_is_130(monkeypatch, capsys):
    def boom(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.config, "load", boom)
    assert cli.run(["whoami"], http_factory=factory(ok_handler)) == EXIT_CANCELLED
