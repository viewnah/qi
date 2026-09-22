"""`scripts/prune_empty_sessions.py` —— 清理存量空会话的判据。

为什么值得测:`--apply` 会**真删文件**。判错一边是"删掉用户的会话"(不可恢复),
另一边是"空会话继续挤满 `/resume`"。所以把口径钉在测试里,尤其那几条**保守**的:

  * 有消息(哪怕只有用户那句、没有回答)→ 保留;
  * 无消息但**有名字** → 保留(可能是用户起的 —— 脚本不替用户做决定);
  * 读不了 / 有坏行 → 保留(宁可留一个可疑文件)。

脚本不在包里(它是运维工具),所以这里按**文件路径**加载,顺带验证它能独立运行
(它自己往 `sys.path` 里插 `src/`,不依赖已安装的包)。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prune_empty_sessions.py"


@pytest.fixture(scope="module")
def prune():
    spec = importlib.util.spec_from_file_location("prune_empty_sessions", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(root: Path, name: str, entries: list[dict]) -> Path:
    path = root / name
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
                    encoding="utf-8")
    return path


def _header(title: str = "") -> dict:
    return {"type": "session", "version": 2, "id": "x", "title": title,
            "created_at": "2026-01-01T00:00:00"}


def test_empty_header_only_is_prunable(prune, tmp_path):
    path = _write(tmp_path, "a.jsonl", [_header()])
    empty, why = prune.looks_empty(path)
    assert empty is True and why == ""


def test_zero_byte_file_is_prunable(prune, tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert prune.looks_empty(path)[0] is True


def test_legacy_default_title_is_not_a_users_name(prune, tmp_path):
    """旧版写死的默认名 `tui` 不算名字 —— 否则两千多条老会话一个都清不掉。"""
    path = _write(tmp_path, "a.jsonl", [_header("tui")])
    assert prune.looks_empty(path)[0] is True


def test_message_is_never_pruned(prune, tmp_path):
    """有消息就保留 —— 哪怕只有用户那句、没有回答(会话可能被中断,内容仍是用户的)。"""
    path = _write(tmp_path, "a.jsonl", [
        _header(),
        {"type": "message", "role": "user", "content": "只问了没答"},
    ])
    empty, why = prune.looks_empty(path)
    assert empty is False and why == "有消息"


def test_only_settings_entries_are_prunable(prune, tmp_path):
    """只有 header + 设置类 entry 的会话仍然算空(启动时落的那两条就是这种)。"""
    path = _write(tmp_path, "a.jsonl", [
        _header(),
        {"type": "model_change", "provider": "p", "model_id": "m"},
        {"type": "thinking_level_change", "thinking_level": "high"},
    ])
    assert prune.looks_empty(path)[0] is True


def test_named_but_empty_is_kept(prune, tmp_path):
    """无消息**但用户改过名** → 保留。改名字是用户的动作,脚本不替他把这个决定推翻。"""
    path = _write(tmp_path, "a.jsonl", [_header("我的重要会话")])
    empty, why = prune.looks_empty(path)
    assert empty is False and "我的重要会话" in why


def test_corrupt_file_is_kept(prune, tmp_path):
    """坏行 → 保留。宁可留一个可疑文件,也不删掉一个**可能**含内容的会话。"""
    path = tmp_path / "bad.jsonl"
    path.write_text('{"type": "session", "title": ""}\n{ 这不是 JSON\n', encoding="utf-8")
    empty, why = prune.looks_empty(path)
    assert empty is False and why == "有坏行"


def test_main_dry_run_deletes_nothing(prune, tmp_path, capsys, monkeypatch):
    """**默认 dry-run**:只列清单,一个文件都不动加 `--apply` 才真删。"""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    doomed = _write(sessions, "empty.jsonl", [_header()])
    kept = _write(sessions, "kept.jsonl", [
        _header(), {"type": "message", "role": "user", "content": "hi"}])

    monkeypatch.setattr(sys, "argv", ["prune", "--dir", str(sessions)])
    assert prune.main() == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "1 个空会话(无消息、无名字)" in out
    assert "将删除 1 个" in out
    assert doomed.is_file() and kept.is_file()          # 都没删


def test_main_apply_removes_only_empty(prune, tmp_path, monkeypatch, capsys):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    doomed = _write(sessions, "empty.jsonl", [_header()])
    named = _write(sessions, "named.jsonl", [_header("有名字")])
    kept = _write(sessions, "kept.jsonl", [
        _header(), {"type": "message", "role": "user", "content": "hi"}])

    monkeypatch.setattr(sys, "argv", ["prune", "--dir", str(sessions), "--apply"])
    assert prune.main() == 0
    assert not doomed.exists()
    assert named.is_file(), "有名字的空会话不删"
    assert kept.is_file()
    out = capsys.readouterr().out
    assert "已删除 1 个空会话" in out
    assert "保留 1 个无消息但有名字的" in out


def test_include_named_deletes_them_too(prune, tmp_path, monkeypatch, capsys):
    """`--include-named` 是给测试垃圾用的(它们带着假名字,如 `create("t")` 的 `"t"`)。"""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    named = _write(sessions, "named.jsonl", [_header("t")])
    kept = _write(sessions, "kept.jsonl", [
        _header(), {"type": "message", "role": "user", "content": "hi"}])

    monkeypatch.setattr(sys, "argv",
                        ["prune", "--dir", str(sessions), "--apply", "--include-named"])
    assert prune.main() == 0
    assert not named.exists()
    assert kept.is_file(), "有消息的永远不删,哪怕带 --include-named"
    out = capsys.readouterr().out
    assert "已删除 1 个空会话" in out
    assert "保留 0 个无消息但有名字的" in out


def test_include_named_default_off_keeps_named(prune, tmp_path, monkeypatch):
    """不加开关时,"有名字但无消息"照旧保留 —— 默认口径不能被静默放宽。"""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    named = _write(sessions, "named.jsonl", [_header("我的会话")])
    monkeypatch.setattr(sys, "argv", ["prune", "--dir", str(sessions), "--apply"])
    assert prune.main() == 0
    assert named.is_file()


def test_main_reports_missing_dir(prune, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prune", "--dir", str(tmp_path / "没有这个")])
    assert prune.main() == 1
    assert "没有这个目录" in capsys.readouterr().out


def test_default_dir_follows_agent_home(prune, tmp_path, monkeypatch):
    """默认目录跟着 `QI_AGENT_HOME` 走 —— 免得它去动真实 `~/.qi`。"""
    monkeypatch.setenv("QI_AGENT_HOME", str(tmp_path / "agent"))
    assert prune.default_sessions_dir() == tmp_path / "agent" / "sessions"
