"""服务器目录浏览(「添加工作区」选择器的后端)单元测试。

这里只测**纯逻辑**:`~` 展开、软链、排序、只列目录。状态码(404/400/403)与
鉴权在 `tests/test_web_api.py` 里测 —— 那是端点的事。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qi_agent.web import browse


@pytest.fixture
def home(tmp_path, monkeypatch):
    """把"主目录"指到 tmp:测 `~` 展开又不碰真实 home。"""
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(browse, "home_dir", lambda: fake)
    return fake


def test_empty_path_means_home(home):
    assert browse.resolve_dir("") == Path(str(home))
    assert browse.resolve_dir(None) == Path(str(home))
    assert browse.resolve_dir("~") == Path(str(home))
    assert browse.resolve_dir("~/src") == Path(str(home)) / "src"


def test_only_directories_sorted_case_insensitively(home, tmp_path):
    root = tmp_path / "proj"
    (root / "Zeta").mkdir(parents=True)
    (root / "alpha").mkdir()
    (root / "README.md").write_text("x", encoding="utf-8")  # 文件不该出现
    names = [e["name"] for e in browse.list_directories(str(root))]
    assert names == ["alpha", "Zeta"]          # 不区分大小写排序
    assert all(Path(e["path"]).is_dir() for e in browse.list_directories(str(root)))


def test_symlink_to_directory_counts(home, tmp_path):
    """指向目录的软链算目录(开发者常把源码放在软链后面);断链不算。"""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    (tmp_path / "broken").symlink_to(tmp_path / "nope")
    names = [e["name"] for e in browse.list_directories(str(tmp_path))]
    assert "link" in names and "broken" not in names


def test_dotfiles_are_listed(tmp_path):
    """`.` 开头的目录(如 .config、.git)要列出来 —— 选择器里它们是正常目标。"""
    root = tmp_path / "proj"          # 单独一层:`home` 夹具也会落在 tmp_path 下
    (root / ".config").mkdir(parents=True)
    (root / ".git").mkdir()
    names = [e["name"] for e in browse.list_directories(str(root))]
    assert names == [".config", ".git"]


def test_missing_and_not_a_directory_raise_the_natural_errors(home, tmp_path):
    with pytest.raises(FileNotFoundError):
        browse.list_directories(str(tmp_path / "没有这个"))
    file = tmp_path / "a.txt"
    file.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        browse.list_directories(str(file))


def test_parent_is_none_only_at_the_root(home):
    assert browse.parent_of(Path("/")) is None
    assert browse.parent_of(Path("/tmp")) == "/"


def test_windows_drives_are_empty_off_windows():
    assert browse.windows_drives() == [] or browse.windows_drives()[0]["name"].endswith(":")
