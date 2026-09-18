"""工作区显示名偏好的读写测试(`workspaces.json`)。

这个文件只存一样东西:改过的**显示名**。测试盯的是三件真会出错的事:
目录键的规范化(否则同一目录两种写法会分成两组)、坏文件的降级(偏好文件不该让
`qi web` 起不来)、以及原子写(不能读到半截 JSON)。

不做的事:不碰真实 `~/.qi`(全部走 tmp_path);不测"删除工作区"——那是 API 层
(先删会话、再忘名字),在 `tests/test_web_api.py` 里测。
"""

from __future__ import annotations

import json
from pathlib import Path

from qi_agent.workspaces import WorkspaceNames, WorkspaceStore, normalize


def test_normalize_agrees_with_session_store(tmp_path):
    """键必须与 `SessionStore.create()` 写进 header 的写法同源。

    macOS 上 `/tmp` 是 `/private/tmp` 的软链:不 resolve 就会同一个目录两个键、
    于是同一个项目分成两组 —— 这不是假设,是必然。
    """
    link = tmp_path / "link"
    target = tmp_path / "real"
    target.mkdir()
    link.symlink_to(target)
    assert normalize(link) == normalize(target) == str(target.resolve())
    assert normalize(str(target) + "/") == str(target)  # 尾斜杠不影响


def test_rename_round_trip_including_restore(tmp_path):
    store = WorkspaceStore(tmp_path / "workspaces.json")
    cwd = str(tmp_path)

    assert store.load() == WorkspaceNames()
    store.rename(cwd, "  我的项目  ")
    assert store.load().name_of(cwd) == "我的项目"  # 前后空白被剪掉
    # 空名 = 取消改名(恢复成目录名),不是"起个空名字"
    store.rename(cwd, "   ")
    assert store.load().names == {}


def test_forget_only_drops_the_name(tmp_path):
    """`forget` 只清显示名,不碰任何文件 —— 会话的删除是 API 层的事。"""
    store = WorkspaceStore(tmp_path / "workspaces.json")
    cwd = str(tmp_path)
    store.rename(cwd, "改过的")
    store.forget(cwd)
    assert store.load().names == {}
    assert tmp_path.is_dir()  # 目录当然还在


def test_corrupt_or_hostile_file_degrades_to_empty(tmp_path):
    """坏文件/错类型一律当空 —— 这是 UI 偏好,不是数据源,不该让宿主起不来。"""
    path = tmp_path / "workspaces.json"
    store = WorkspaceStore(path)

    path.write_text("{ 这不是 JSON", encoding="utf-8")
    assert store.load() == WorkspaceNames()

    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert store.load() == WorkspaceNames()

    path.write_text('{"names": "不是对象"}', encoding="utf-8")
    assert store.load() == WorkspaceNames()

    path.write_text('{"names": {"": "  ", "/x": 5}}', encoding="utf-8")
    assert store.load() == WorkspaceNames()

    store.path.unlink()
    assert store.load() == WorkspaceNames()  # 文件不存在也当空


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    """原子写:写完目录里只应剩下目标文件,没有临时文件残留。"""
    path = tmp_path / "workspaces.json"
    store = WorkspaceStore(path)
    store.rename(str(tmp_path), "x")
    store.rename(str(tmp_path / "other"), "y")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert len(payload["names"]) == 2
    assert [p.name for p in tmp_path.iterdir()] == ["workspaces.json"]
