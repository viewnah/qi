"""应用设置 `settings.json`(对齐 pi)。

位置(高 → 低):

    <git根>/.qi/settings.json      项目覆盖(可提交共享;与 `~/.qi/agent` 配对)
    ~/.qi/agent/settings.json      全局(对齐 pi 的 `~/.pi/agent/settings.json`)

合并规则同 pi:键级深合并,项目覆盖全局;数组整体替换(不逐项合并)。
路径类字段(extensions / skills / prompts / themes)相对**各自 settings.json 所在目录**
解析,绝对路径与 `~` 都支持;`!pat` / `-pat` 为排除项,`+path` 为强制纳入。

本模块只负责"JSON 文件的装载/合并/写入"这一层,models.json 的语义在 config.py。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .paths import SETTINGS_FILE_NAME, settings_file_candidates

SCOPES = ("user", "project")


class SettingsError(Exception):
    """settings.json 读取/校验/写入失败。"""


# ── 通用 JSON 装载与合并 ────────────────────────────────

def read_json(path: Path) -> dict[str, Any]:
    """读取 JSON 对象文件;空文件视为 {};失败抛 SettingsError(带路径)。"""
    try:
        with path.open("rb") as fh:
            raw = fh.read()
        if not raw.strip():
            return {}
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SettingsError(f"读取 {path} 失败: {exc}") from exc
    return data if isinstance(data, dict) else {}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """键级深合并:override 覆盖 base;嵌套 dict 递归合并;数组整体替换。"""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# ── 设置模型 ────────────────────────────────────────────

class QiSettings(BaseModel):
    """settings.json 的已知字段;未知字段原样保留(extra=allow)。

    字段名与 pi 一致,便于两边共享同一个文件;`skillsEnabled` 是 qi 扩展
    (pi 用 `--no-skills` 关闭技能)。
    """

    model_config = {"extra": "allow"}  # type: ignore[assignment]

    # 模型
    defaultProvider: str | None = None
    defaultModel: str | None = None
    defaultThinkingLevel: str | None = None
    enabledModels: list[str] | None = None

    # 界面
    theme: str | None = None
    quietStartup: bool = False
    defaultProjectTrust: str = "ask"
    doubleEscapeAction: str = "tree"

    # 会话 / 工具
    sessionDir: str | None = None
    defaultTools: list[str] | None = None

    # 运行行为
    compaction: dict[str, Any] | None = None
    retry: dict[str, Any] | None = None

    # 资源
    packages: list[Any] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    prompts: list[str] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    enableSkillCommands: bool = True
    skillsEnabled: bool = True


# ── 装载 ────────────────────────────────────────────────

def settings_scope_path(scope: str, cwd: Path | None = None) -> Path:
    """某作用域的 settings.json 路径(写入用)。"""
    for path, name in settings_file_candidates(cwd):
        if name == scope:
            return path
    raise SettingsError(f"未知作用域 {scope!r}(可选:{', '.join(SCOPES)})")


def load_settings_raw(cwd: Path | None = None) -> tuple[dict[str, Any], list[Path]]:
    """按优先级装载并深合并,返回 (merged, 实际读取的文件列表(高→低))。"""
    merged: dict[str, Any] = {}
    loaded: list[Path] = []
    for path, _scope in reversed(settings_file_candidates(cwd)):  # 低 → 高
        if not path.is_file():
            continue
        merged = deep_merge(merged, read_json(path))   # read_json 失败已带路径报错
        loaded.append(path)
    return merged, list(reversed(loaded))


def load_settings(cwd: Path | None = None) -> tuple[QiSettings, list[Path]]:
    """装载校验后的设置;无文件时返回默认值。"""
    raw, loaded = load_settings_raw(cwd)
    try:
        return QiSettings.model_validate(raw), loaded
    except Exception as exc:  # pydantic.ValidationError
        raise SettingsError(f"{SETTINGS_FILE_NAME} 校验失败: {exc}") from exc


def load_settings_by_scope(cwd: Path | None = None) -> dict[str, QiSettings]:
    """每个作用域**各自**解析后的设置(不合并),缺失的作用域不出现在结果里。

    资源路径必须按这个结果解析:pi 的语义是 `~/.pi/agent/settings.json` 里的路径
    相对于 `~/.pi/agent`,`<cwd>/.pi/settings.json` 里的相对于 `.pi`。用合并后的
    数组会让一侧的路径落到另一侧的基准目录上。
    """
    out: dict[str, QiSettings] = {}
    for path, scope in settings_file_candidates(cwd):
        if not path.is_file():
            continue
        try:
            out[scope] = QiSettings.model_validate(read_json(path))
        except Exception as exc:  # pydantic.ValidationError
            raise SettingsError(f"{path} 校验失败: {exc}") from exc
    return out


def save_settings(scope: str, data: dict[str, Any], cwd: Path | None = None) -> Path:
    """整份写入某作用域的 settings.json(保留未知字段)。"""
    path = settings_scope_path(scope, cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# ── 点号键读写(qi config --set / --unset) ──────────────

def parse_value(text: str) -> Any:
    """`--set` 的值:先按 JSON 解析,失败则当字符串(便于 theme=light)。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def set_value(scope: str, dotted: str, value: Any, cwd: Path | None = None) -> Path:
    """写某个点号路径(如 skills / compaction.enabled);中间层不存在则建。"""
    path = settings_scope_path(scope, cwd)
    data = read_json(path) if path.is_file() else {}
    keys = [k for k in dotted.split(".") if k]
    if not keys:
        raise SettingsError("键不能为空")
    cursor = data
    for key in keys[:-1]:
        nxt = cursor.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[key] = nxt
        cursor = nxt
    cursor[keys[-1]] = value
    return save_settings(scope, data, cwd)


