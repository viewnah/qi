"""qi-lab 内置扩展的发现、外部覆盖判定与装载顺序。

内置扩展是**目录通道**扩展:每个目录里有 `extension.py`,运行期由 qi-lab 把目录作为
`extra_extension_paths` 传给 `QiRuntime`。它们不是独立的 pip distribution,所以不会出现
在 `pip list` / `qi list` 里;状态由 qi-lab 自己报告。

外部优先规则(见 `extensions/bundle.toml`):

* 如果同名扩展已经通过 `settings.packages` 声明、且对应 entry point 真的存在,
  跳过内置副本;
* 如果项目/用户级 `extensions/` 里有同名目录,跳过内置副本(项目级只在信任时算);
* 只是 pip 装了但没声明 → 不跳过。core 的声明门控不会加载它,跳过内置反而少功能。
"""

from __future__ import annotations

import importlib.metadata as metadata
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

BUNDLE_FILE = "bundle.toml"


@dataclass(frozen=True)
class BundledExtension:
    """`extensions/bundle.toml` 里的一条。"""

    key: str                  # 仓库目录名,如 qi-mcp
    path: Path                # 扩展目录(含 extension.py)
    extension: str            # core 里的扩展名(entry point 名),如 mcp
    dist: str                 # 将来独立发布时的发行名,如 qi-mcp
    requires: tuple[str, ...] = ()
    enabled: bool = True

    @property
    def available(self) -> bool:
        return self.path.is_dir() and (self.path / "extension.py").is_file()


def extensions_root() -> Path:
    """内置扩展根目录:装好的 wheel 优先,源码模式回落到仓库根。"""
    packaged = Path(__file__).resolve().parent / "extensions"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "extensions"


def load_bundle(root: Path | None = None) -> list[BundledExtension]:
    """读 `bundle.toml`。文件缺失/坏掉返回空表(启动提示由调用方给)。"""
    root = root or extensions_root()
    manifest = root / BUNDLE_FILE
    if not manifest.is_file():
        return []
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    out: list[BundledExtension] = []
    for key, raw in (data.get("extensions") or {}).items():
        if not isinstance(raw, dict):
            continue
        rel = str(raw.get("path") or key)
        out.append(BundledExtension(
            key=str(key),
            path=root / rel,
            extension=str(raw.get("extension") or key),
            dist=str(raw.get("dist") or key),
            requires=tuple(str(x) for x in (raw.get("requires") or ())),
            enabled=bool(raw.get("enabled", True)),
        ))
    return out


def _normalize(name: str) -> str:
    """PEP 503 归一(与 core 的 packages.normalize_name 同一规则)。"""
    import re

    return re.sub(r"[-_.]+", "-", name or "").strip().lower()


def project_trusted(cwd: Path | None = None) -> bool:
    """项目是否可信。判定失败时保守返回 False(与 core 的 fail-safe 一致)。"""
    try:
        from .core import TrustStore, load_settings_by_scope, resolve_project_trust

        scopes = load_settings_by_scope(cwd)
        return bool(resolve_project_trust(
            scopes.get("user"), stored=TrustStore().get(cwd or Path.cwd()))[0])
    except Exception:  # noqa: BLE001 信任判定失败不阻断启动,按未信任处理
        return False


def _declared_pip(cwd: Path | None, trusted: bool) -> dict[str, bool]:
    try:
        from .core import declared_pip_extensions

        return declared_pip_extensions(cwd, project_trusted=trusted) or {}
    except Exception:  # noqa: BLE001 core 版本差异 / 设置坏掉 → 不过滤
        return {}


def _entry_points_by_dist() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    try:
        eps = metadata.entry_points(group="qi.extensions")
    except Exception:  # noqa: BLE001
        return out
    for ep in eps:
        dist = getattr(ep, "dist", None)
        dist_name = getattr(dist, "name", None) if dist is not None else None
        if dist_name:
            out.setdefault(_normalize(dist_name), set()).add(ep.name)
    return out


def _dir_extension_names(cwd: Path | None, trusted: bool) -> set[str]:
    """项目/用户级目录通道里已有的扩展名(core 会先于 extra_dirs 加载它们)。"""
    from .core import paths

    names: set[str] = set()
    roots: list[Path] = []
    if trusted:
        roots.append(paths.project_home(cwd) / paths.EXTENSIONS_DIR_NAME)
    roots.append(paths.global_home() / paths.EXTENSIONS_DIR_NAME)
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.is_dir() and (child / "extension.py").is_file():
                names.add(child.name)
    return names


