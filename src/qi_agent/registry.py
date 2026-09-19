"""注册表:ToolCatalog / AgentRegistry / 扩展能力注册表 + 扩展发现(docs/extensions.md)。"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .extensions import ExtensionApi, ExtensionBus
from .models import AgentUnit, ToolOutcome

# 扩展本地目录通道:目录含 extension.py,export register(api)
EXTENSION_ENTRY_FILE = "extension.py"
#: pip 通道的 entry point 组(v1 叫 `qi.plugins`;**cold cut,不留别名** —— 见 extensions.md E1)
EXTENSION_ENTRY_POINT_GROUP = "qi.extensions"

# 工具执行函数签名:async def execute(args, ctx) -> str | ToolOutcome
# 返回 str 即视为成功;需要上报 status/exit_code 的工具返回 ToolOutcome。
ToolExecutor = Callable[[dict, Any], Awaitable[str | ToolOutcome]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                        # JSON Schema
    execute: ToolExecutor                   # async def execute(args, ctx) -> str | ToolOutcome
    keywords: list[str] = field(default_factory=list)

    def to_llm_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolError(Exception):
    """工具执行错误:以结果文本返回给模型,不中断会话。"""


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
                        extra_dirs: Iterable[Path] | None = None,
                        project_trusted: bool = True) -> list[str]:
    """发现并装载扩展:目录通道 + entry points(`qi.extensions`)。返回扩展名列表。

    `bus` **必填**:扩展能力的一半是订阅事件,没总线的装载等于装了个哑巴
    (工具能注册、`on()` 却无处可去)—— 宁可在调用点报 TypeError,不要静默丢掉 handler。

    优先级(先到先得,同名跳过):项目 `.qi/extensions/` → 全局 `<agent>/extensions/`
    → `extra_dirs`(settings.json 的 `extensions[]`)→ entry points。

    `project_trusted=False` 时**不扫项目目录** —— 扩展是仓库控制的任意代码,
    未信任就不能执行(docs/extensions.md §5.3)。`extra_dirs` 由调用方负责:
    从**项目级** settings 解析出来的路径要自己在未信任时不传。
    """
    loaded: list[str] = []
    for name, load in _iter_extension_loaders(cwd, extra_dirs, project_trusted):
        try:
            api = ExtensionApi(catalog=catalog, bus=bus, _name=name)
            module = load()
            register = getattr(module, "register", None)
            if not callable(register):
                continue
            register(api)
            capabilities.merge(api)
            loaded.append(name)
        except Exception as exc:  # 扩展坏 → 启动报错(不静默)
            raise RuntimeError(f"扩展 {name} 装载失败: {exc}") from exc
    return loaded


def _iter_extension_loaders(cwd: Path | None,
                            extra_dirs: Iterable[Path] | None = None,
                            project_trusted: bool = True):
    """(name, loader) 生成器:项目目录 + 全局目录 + 附加目录 + entry point 通道。"""
    import importlib.metadata as metadata

    seen: set[str] = set()
    # 内建目录:项目那一档受信任门控 —— **未信任就不进循环**(而不是扫了再丢)
    builtin_roots: list[Path] = []
    if project_trusted:
        builtin_roots.append(paths.project_home(cwd) / paths.EXTENSIONS_DIR_NAME)
    builtin_roots.append(paths.global_home() / paths.EXTENSIONS_DIR_NAME)
    #: 内建目录固定是"父目录"(子目录 = 扩展);附加路径两种都行(见下)
    roots: list[tuple[Path, bool]] = [(r, False) for r in builtin_roots]
    roots += [(Path(p), True) for p in (extra_dirs or ())]
    for root, allow_self in roots:
        if not root.is_dir():
            continue
        # `settings.extensions` 的一条可以直接指向**单个扩展目录**(对齐 pi:
        # `"extensions": ["/path/to/local/extension.ts", "…/extension/dir"]`),
        # 也可以是指向若干扩展的父目录 —— 内建目录只取后者,避免
        # `.qi/extensions/extension.py` 被当成一个叫 "extensions" 的扩展。
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
            yield name, _make_file_loader(name, entry)
    # entry point 通道(pip 包)
    for ep in metadata.entry_points(group=EXTENSION_ENTRY_POINT_GROUP):
        name = ep.name
        if name in seen:
            continue
        seen.add(name)
        yield name, ep.load


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
