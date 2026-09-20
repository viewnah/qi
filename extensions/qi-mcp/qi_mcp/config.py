"""mcp.json 的**两层**声明表(全局 → 项目,项目同名覆盖)。

格式只有一份真相(v1 起就没变,解析器也照搬过来 —— 它被测过):

```json
{"mcpServers": {"github": {"command": "npx", "args": ["-y", "…"]}}}
```

两层(与 v1 一致,勿改):

| 层 | 路径 | 说明 |
| --- | --- | --- |
| `global` | `~/.qi/agent/mcp.json` | 用户级基建,跨项目 |
| `project` | `<git 根>/.qi/mcp.json` | 跟仓库走,可提交共享 → **同名覆盖全局** |

**第三层(agent 私有 `<角色目录>/mcp.json`)不在本模块** —— E25 定的是由 **qi-agents** 读它、
再把定义**按值**交给 qi-mcp。这里只认 qi 的两层。

E25 也已定:**逐 server 的 `directTools` opt-in + 一个 `mcp` 代理工具做默认暴露**,
所以本模块只负责"声明表读出来 + 合并 + 标注来源层",不决定谁看见哪个 server。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

MCP_FILE = "mcp.json"
McpScope = Literal["global", "project"]

#: 本扩展认识的 server 字段。**不校验、只做提示** —— MCP 生态的字段还在长
#: (`lifecycle` / `oauth` / `caFile`…),把未知字段判死会让新 server 用不了。
KNOWN_FIELDS = (
    "command", "args", "env", "cwd", "url", "headers", "socket",
    "directTools", "includeTools", "excludeTools", "toolPrefix",
    "lifecycle", "idleTimeout", "disabled", "debug",
)


class ConfigError(Exception):
    """mcp.json 写坏了。**带上路径** —— 现场只看到一个 JSON 报错是没法修的。"""


@dataclass(frozen=True)
class ServerSpec:
    """一个 server 的声明 + 它从哪来(来源层是诊断与覆盖规则的关键)。"""

    name: str
    config: dict[str, Any] = field(default_factory=dict)
    scope: McpScope = "global"
    path: Path | None = None

    @property
    def disabled(self) -> bool:
        """只有**字面 JSON `true`** 才算关(`"true"` 字符串、数字 1 都不算 —— 照 pi,
        避免手写配置时的误判)。用 `isinstance(..., bool)` 而不是 `is True` 表达这层意思。
        """
        value = self.config.get("disabled")
        return isinstance(value, bool) and value

    @property
    def transport(self) -> str:
        """`stdio` / `http` / `socket`;都没有就是 `unknown`(声明不完整)。"""
        if self.config.get("command"):
            return "stdio"
        if self.config.get("url"):
            return "http"
        if self.config.get("socket"):
            return "socket"
        return "unknown"

    @property
    def direct_tools(self) -> bool | list[str]:
        """`true` / `false` / 工具名数组 —— 直连注册的开关(E25:默认走代理)。"""
        value = self.config.get("directTools", False)
        if isinstance(value, list):
            return [str(v) for v in value]
        return isinstance(value, bool) and value

    def summary(self) -> str:
        """一行描述(给 `/mcp` 面板与诊断看)。"""
        if self.transport == "stdio":
            cmd = " ".join([str(self.config.get("command", ""))]
                           + [str(a) for a in self.config.get("args", []) or []])
            return f"stdio: {cmd}".strip()
        if self.transport == "http":
            return f"http: {self.config.get('url')}"
        if self.transport == "socket":
            return f"socket: {self.config.get('socket')}"
        return "声明不完整(缺 command / url / socket)"


def _home(cwd: Path | None) -> tuple[Path, Path]:
    from qi_agent import paths

    return paths.global_home(), paths.project_home(cwd)


def scope_files(cwd: Path | None = None) -> list[tuple[McpScope, Path]]:
    """两层文件路径,**低 → 高**(项目覆盖全局)。文件存不存在都返回。"""
    global_home, project_home = _home(cwd)
    return [("global", global_home / MCP_FILE), ("project", project_home / MCP_FILE)]


def read_file(path: Path) -> dict[str, dict]:
    """解析一份 mcp.json → `{server 名: config}`。**文件必须存在**(调用方先探)。

    这里对坏文件是**严格**的(抛 `ConfigError`):一份写坏的 mcp.json 会让用户以为
    "我配了但没生效",而报错能直接指到文件。宽松留给上层——`load_layers` 把不存在的
    文件当空,那是"没配",不是"配错了"。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: JSON 解析失败 {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: 读取失败 {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: 顶层必须是对象")
    servers = raw.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ConfigError(f"{path}: mcpServers 必须是对象")
    out: dict[str, dict] = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            raise ConfigError(f"{path}: server `{name}` 的值必须是对象")
        out[str(name)] = cfg
    return out


def load_layers(cwd: Path | None = None) -> list[tuple[McpScope, Path, dict[str, dict]]]:
    """读两层。**不存在的文件也回**(三个值都在,servers 为空)。

    设置页/`/mcp` 要能区分"这里没文件"与"文件在但没写 server" ——
    否则它只能靠猜,而猜出来的东西用户没法核实。
    """
    return [(scope, path, read_file(path) if path.is_file() else {})
            for scope, path in scope_files(cwd)]


def resolve(cwd: Path | None = None) -> dict[str, ServerSpec]:
    """两层合并成一张按名查的表:**项目同名覆盖全局**(低 → 高写入)。

    被覆盖的那个不会出现在结果里 —— 覆盖是**合并的规矩**,不是"两个都留着";想诊断
    "谁盖了谁"用 `load_layers()` 拿逐层原样。
    """
    table: dict[str, ServerSpec] = {}
    for scope, path, servers in load_layers(cwd):
        for name, config in servers.items():
            table[name] = ServerSpec(name=name, config=config, scope=scope, path=path)
    return table


def active(cwd: Path | None = None) -> dict[str, ServerSpec]:
    """合并后**排除 disabled** 的那部分 —— 连接/注册只看这个。"""
    return {name: spec for name, spec in resolve(cwd).items() if not spec.disabled}


def unknown_fields(spec: ServerSpec) -> list[str]:
    """声明里 qi 不认识的字段(只提示,不拦)。"""
    return sorted(k for k in spec.config if k not in KNOWN_FIELDS)
