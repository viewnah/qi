"""a2a.json 的**两层**声明表(全局 → 项目,项目同名深合并)。

一个条目通常就是**一个智能体**(一张 Agent Card);写列表卡时它是网关。

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/a2a.schema.json",
  "a2aServers": {
    "hikqin-analysis": {
      "cardUrl": "http://127.0.0.1:8080/a2a/agents/agent-3fa85c64/card.json"
    }
  }
}
```

| 层 | 路径 | 说明 |
| --- | --- | --- |
| `global` | `~/.qi/agent/a2a.json` | 用户级基建,跨项目 |
| `project` | `<cwd>/.qi/a2a.json` | 跟仓库走,可提交共享 → 同名**深合并**到全局 |

三种等价写法(都指向一个可调用端点):

| 写法 | RPC url | tenant |
| --- | --- | --- |
| `cardUrl` | 卡片里的 `supportedInterfaces[0].url` | 卡片里的 `tenant` |
| `baseUrl` + `tenant` | `{baseUrl}/a2a` | 配置 |
| `baseUrl` | `{baseUrl}/a2a` | 调用时用 `agent` 选 |

**深合并**:嵌套对象递归合并、数组整体替换、`null` 删除键。项目层写
`{"disabled": true}` 就能关掉全局的同名条目 —— 不用把整段配置抄一遍。

**只有字面 JSON `true` 才算 disabled**(照 qi-mcp:`"true"` 字符串、数字 1 都不算)。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

A2A_FILE = "a2a.json"
SERVERS_KEY = "a2aServers"

EntryScope = Literal["global", "project"]

#: 本扩展**真的会读**的字段。不在表里的字段会被 `/a2a` 标成“未识别”(只提示,不拦)——
#: A2A 生态的字段还在长,把未知字段判死会让新网关用不了。
KNOWN_FIELDS = (
    "cardUrl", "rpcUrl", "baseUrl", "tenant", "description",
    "headers", "timeoutMs", "streaming", "discovery",
    "directTools", "toolPrefix", "disabled",
)

DISCOVERY_MODES = ("auto", "single", "list")

DEFAULT_TIMEOUT_MS = 600_000          # 10 分钟:长任务(数据分析)常常跑很久
DEFAULT_TOOL_PREFIX = "a2a"

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """a2a.json 写坏了。**带上路径/条目名** —— 现场只看到一个 JSON 报错是没法修的。"""


def _home(cwd: Path | None) -> tuple[Path, Path]:
    from qi_agent import paths

    return paths.global_home(), paths.project_home(cwd)


def scope_files(cwd: Path | None = None) -> list[tuple[EntryScope, Path]]:
    """两层文件路径,**低 → 高**(项目覆盖全局)。文件存不存在都返回。"""
    global_home, project_home = _home(cwd)
    return [("global", global_home / A2A_FILE), ("project", project_home / A2A_FILE)]


def read_file(path: Path) -> dict[str, dict]:
    """解析一份 a2a.json → `{条目名: config}`。**文件必须存在**(调用方先探)。

    这里对坏文件是**严格**的(抛 `ConfigError`):一份写坏的 a2a.json 会让用户以为
    “我配了但没生效”,而报错能直接指到文件。不存在的文件由 `load_layers` 当空 ——
    那是“没配”,不是“配错了”。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: JSON 解析失败 {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: 读取失败 {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: 顶层必须是对象")
    servers = raw.get(SERVERS_KEY, {})
    if not isinstance(servers, dict):
        raise ConfigError(f"{path}: {SERVERS_KEY} 必须是对象")
    out: dict[str, dict] = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            raise ConfigError(f"{path}: 条目 `{name}` 的值必须是对象")
        out[str(name)] = cfg
    return out


def load_layers(cwd: Path | None = None) -> list[tuple[EntryScope, Path, dict[str, dict]]]:
    """读两层,**不展开环境变量**(诊断/面板要看原样)。不存在的文件也回三个值。"""
    return [(scope, path, read_file(path) if path.is_file() else {})
            for scope, path in scope_files(cwd)]


def merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """键级深合并:嵌套 dict 递归;数组整体替换;`null` 删除键。"""
    out = dict(base)
    for key, value in override.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_config(out[key], value)
        else:
            out[key] = value
    return out


def _expand(value: Any, field_name: str, entry_name: str) -> Any:
    """递归展开 `${VAR}`;缺变量 → ConfigError(带条目与字段)。"""
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(
                    f"a2aServers.{entry_name}.{field_name}: 环境变量 {name} 未设置")
            return os.environ[name]

        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_expand(v, field_name, entry_name) for v in value]
    if isinstance(value, dict):
        return {str(k): _expand(v, f"{field_name}.{k}" if field_name else str(k), entry_name)
                for k, v in value.items()}
    return value


def _check_url(value: Any, field_name: str, entry_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"a2aServers.{entry_name}.{field_name}: 需要一个非空 URL")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(
            f"a2aServers.{entry_name}.{field_name}: 只支持 http/https,收到 {value!r}")


def validate(name: str, cfg: dict[str, Any], path: Path | None) -> None:
    """校验一个**已展开**的条目。未知字段不拦(`/a2a` 会提示)。"""
    where = f"{path}: " if path else ""

    def fail(message: str) -> None:
        raise ConfigError(f"{where}a2aServers.{name}: {message}")

    for key in ("baseUrl", "rpcUrl", "cardUrl"):
        if key in cfg:
            _check_url(cfg[key], key, name)

    # 关掉的条目允许“先占位、后填地址”;没关就必须至少能定位到一个端点。
    if not any(cfg.get(k) for k in ("baseUrl", "rpcUrl", "cardUrl")):
        if not (isinstance(cfg.get("disabled"), bool) and cfg["disabled"]):
            fail("缺少 baseUrl / rpcUrl / cardUrl 之一")

    tenant = cfg.get("tenant")
    if tenant is not None and not isinstance(tenant, str):
        fail("tenant 必须是字符串")

    description = cfg.get("description")
    if description is not None and not isinstance(description, str):
        fail("description 必须是字符串")

    headers = cfg.get("headers")
    if headers is not None:
        if not isinstance(headers, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
            fail("headers 必须是 {字符串: 字符串}")

    timeout = cfg.get("timeoutMs")
    if timeout is not None and (not isinstance(timeout, int) or isinstance(timeout, bool)
                                or timeout <= 0):
        fail("timeoutMs 必须是正整数")

    streaming = cfg.get("streaming")
    if streaming is not None and not isinstance(streaming, bool):
        fail("streaming 必须是布尔值")

    discovery = cfg.get("discovery")
    if discovery is not None and discovery not in DISCOVERY_MODES:
        fail(f"discovery 只能是 {' / '.join(DISCOVERY_MODES)}")

    direct = cfg.get("directTools")
    if direct is not None and not (
            isinstance(direct, bool)
            or (isinstance(direct, list) and all(isinstance(v, str) for v in direct))):
        fail("directTools 必须是布尔值或字符串数组")

    prefix = cfg.get("toolPrefix")
    if prefix is not None and (not isinstance(prefix, str) or not prefix.strip()):
        fail("toolPrefix 必须是非空字符串")


@dataclass(frozen=True)
class A2AEntry:
    """一个条目 + 它从哪来(来源层是诊断与覆盖规则的关键)。"""

    name: str
    config: dict[str, Any] = field(default_factory=dict)
    scope: EntryScope = "global"
    path: Path | None = None

    @property
    def disabled(self) -> bool:
        value = self.config.get("disabled")
        return isinstance(value, bool) and value

    @property
    def tenant(self) -> str:
        value = self.config.get("tenant")
        return value.strip() if isinstance(value, str) else ""

    @property
    def description(self) -> str:
        value = self.config.get("description")
        return value.strip() if isinstance(value, str) else ""

    @property
    def base_url(self) -> str:
        value = self.config.get("baseUrl")
        return value.rstrip("/") if isinstance(value, str) else ""

    @property
    def card_url(self) -> str:
        value = self.config.get("cardUrl")
        return value if isinstance(value, str) else ""

    @property
    def rpc_url(self) -> str:
        value = self.config.get("rpcUrl")
        return value if isinstance(value, str) else ""

    @property
    def headers(self) -> dict[str, str]:
        value = self.config.get("headers")
        return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}

    @property
    def timeout_ms(self) -> int:
        value = self.config.get("timeoutMs")
        return value if isinstance(value, int) and not isinstance(value, bool) else DEFAULT_TIMEOUT_MS

    @property
    def timeout(self) -> float:
        return self.timeout_ms / 1000.0

    @property
    def streaming(self) -> bool:
        value = self.config.get("streaming")
        return True if value is None else bool(value)

    @property
    def discovery(self) -> str:
        value = self.config.get("discovery")
        return value if value in DISCOVERY_MODES else "auto"

    @property
    def direct_tools(self) -> bool | list[str]:
        value = self.config.get("directTools", False)
        if isinstance(value, list):
            return [str(v) for v in value]
        return isinstance(value, bool) and value

    @property
    def tool_prefix(self) -> str:
        value = self.config.get("toolPrefix")
        return value.strip() if isinstance(value, str) and value.strip() else DEFAULT_TOOL_PREFIX

    def wants_direct(self) -> bool:
        """这个条目要不要注册直连工具(`directTools` 的三种取值)。"""
        value = self.direct_tools
        return bool(value) if not isinstance(value, list) else self.name in value

    def summary(self) -> str:
        """一行描述(给 `/a2a` 面板与错误信息看)。"""
        if self.card_url:
            return f"card: {self.card_url}"
        if self.base_url:
            if self.tenant:
                return f"tenant {self.tenant} @ {self.base_url}"
            return f"gateway {self.base_url}"
        if self.rpc_url:
            return f"rpc: {self.rpc_url}"
        return "声明不完整(缺 baseUrl / rpcUrl / cardUrl)"


def resolve(cwd: Path | None = None, *, trusted: bool = True) -> dict[str, A2AEntry]:
    """两层合并 + 展开环境变量 + 校验,返回按名查的表。

    项目层未受信任时**整层跳过** —— 项目里的 a2a.json 能把对话内容指向任意端点,
    风险与项目级扩展同级。
    """
    merged: dict[str, tuple[dict[str, Any], EntryScope, Path]] = {}
    for scope, path, servers in load_layers(cwd):
        if scope == "project" and not trusted:
            continue
        for name, cfg in servers.items():
            previous = merged.get(name)
            combined = merge_config(previous[0], cfg) if previous else dict(cfg)
            merged[name] = (combined, scope, path)

    out: dict[str, A2AEntry] = {}
    for name, (cfg, scope, path) in merged.items():
        expanded = _expand(cfg, "", name)
        validate(name, expanded, path)
        out[name] = A2AEntry(name=name, config=expanded, scope=scope, path=path)
    return out


def active(cwd: Path | None = None, *, trusted: bool = True) -> dict[str, A2AEntry]:
    """合并后**排除 disabled** 的那部分 —— 工具与直连注册只看这个。"""
    return {name: entry for name, entry in resolve(cwd, trusted=trusted).items()
            if not entry.disabled}


def unknown_fields(entry: A2AEntry) -> list[str]:
    """声明里本扩展不认识的字段(只提示,不拦)。"""
    return sorted(k for k in entry.config if k not in KNOWN_FIELDS)


def entry_tool_name(entry: A2AEntry) -> str:
    """直连工具名:`<prefix>__<条目>`,非标识符字符收成 `_`。"""
    safe = re.sub(r"[^0-9A-Za-z_]+", "_", entry.name).strip("_") or "entry"
    return f"{entry.tool_prefix}__{safe}"