def _external_keys(cwd: Path | None, trusted: bool) -> set[str]:
    """这次运行中真的会被 core 加载的外部扩展(按 manifest key 表示)。"""
    declared = _declared_pip(cwd, trusted)
    eps = _entry_points_by_dist()
    dirs = _dir_extension_names(cwd, trusted)
    out: set[str] = set()
    for ext in load_bundle():
        dist = _normalize(ext.dist)
        pip_hit = bool(declared.get(dist)) and ext.extension in eps.get(dist, set())
        dir_hit = ext.key in dirs or ext.extension in dirs or ext.path.name in dirs
        if pip_hit or dir_hit:
            out.add(ext.key)
    return out


def _topo(exts: Iterable[BundledExtension]) -> list[BundledExtension]:
    """按 `requires` 排序;未知依赖忽略(不是内置扩展,由 core 自己解决)。"""
    items = list(exts)
    by_key = {e.key: e for e in items}
    out: list[BundledExtension] = []
    done: set[str] = set()
    visiting: set[str] = set()

    def visit(ext: BundledExtension) -> None:
        if ext.key in done:
            return
        if ext.key in visiting:
            raise ValueError(f"内置扩展依赖成环:{ext.key}")
        visiting.add(ext.key)
        for req in ext.requires:
            dep = by_key.get(req)
            if dep is not None:
                visit(dep)
        visiting.remove(ext.key)
        done.add(ext.key)
        out.append(ext)

    for ext in items:
        visit(ext)
    return out


def active_extensions(cwd: Path | None = None,
                      trusted: bool | None = None) -> list[BundledExtension]:
    """本次运行该由 qi-lab 加载的内置扩展(已过滤外部覆盖,已排序)。"""
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    trusted = project_trusted(cwd) if trusted is None else trusted
    external = _external_keys(cwd, trusted)
    candidates = [e for e in load_bundle()
                  if e.enabled and e.available and e.key not in external]
    try:
        return _topo(candidates)
    except ValueError:
        # 成环是 bundle.toml 的错;退回声明顺序,至少不让整个应用起不来。
        return candidates


def extension_paths(cwd: Path | None = None,
                    trusted: bool | None = None) -> list[Path]:
    """给 `QiRuntime(extra_extension_paths=…)` 的目录列表。"""
    return [e.path for e in active_extensions(cwd, trusted)]


def ensure_sys_path(cwd: Path | None = None,
                    trusted: bool | None = None) -> list[Path]:
    """让内置扩展的顶层包在 qi-lab 进程里可 import。

    目录通道的 loader 只在 exec `extension.py` 时临时插 `sys.path`;而 qi-lab 的
    `/api/meta`、适配层可能在 runtime 建立之前就 `import qi_store`。这里把**本次生效**的
    内置扩展目录常驻在 `sys.path` 前面 —— 被外部版本覆盖的扩展不会加进来,所以外部包
    仍然从 site-packages 解析。
    """
    paths = extension_paths(cwd, trusted)
    for path in reversed(paths):
        text = str(path)
        if text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)
    return paths


def status(cwd: Path | None = None,
           trusted: bool | None = None) -> list[dict[str, object]]:
    """给 `qi-lab doctor` / `/api/meta` 的报告:每个内置扩展是否生效、为什么。"""
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    trusted = project_trusted(cwd) if trusted is None else trusted
    external = _external_keys(cwd, trusted)
    active = {e.key for e in active_extensions(cwd, trusted)}
    out: list[dict[str, object]] = []
    for ext in load_bundle():
        if not ext.enabled:
            state = "disabled"
        elif not ext.available:
            state = "missing"
        elif ext.key in external:
            state = "external"
        elif ext.key in active:
            state = "bundled"
        else:
            state = "inactive"
        version = _dist_version(ext.dist)
        if version is None and state in ("bundled", "external"):
            version = _lab_version()
        out.append({
            "name": ext.key,
            "extension": ext.extension,
            "dist": ext.dist,
            "version": version,
            "state": state,
            "path": str(ext.path),
        })
    return out


def _lab_version() -> str:
    from . import __version__

    return __version__


def _dist_version(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return None
