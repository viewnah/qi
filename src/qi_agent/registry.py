"""注册表:ToolCatalog / AgentRegistry / 扩展能力注册表 + 扩展发现(docs/extensions.md)。

`Tool` / `ToolError` / `ToolExecutor` 在 P-E2a 搬到了 `extensions.py`(扩展宿主的公开面);
这里只**转发导入**,旧写法 `from qi_agent.registry import Tool` 仍然有效。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths
from .extensions import (
    ExtensionApi,
    ExtensionBus,
    Tool,
    ToolError,
    ToolExecutor,
)
from .models import AgentUnit
__all__ = [
    "AgentRegistry",
    "CapabilityRegistry",
    "EXTENSION_ENTRY_FILE",
    "EXTENSION_ENTRY_POINT_GROUP",
    "Tool",
    "ToolCatalog",
    "ToolError",
    "ToolExecutor",
    "discover_extensions",
]

# 扩展本地目录通道:目录含 extension.py,export register(api)
EXTENSION_ENTRY_FILE = "extension.py"
#: pip 通道的 entry point 组(v1 叫 `qi.plugins`;**cold cut,不留别名** —— 见 extensions.md E1)
EXTENSION_ENTRY_POINT_GROUP = "qi.extensions"
#: 宿主自己的 distribution 名(禁写进扩展的 dependencies —— 见 §5.5)
HOST_DISTRIBUTION = "qi-agent"

# `packaging` 是可选依赖:它在绝大多数环境里经由 setuptools/pip/pytest 存在,
# 但 qi 自己不依赖它 —— 拿不到就只报告原始 spec,不猜“满不满足”。
try:  # pragma: no cover - 取决于环境
    from packaging.requirements import Requirement as _Requirement
    from packaging.version import Version as _Version
    _HAS_PACKAGING = True
except Exception:  # noqa: BLE001
    _Requirement = None            # type: ignore[assignment]
    _Version = None                # type: ignore[assignment]
    _HAS_PACKAGING = False

_LEADING_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalize_dist_name(name: str) -> str:
    """PEP 503 归一:大小写与 `-_.` 都视为等价。"""
    return re.sub(r"[-_.]+", "-", name or "").strip().lower()


def _requirement_targets_host(req: str) -> bool:
    """这条依赖是否指向宿主(不借助 packaging 也能判定 —— 它只影响诊断)。"""
    name = ""
    if _Requirement is not None:
        try:
            name = _Requirement(req).name
        except Exception:  # noqa: BLE001 坏 spec 不拖垮装载
            name = ""
    if not name:
        match = _LEADING_NAME.match(req)
        name = match.group(1) if match else ""
    return _normalize_dist_name(name) == _normalize_dist_name(HOST_DISTRIBUTION)


def installed_host_version() -> str | None:
    """当前装着的 qi-agent 版本(拿不到就 None)。"""
    import importlib.metadata as metadata

    try:
        return metadata.version(HOST_DISTRIBUTION)
    except Exception:  # noqa: BLE001 包元数据缺失(源码直跑等)
        return None


def _specifier_allows(reqs: list[str], version: str | None) -> bool | None:
    """这些依赖约束是否被当前版本满足;None = 判定不了(没 packaging/没版本)。"""
    if not _HAS_PACKAGING or version is None:
        return None
    try:
        parsed = _Version(version)          # type: ignore[misc]
        return all(parsed in _Requirement(r).specifier for r in reqs)  # type: ignore[misc]
    except Exception:  # noqa: BLE001
        return None


def warn_host_dependency(name: str, dist: Any,
                         report: Callable[[str], None] | None) -> None:
    """报告“扩展把宿主写进了依赖”这件亊(§5.5 / E11)。

    为什么不静默:pi 用 `peerDependencies` + `"*"` 表达“宿主提供、别自己打包”,
    而 Python **没有** peer 这个概念 —— 扩展一旦写 `qi-agent==0.1.0`,pip 就会在解析时
    把我们自己的 qi 降级(宿主被自己的扩展踢掉)。这类故障现场和根因隔得很远,
    所以到了装完才报就晚了;装载时就指出。

    **只报告不拒绝**:声明它本身不危险(危险的是被 pip 解成一棵冲突的树),
    拒载会让一个本来能跑的扩展直接不可用 —— 而用户此刻需要的是“知道并去改 pyproject”。
    """
    if report is None or dist is None:
        return
    requires = [str(r) for r in (getattr(dist, "requires", None) or ())]
    host_specs = [r for r in requires if _requirement_targets_host(r)]
    if not host_specs:
        return
    installed = installed_host_version()
    allowed = _specifier_allows(host_specs, installed)
    verdict = ("" if allowed is None else
               "当前版本**满足**它" if allowed else "当前版本**不满足**它")
    detail = "" if allowed is not None else "(未能判定是否满足:缺 packaging 或版本信息)"
    report(f"扩展 {name} 把宿主 {HOST_DISTRIBUTION} 写进了依赖:{'、'.join(host_specs)};"
           f"当前 {HOST_DISTRIBUTION} {installed or '未知'}。{verdict}{detail} "
           "宿主应当由 qi 自己提供,不该出现在扩展的 dependencies 里"
           "(否则 pip 可能把 qi 自己降级)—— 请从 pyproject 里去掉。")


class ToolCatalog:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def names(self) -> set[str]:
        return set(self._tools)

    def all(self) -> list[Tool]:
        """所有已注册工具(按名排序)。`getAllTools()` 与诊断面用它。"""
        return [self._tools[n] for n in sorted(self._tools)]

    def resolve(self, names: list[str]) -> list[Tool]:
        return [self._tools[n] for n in names]

    def llm_schemas(self, names: list[str] | None = None) -> list[dict]:
        source = [self._tools[n] for n in names] if names is not None else list(self._tools.values())
        return [t.to_llm_schema() for t in source]


class AgentRegistry:
    """装载后的 agent 集合(项目版已覆盖全局版)。"""

    def __init__(self) -> None:
        self._agents: dict[str, AgentUnit] = {}

    def register_all(self, units: dict[str, AgentUnit]) -> None:
        self._agents = dict(units)

    def get(self, name: str) -> AgentUnit | None:
        return self._agents.get(name)

    @property
    def names(self) -> list[str]:
        return sorted(self._agents)

    def all(self) -> list[AgentUnit]:
        return [self._agents[n] for n in self.names]


# ── 扩展能力注册表(docs/extensions.md:provides_config)──────────────────────
# `ExtensionApi` 现在住在 extensions.py(扩展宿主的公开面);这里只做转发导入,
# 旧的 `from qi_agent.registry import ExtensionApi` 写法仍然有效。


class CapabilityRegistry:
    """汇总所有已发现扩展的消费型配置能力(动态装载门控)。"""

    def __init__(self) -> None:
        self._providers: dict[str, set[str]] = {}    # kind -> {extension_name}
        self._types: dict[str, set[str]] = {}        # kind -> {type…}

    def merge(self, api: ExtensionApi) -> None:
        for kind in api.config_kinds:
            self._providers.setdefault(kind, set()).add(api._name)
            if kind in api._types:
                self._types.setdefault(kind, set()).update(api._types[kind])

    def has_provider(self, kind: str) -> bool:
        return kind in self._providers and bool(self._providers[kind])

    def types(self, kind: str) -> set[str]:
        return set(self._types.get(kind, ()))

    @property
    def kinds(self) -> list[str]:
        return sorted(self._providers)


def discover_extensions(catalog: ToolCatalog, capabilities: CapabilityRegistry,
                        cwd: Path | None = None, *,
                        bus: ExtensionBus,
                        host: Any = None,
                        on_warning: Callable[[str], None] | None = None,
                        extra_dirs: Iterable[Path | tuple[Path, str]] | None = None,
                        project_trusted: bool = True) -> list[str]:
    """发现并装载扩展:目录通道 + entry points(`qi.extensions`)。返回扩展名列表。

    `bus` **必填**:扩展能力的一半是订阅事件,没总线的装载等于装了个哑巴
    (工具能注册、`on()` 却无处可去)—— 宁可在调用点报 TypeError,不要静默丢掉 handler。

    `host` 是工具集读写面(`setActiveTools` / `getActiveTools` 的后端)。**选填**:
    没给时 `getActiveTools` 退回“catalog 里的全部”,而 `setActiveTools` 会**报错**
    (不是静默无效)—— 失败看得见,所以不必像 bus 那样强制。

    `on_warning` 收“装上了但有隐患”的报告(目前只有一件:扩展把宿主写进了依赖)。
    它**不阻止装载** —— 那类问题的现场在 pip 的解析结果里,不在这一行,所以要做的是
    让用户知道,而不是把扩展判死。

    优先级(先到先得,同名跳过):项目 `.qi/extensions/` → 全局 `<agent>/extensions/`
    → `extra_dirs`(settings.json 的 `extensions[]`)→ entry points。

    `extra_dirs` 每项可以是 `Path`(作用域记 `temporary`)或 `(Path, scope)`;
    `scope` 就是 `source_info.scope`,用于 `getAllTools()` 过滤与诊断。

    `project_trusted=False` 时**不扫项目目录** —— 扩展是仓库控制的任意代码,
    未信任就不能执行(docs/extensions.md §5.3)。`extra_dirs` 由调用方负责:
    从**项目级** settings 解析出来的路径要自己在未信任时不传。
    """
    loaded: list[str] = []
    for name, load, origin in _iter_extension_loaders(cwd, extra_dirs, project_trusted):
        try:
            api = ExtensionApi(catalog=catalog, bus=bus, _name=name,
                               _path=origin["path"], _scope=origin["scope"],
                               _origin=origin["origin"], _host=host)
            module = load()
            register = getattr(module, "register", None)
            if not callable(register):
                continue
            register(api)
            capabilities.merge(api)
            warn_host_dependency(name, origin.get("dist"), on_warning)
            loaded.append(name)
        except Exception as exc:  # 扩展坏 → 启动报错(不静默)
            raise RuntimeError(f"扩展 {name} 装载失败: {exc}") from exc
    return loaded


def _normalize_extra(extra_dirs: Iterable[Path | tuple[Path, str]] | None
                     ) -> list[tuple[Path, str]]:
    """附加路径归一:`(path, scope)` 原样过,裸 `Path` 记 `temporary`。

    允许裸 `Path` 是为了 `qi -e ./my-ext` 这种一次性试用能写成一行。
    """
    out: list[tuple[Path, str]] = []
    for item in extra_dirs or ():
        if isinstance(item, tuple):
            path, scope = item
            out.append((Path(path), scope))
        else:
            out.append((Path(item), "temporary"))
    return out


def _iter_extension_loaders(cwd: Path | None,
                            extra_dirs: Iterable[Path | tuple[Path, str]] | None = None,
                            project_trusted: bool = True):
    """`(name, loader, origin)` 生成器:项目目录 + 全局目录 + 附加目录 + entry point。

    `origin` = `{path, scope, origin}`,直接进工具的 `source_info` —— 所以
    “这个工具是谁装的、从哪来的”在注册那一刻就固定了,不靠事后回查。
    """
    import importlib.metadata as metadata

    seen: set[str] = set()
    # (目录, 是否允许该目录本身就是扩展, scope)。内建目录:项目那一档受信任门控 ——
    # **未信任就不进循环**(而不是扫了再丢)
    roots: list[tuple[Path, bool, str]] = []
    if project_trusted:
        roots.append((paths.project_home(cwd) / paths.EXTENSIONS_DIR_NAME, False, "project"))
    roots.append((paths.global_home() / paths.EXTENSIONS_DIR_NAME, False, "user"))
    # 附加路径两种都行(见下):可以是父目录,也可以直接是一个扩展目录
    roots += [(path, True, scope) for path, scope in _normalize_extra(extra_dirs)]
    for root, allow_self, scope in roots:
        if not root.is_dir():
            continue
        # `settings.extensions` 的一条可以直接指向**单个扩展目录**(对齐 pi:
        # `"extensions": ["/path/to/local/extension.ts", "…/extension/dir"]`)。
        # 内建目录只取后者,避免 `.qi/extensions/extension.py` 被当成一个叫
        # "extensions" 的扩展。
        if allow_self and (root / EXTENSION_ENTRY_FILE).is_file():
            children: list[Path] = [root]
        else:
            children = sorted(root.iterdir())
        for child in children:
            entry = child / EXTENSION_ENTRY_FILE
            if not (child.is_dir() and entry.is_file()):
                continue
            name = child.name
            if name in seen:
                continue
            seen.add(name)
            yield name, _make_file_loader(name, entry), {
                "path": str(entry), "scope": scope, "origin": "top-level"}    # entry point 通道(pip 包)
    for ep in metadata.entry_points(group=EXTENSION_ENTRY_POINT_GROUP):
        name = ep.name
        if name in seen:
            continue
        seen.add(name)
        # 包通道的来源靠 distribution 定位;拿不到就留空(dist_info 缺失等)
        dist = getattr(ep, "dist", None)
        path = str(getattr(dist, "_path", "") or "") if dist is not None else ""
        yield name, ep.load, {"path": path, "scope": "user", "origin": "package",
                              "dist": dist}


def _make_file_loader(name: str, entry: Path):
    def loader():
        spec = importlib.util.spec_from_file_location(f"qi_extension_{name}", entry)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        # 让扩展能 import 兄弟模块(轻量目录通道支持)
        sys.path.insert(0, str(entry.parent))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(entry.parent))
        return module

    return loader
