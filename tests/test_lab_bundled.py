"""内置扩展清单:顺序、外部覆盖与状态。"""

from __future__ import annotations

from pathlib import Path

from qi_lab import bundled


def test_bundle_manifest_is_complete():
    exts = bundled.load_bundle()
    keys = [e.key for e in exts]
    assert keys == [
        "qi-mcp", "qi-agents", "qi-a2a", "qi-store", "qi-kb", "qi-assistants",
    ]
    for ext in exts:
        assert ext.available, f"{ext.key} 缺少 extension.py"


def test_agents_load_after_mcp():
    """qi-agents 在 import 期就可能用到 qi-mcp,拓扑排序必须把它放后面。"""
    order = [e.key for e in bundled.active_extensions(Path.cwd(), trusted=True)]
    assert order.index("qi-mcp") < order.index("qi-agents")


def test_external_extension_hides_bundled_copy(monkeypatch):
    monkeypatch.setattr(bundled, "_external_keys", lambda cwd, trusted: {"qi-mcp"})
    active = [e.key for e in bundled.active_extensions(Path.cwd(), trusted=True)]
    assert "qi-mcp" not in active
    assert "qi-agents" in active


def test_status_reports_bundled(tmp_path, monkeypatch):
    monkeypatch.setattr(bundled, "_external_keys", lambda cwd, trusted: set())
    rows = {row["name"]: row for row in bundled.status(tmp_path, trusted=True)}
    assert rows["qi-mcp"]["state"] == "bundled"
    assert rows["qi-mcp"]["version"] == bundled._lab_version()
