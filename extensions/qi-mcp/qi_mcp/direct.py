"""直连注册:`directTools` opt-in 的那些工具(命名 / 过滤 / 选择)。

E25 定的默认是**一个 `mcp` 代理工具**;直连是每 server 显式开的(pi 同款:一个 server 的工具
定义轻松 10k+ token,默认不该付这笔钱)。开着的时候要回答三个问题,这个模块只回答这三个:

1. **叫什么名字**(`tool_name`):默认 `mcp__<server>__<tool>`(E22),`toolPrefix` 可改;
2. **露出哪些**(`select_tools`):`includeTools` 白名单 → `excludeTools` 去掉(顺序照 pi:
   exclude 在 include 之后应用);
3. **过滤对谁生效**:对**整个 server 的可见工具集**生效 —— 不只是直连。否则过滤就是装饰:
   代理工具照样能搜到、调到被过滤掉的工具。

**匹配口径**(照 pi):模式既匹配 server 声明的**原名**,也匹配**生成后**的名字
(`mcp__gh__create_issue`),所以 `gh__*`、`mcp__gh__*`、`create_*` 三种写法都能用。
"""

from __future__ import annotations

from fnmatch import fnmatch

from .config import ServerSpec
from .servers import ToolInfo, qualified

#: 支持的前缀模式。`mcp` 是默认(E22),`server` 短一截,`none` 用原名(**可能撞名**)。
PREFIX_MODES = ("mcp", "server", "none")
DEFAULT_PREFIX_MODE = "mcp"


def prefix_mode(spec: ServerSpec) -> str:
    """读 `toolPrefix`。不认识的值**不报错**,记成默认值 —— 一个手滑的字符串不该让
    server 直接不可用(与 `requestTimeoutMs` 同一条原则)。返回值一律落在 `PREFIX_MODES` 里。
    """
    raw = spec.config.get("toolPrefix")
    return raw if isinstance(raw, str) and raw in PREFIX_MODES else DEFAULT_PREFIX_MODE


def tool_name(server: str, tool: str, mode: str = DEFAULT_PREFIX_MODE) -> str:
    """按模式生成工具名。`none` 下直接用原名(撞名风险由调用方处理)。"""
    if mode == "none":
        return tool
    if mode == "server":
        return f"{server}__{tool}"
    return qualified(server, tool)


def all_names(info: ToolInfo) -> list[str]:
    """一个工具的各种可能写法 —— 过滤与查名都按这一组匹配(照 pi:原名与生成名都算)。"""
    return [info.name, info.qualified, f"{info.server}__{info.name}"]


def _matches(pattern: str, info: ToolInfo) -> bool:
    return any(fnmatch(name, pattern) for name in all_names(info))


def _as_patterns(raw: object) -> list[str]:
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(v) for v in raw if str(v).strip()]
    return []


def select_tools(infos: list[ToolInfo], spec: ServerSpec) -> list[ToolInfo]:
    """`includeTools` → `excludeTools`(按这个顺序)。两个都没写就是全部。"""
    include = _as_patterns(spec.config.get("includeTools"))
    exclude = _as_patterns(spec.config.get("excludeTools"))
    chosen = [i for i in infos if any(_matches(p, i) for p in include)] if include else list(infos)
    if exclude:
        chosen = [i for i in chosen if not any(_matches(p, i) for p in exclude)]
    return chosen


def direct_selection(infos: list[ToolInfo], spec: ServerSpec) -> list[ToolInfo]:
    """这个 server 要**直连**注册的那部分(已经过 `select_tools`)。

    - `directTools: false`(默认)→ 空:只走代理;
    - `true` → 过滤后的全部;
    - `["create_*", "mcp__gh__list_issues"]` → 其中匹配的那些(同样按原名/生成名匹配;
      模式没命中任何工具时**不报错**,只是不注册 —— 名字写错由 `/mcp` 面板与 note 反映)。

    取值统一走 `spec.direct_tools`(那里已经把 `true` / 数组 / 其余 三分开),不在这里重新解析 ——
    解析规则只有一处才好改。
    """
    selected = select_tools(infos, spec)
    wanted = spec.direct_tools
    if isinstance(wanted, bool):
        return selected if wanted else []
    patterns = _as_patterns(wanted)
    return [i for i in selected if any(_matches(p, i) for p in patterns)] if patterns else []
