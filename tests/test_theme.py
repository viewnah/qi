"""pi 主题移植回归:调色板解析 / 终端背景探测 / footer 格式化。

调色板数据 vendored 自 pi(`themes/dark.json` / `light.json`),这些测试锁住
「解析规则」和「探测规则」不漂移 —— 颜色本身变了应该能被 diff 出来。
"""

from __future__ import annotations

import dataclasses

import pytest

from qi_agent.theme import (
    Palette,
    ThemeError,
    format_cwd_line,
    format_tokens,
    load_palette,
    parse_background_color,
    parse_osc11,
    resolve_theme,
    rich_theme,
    shorten_home,
    syntax_theme,
    textual_theme,
)


def test_dark_palette_resolves_vars_references():
    p = load_palette("dark")
    assert p.colors["accent"] == "#8abeb7"       # vars 引用
    assert p.colors["mdHeading"] == "#f0c674"    # 直接 hex
    assert p.colors["userMessageBg"] == "#343541"
    assert p.is_dark


def test_light_palette_is_not_dark():
    p = load_palette("light")
    assert p.is_dark is False
    assert p.colors["userMessageBg"] == "#e8e8e8"
    assert p.page_bg == "#f8f8f8"


def test_palette_unknown_key_raises_instead_of_blank():
    with pytest.raises(ThemeError):
        load_palette("dark").hex("nope")


def test_unknown_theme_name_raises():
    with pytest.raises(ThemeError):
        load_palette("solarized")


@pytest.mark.parametrize("reply,expected", [
    ("\x1b]11;rgb:1e1e/1e1e/1e1e\x07", "dark"),
    ("\x1b]11;rgb:ffff/ffff/ffff\x07", "light"),
    ("\x1b]11;rgb:00/00/00\x07", "dark"),
    ("\x1b]11;#ffffff\x07", "light"),
    ("\x1b]11;#101010\x07", "dark"),
    ("nonsense", None),
    ("", None),
])
def test_parse_osc11(reply, expected):
    assert parse_osc11(reply) == expected


def test_parse_background_color_takes_high_byte_of_16bit():
    assert parse_background_color("\x1b]11;rgb:ffff/e8e8/e8e8\x07") == "#ffe8e8"
    assert parse_background_color("no color here") is None


def test_format_tokens_matches_pi_thresholds():
    assert format_tokens(0) == "0"
    assert format_tokens(999) == "999"
    assert format_tokens(1234) == "1.2k"
    assert format_tokens(12345) == "12k"
    assert format_tokens(999_999) == "1000k"
    assert format_tokens(1_500_000) == "1.5M"
    assert format_tokens(12_000_000) == "12M"


def test_shorten_home_and_cwd_line():
    assert shorten_home("/Users/me/proj", "/Users/me") == "~/proj"
    assert shorten_home("/tmp/x", "/Users/me") == "/tmp/x"
    line = format_cwd_line("/Users/me/proj", home="/Users/me", branch="main", session_name="tui")
    assert line == "~/proj (main) • tui"
    assert format_cwd_line("/tmp/x", home="/Users/me") == "/tmp/x"


def test_resolve_theme_env_overrides_setting(monkeypatch):
    monkeypatch.setenv("QI_THEME", "light")
    assert resolve_theme("dark", probe=False).name == "light"


def test_resolve_theme_defaults_to_dark_without_probe(monkeypatch):
    monkeypatch.delenv("QI_THEME", raising=False)
    assert resolve_theme("dark", probe=False).name == "dark"
    assert resolve_theme("auto", probe=False).name == "dark"   # 探测不到 → dark(对齐 pi)
    assert resolve_theme("garbage", probe=False).name == "dark"


def test_textual_theme_uses_detected_terminal_background():
    palette = dataclasses.replace(load_palette("dark"), terminal_bg="#101010")
    theme = textual_theme(palette)
    assert theme.background == "#101010"          # inline 区域与终端底色一致 = 看不出填色
    assert theme.dark is True
    assert theme.variables["md-code"] == "#8abeb7"


def test_textual_theme_falls_back_to_page_bg():
    theme = textual_theme(load_palette("light"))
    assert theme.background == "#f8f8f8"


def test_rich_and_syntax_themes_build():
    palette = load_palette("dark")
    from rich.console import Console

    console = Console()
    console.push_theme(rich_theme(palette))
    assert console.get_style("markdown.h1").color is not None
    syntax = syntax_theme(palette)
    # pi 的代码块没有底色(None = 不画背景)
    assert syntax.get_background_style().bgcolor is None


def test_palette_dataclass_is_frozen():
    palette = Palette(name="dark", colors={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        palette.name = "light"  # type: ignore[misc]
