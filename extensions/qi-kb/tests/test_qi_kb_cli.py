from __future__ import annotations

import json
from pathlib import Path

from qi_kb import cli, config
from qi_kb.errors import Conflict


def write_kb(home: Path) -> None:
    (home / "kb.json").write_text(json.dumps({
        "providers": {
            "alice": {"baseUrl": "http://hik:7091",
                      "knowledgeBases": {"kb1": {"id": "kb1", "name": "测试库"}}},
        }
    }, ensure_ascii=False), encoding="utf-8")


def test_cli_provider_add(kb_home):
    code = cli.main(["provider", "add", "alice", "--base-url", "http://hik:7091", "--json"])
    assert code == 0
    cfg = config.load()
    assert cfg.providers["alice"]["baseUrl"] == "http://hik:7091"


def test_cli_status_json(kb_home, capsys):
    write_kb(kb_home)
    assert cli.main(["status", "--json"]) == 0
    out = capsys.readouterr().out
    assert "测试库" in out


def test_cli_install_conflict_noninteractive(kb_home, monkeypatch):
    write_kb(kb_home)

    def fake_install(*args, **kwargs):
        raise Conflict("已由 alice 安装", existing_provider="alice", target_provider="bob")

    monkeypatch.setattr(cli.service, "install", fake_install)
    code = cli.main(["install", "kb1", "--provider", "bob"])
    assert code == 5


def test_cli_install_rebind(kb_home, monkeypatch):
    write_kb(kb_home)
    seen = {}

    def fake_install(cwd, kb, provider, *, scope="global", rebind=False):
        seen.update(kb=kb, provider=provider, rebind=rebind)
        return {"action": "rebind", "provider": provider, "kbId": kb,
                "kb": {"name": "测试库"}}

    monkeypatch.setattr(cli.service, "install", fake_install)
    code = cli.main(["install", "kb1", "--provider", "bob", "--rebind", "--json"])
    assert code == 0
    assert seen["rebind"] is True


def test_cli_ask_stream(kb_home, monkeypatch, capsys):
    write_kb(kb_home)

    def fake_stream(*args, **kwargs):
        yield {"type": "token", "content": "你", "provider": "alice"}
        yield {"type": "token", "content": "好", "provider": "alice"}
        yield {"type": "sources", "content": [{"doc_name": "a.pdf",
                                               "kb_name": "测试库",
                                               "provider": "alice"}]}
        yield {"type": "done", "provider": "alice", "kbIds": ["kb1"]}

    monkeypatch.setattr(cli.service, "ask_stream", fake_stream)
    assert cli.main(["ask", "问题", "--stream"]) == 0
    out = capsys.readouterr().out
    assert "你好" in out
    assert "a.pdf" in out
