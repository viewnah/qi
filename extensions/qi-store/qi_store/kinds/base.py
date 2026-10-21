"""kind 适配器的公共部分:作用域根、命名约定、skill 包的结构校验。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..client import StoreClient
from ..errors import UsageError

SKILL_FILE = "SKILL.md"
#: 从 zip 里找 SKILL.md 的下钻层数(技能仓库常见的 `skills/<名>/SKILL.md` 布局)
MAX_LOCATE_DEPTH = 3
#: 解压防御:成员数与解压后总字节数(zip bomb 的两条腿;与 qi-lab 同一档)
MAX_ZIP_MEMBERS = 200
MAX_ZIP_BYTES = 8 * 1024 * 1024


@dataclass
class PlanContext:
    """一次 plan 需要的全部上下文。`client=None` 表示离线(只能列,不能取货)。"""

    root: Path                      # 作用域根:全局 agent 目录 或 <cwd>/.qi
    scope: str                      # global | project
    store_name: str
    base_url: str
    client: StoreClient | None = None
    name_override: str | None = None

    def skills_root(self) -> Path:
        return self.root / "skills"

    def mcp_file(self) -> Path:
        return self.root / "mcp.json"

    def a2a_file(self) -> Path:
        return self.root / "a2a.json"

    def require_client(self) -> StoreClient:
        if self.client is None:
            raise UsageError("离线模式取不到内容", hint="去掉 --offline,或先在线跑一次")
        return self.client


def env_name(name: str, suffix: str = "TOKEN") -> str:
    """`ppt-tools` + `TOKEN` → `PPT_TOOLS_TOKEN`。

    名字开头的数字不是合法 shell 变量名,补 `MCP_` 前缀 —— 生成的这一行是要被
    `export` 的,静默生成一个没法 export 的名字比不生成更坏。
    """
    base = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    if not base or base[0].isdigit():
        base = "MCP_" + base
    return f"{base}_{suffix}"


def check_name(name: str, what: str) -> str:
    """落盘名:不许空、不许路径分隔符、不许 `..`。"""
    cleaned = name.strip()
    if not cleaned:
        raise UsageError(f"{what}名不能为空")
    if any(ch in cleaned for ch in ("/", "\\")) or cleaned in (".", ".."):
        raise UsageError(f"{what}名不能含路径分隔符:{name!r}")
    return cleaned


def read_frontmatter(text: str) -> dict[str, Any]:
    """读 `---` 之间的 frontmatter。解析不了就返回 `{}`(不因为一个坏文件炸掉整个安装)。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    block = text[3:end]
    try:
        import yaml

        data = yaml.safe_load(block)
    except Exception:                                # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}
