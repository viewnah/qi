"""注册表:ToolCatalog / AgentRegistry / 插件能力注册表 + 插件发现(plugins.md)。"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .models import AgentUnit

# 插件本地目录通道:目录含 plugin.py,export register(api)
PLUGIN_ENTRY_FILE = "plugin.py"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                        # JSON Schema
    execute: object                         # async def execute(args, ctx) -> str
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


# ── 插件能力注册表(plugins.md:provides_config / provides_types)──────────────

@dataclass
class PluginApi:
    """传给插件 register(api) 的接口。"""

    catalog: ToolCatalog
    _config_kinds: set[str] = field(default_factory=set)
    _types: dict[str, set[str]] = field(default_factory=dict)
    _name: str = ""

    def add_tool(self, tool: Tool) -> None:
        self.catalog.register(tool)

    def provides_config(self, kind: str, types: list[str] | None = None) -> None:
        """声明消费的 agent 配置种类;types 为该种类支持的 type 值(如数据源 mysql/...)。"""
        self._config_kinds.add(kind)
        if types:
            self._types.setdefault(kind, set()).update(types)

    @property
    def config_kinds(self) -> set[str]:
        return self._config_kinds


class CapabilityRegistry:
    """汇总所有已发现插件的消费型配置能力(动态装载门控)。"""

    def __init__(self) -> None:
        self._providers: dict[str, set[str]] = {}    # kind -> {plugin_name}
        self._types: dict[str, set[str]] = {}        # kind -> {type…}

    def merge(self, api: PluginApi) -> None:
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


def discover_plugins(catalog: ToolCatalog, capabilities: CapabilityRegistry,
                     cwd: Path | None = None) -> list[str]:
    """发现并装载插件:entry points(qi.plugins)+ 本地目录双通道。返回插件名列表。"""
    loaded: list[str] = []
    for name, load in _iter_plugin_loaders(cwd):
        try:
            api = PluginApi(catalog=catalog, _name=name)
            module = load()
            register = getattr(module, "register", None)
            if not callable(register):
                continue
            register(api)
            capabilities.merge(api)
            loaded.append(name)
        except Exception as exc:  # 插件坏 → 启动报错(不静默)
            raise RuntimeError(f"插件 {name} 装载失败: {exc}") from exc
    return loaded


def _iter_plugin_loaders(cwd: Path | None):
    """(name, loader) 生成器:目录通道 + entry point 通道。"""
    import importlib.metadata as metadata

    seen: set[str] = set()
    # 目录通道:项目/全局 .qi/plugins/<name>/plugin.py
    for qi_home in (paths.project_home(cwd), paths.global_home()):
        plugins_dir = qi_home / "plugins"
        if not plugins_dir.is_dir():
            continue
        for child in sorted(plugins_dir.iterdir()):
            entry = child / PLUGIN_ENTRY_FILE
            if child.is_dir() and entry.is_file():
                name = child.name
                if name in seen:
                    continue
                seen.add(name)
                yield name, _make_file_loader(name, entry)
    # entry point 通道(pip 包)
    for ep in metadata.entry_points(group="qi.plugins"):
        name = ep.name
        if name in seen:
            continue
        seen.add(name)
        yield name, ep.load


def _make_file_loader(name: str, entry: Path):
    def loader():
        spec = importlib.util.spec_from_file_location(f"qi_plugin_{name}", entry)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        # 让插件能 import 兄弟模块(轻量目录通道支持)
        sys.path.insert(0, str(entry.parent))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(entry.parent))
        return module

    return loader