def unset_value(scope: str, dotted: str, cwd: Path | None = None) -> tuple[Path, bool]:
    """删除某个点号路径;返回 (文件, 是否真的删掉了键)。"""
    path = settings_scope_path(scope, cwd)
    data = read_json(path) if path.is_file() else {}
    keys = [k for k in dotted.split(".") if k]
    cursor: Any = data
    for key in keys[:-1]:
        cursor = cursor.get(key) if isinstance(cursor, dict) else None
        if not isinstance(cursor, dict):
            return path, False
    removed = False
    if isinstance(cursor, dict) and keys and keys[-1] in cursor:
        del cursor[keys[-1]]
        removed = True
    if removed:
        save_settings(scope, data, cwd)
    return path, removed


# ── 资源路径解析 ────────────────────────────────────────

def split_resource_values(values: list[str]) -> tuple[list[str], list[str]]:
    """把资源数组拆成 (纳入, 排除);`!pat`/`-pat` 为排除,`+path` 为强制纳入。"""
    includes: list[str] = []
    excludes: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        if value.startswith(("!", "-")):
            excludes.append(value[1:])
        elif value.startswith("+"):
            includes.append(value[1:])
        else:
            includes.append(value)
    return includes, excludes


def resolve_resource_paths(value: str, base_dir: Path) -> list[Path]:
    """把一条资源路径解析成实际目录/文件;支持 `~`、相对路径与 glob。"""
    expanded = value.strip().replace("\\", "/")
    if expanded.startswith("~"):
        root = Path(expanded).expanduser()
    else:
        root = Path(expanded)
        if not root.is_absolute():
            root = base_dir / root
    if any(ch in str(root) for ch in "*?["):
        try:
            return sorted(p for p in base_dir.glob(expanded) if p.exists())
        except (OSError, ValueError):
            return []
    return [root] if root.exists() else []


def settings_include_paths(settings: QiSettings | None, scope: str, key: str,
                           cwd: Path | None = None) -> list[Path]:
    """某作用域某资源字段(skills/extensions/...)的**纳入**路径(已展开 glob)。

    `settings` 必须是 `load_settings_by_scope()[scope]` —— 即该作用域**自己**的
    那份,不能传合并后的结果(否则相对路径基准会错)。
    """
    if settings is None:
        return []
    includes, _excludes = split_resource_values(list(getattr(settings, key, None) or []))
    base = settings_scope_path(scope, cwd).parent
    paths: list[Path] = []
    for item in includes:
        for path in resolve_resource_paths(item, base):
            if path not in paths:
                paths.append(path)
    return paths


def settings_exclude_paths(settings: QiSettings | None, scope: str, key: str,
                           cwd: Path | None = None) -> list[Path]:
    """某作用域某资源字段的**排除**路径(`!pat` / `-pat`),同样各自基准解析。

    对齐 pi:排除项作用于**整个发现集**,而不只是数组里纳入的根 —— 所以默认目录
    (`~/.qi/agent/skills` 等)里扫出来的技能也能被单独关掉。
    """
    if settings is None:
        return []
    _includes, excludes = split_resource_values(list(getattr(settings, key, None) or []))
    base = settings_scope_path(scope, cwd).parent
    removed: list[Path] = []
    for item in excludes:
        for path in resolve_resource_paths(item, base):
            if path not in removed:
                removed.append(path)
    return removed


def default_tools(settings: QiSettings) -> list[str] | None:
    """`defaultTools` 规范化(None = 用框架默认)。"""
    if settings.defaultTools is None:
        return None
    return [str(t) for t in settings.defaultTools]


def project_trust_default(settings: QiSettings) -> str:
    """`defaultProjectTrust` 取值归一(ask/always/never);未知值按 ask。"""
    value = (settings.defaultProjectTrust or "ask").strip().lower()
    return value if value in ("ask", "always", "never") else "ask"


DOUBLE_ESCAPE_ACTIONS = ("tree", "fork", "none")
"""`doubleEscapeAction` 的合法取值(对齐 pi 的 settings-manager)。"""


def double_escape_action(settings: QiSettings | None) -> str:
    """空编辑器连按两次 escape 干什么:tree / fork / none。默认 tree(pi 同款)。"""
    value = (getattr(settings, "doubleEscapeAction", None) or "tree").strip().lower()
    return value if value in DOUBLE_ESCAPE_ACTIONS else "tree"


def session_dir(settings: QiSettings, cwd: Path | None = None,
                scopes: dict[str, QiSettings] | None = None) -> Path | None:
    """`sessionDir` 解析;项目优先于全局,各按自己 settings.json 所在目录解析。"""
    scopes = scopes if scopes is not None else load_settings_by_scope(cwd)
    for scope in ("project", "user"):
        raw = (getattr(scopes.get(scope), "sessionDir", None) or "").strip()
        if not raw:
            continue
        base = settings_scope_path(scope, cwd).parent
        resolved = resolve_resource_paths(raw, base)
        if resolved:
            return resolved[0]
        return (Path(raw).expanduser() if Path(raw).is_absolute()
                else base / raw).resolve()
    raw = (settings.sessionDir or "").strip()
    if not raw:
        return None
    base = settings_scope_path("user", cwd).parent
    resolved = resolve_resource_paths(raw, base)
    return resolved[0] if resolved else Path(raw).expanduser()
