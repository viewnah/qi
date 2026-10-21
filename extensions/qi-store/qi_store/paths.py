"""qi-store 的文件位置。

只做"路径算术",不 import 宿主的内部模块 —— 公开面只有 `qi_agent.extensions`
([docs/extensions.md](../../../docs/extensions.md) §3 的白名单),`qi_agent.paths` 不在其中。
所以这里按宿主的**约定**自己推:

    全局  `~/.qi/agent/`      (QI_AGENT_HOME 可覆盖,语义同 PI_CODING_AGENT_DIR)
    项目  `<cwd>/.qi/`

落点:

    ~/.qi/agent/store.json          配置 + 账号(auth 段;写时 0600)
    <cwd>/.qi/store.json            项目级配置(不许有 auth)
    ~/.qi/agent/store/installed.json  实装账本(只用户级)
    ~/.qi/agent/store/cache/          目录快照缓存
"""

from __future__ import annotations

import os
from pathlib import Path

GLOBAL_DIR_NAME = ".qi"
AGENT_DIR_NAME = "agent"
STORE_DIR_NAME = "store"
STORE_FILE_NAME = "store.json"
INSTALLED_FILE_NAME = "installed.json"
CACHE_DIR_NAME = "cache"

#: 覆盖全局 agent 目录(qi 自己的环境变量,跟着走 —— 测试靠它隔离)
QI_AGENT_HOME = "QI_AGENT_HOME"
#: 覆盖名字空间根(`~/.qi`)。**必须也认它**:qi 的测试/多实例就是这样隔离用户状态的,
#: 只认 QI_AGENT_HOME 的话,设了 QI_CONFIG_DIR 的环境里我们会往真实 `~/.qi/agent` 写。
QI_CONFIG_DIR = "QI_CONFIG_DIR"
#: 一次性指向别的实例,不写文件
QI_STORE_URL = "QI_STORE_URL"
#: CI 用的令牌覆盖(优先于 `store.json` 里的 auth)
QI_STORE_TOKEN = "QI_STORE_TOKEN"


def config_root() -> Path:
    """名字空间根:`$QI_CONFIG_DIR`,否则 `~/.qi`(与 `qi_agent.paths.config_root` 同义)。"""
    override = os.environ.get(QI_CONFIG_DIR)
    return Path(override).expanduser() if override else Path.home() / GLOBAL_DIR_NAME


def global_agent_dir() -> Path:
    """全局 agent 目录:`$QI_AGENT_HOME`,否则 `<名字空间根>/agent`。"""
    override = os.environ.get(QI_AGENT_HOME)
    if override:
        return Path(override).expanduser()
    return config_root() / AGENT_DIR_NAME


def project_dir(cwd: Path | None = None) -> Path:
    """项目级目录:`<cwd>/.qi`(与 qi 的 settings.json / mcp.json / a2a.json 同一层)。"""
    return (cwd or Path.cwd()) / GLOBAL_DIR_NAME


def user_config_file() -> Path:
    """用户级配置:与 `settings.json` / `mcp.json` 平级。"""
    return global_agent_dir() / STORE_FILE_NAME


def project_config_file(cwd: Path | None = None) -> Path:
    """项目级配置(可提交进仓库)。"""
    return project_dir(cwd) / STORE_FILE_NAME


def store_home() -> Path:
    """qi-store 自己的目录(账本 + 缓存)。**只用户级**。"""
    return global_agent_dir() / STORE_DIR_NAME


def installed_file() -> Path:
    return store_home() / INSTALLED_FILE_NAME


def cache_dir() -> Path:
    return store_home() / CACHE_DIR_NAME


def cache_file(store_name: str) -> Path:
    """一个源的目录快照。名字做最小净化 —— 源名来自配置,但别让 `../` 逃出缓存目录。"""
    safe = "".join(ch for ch in store_name if ch.isalnum() or ch in "-_.")
    return cache_dir() / f"{safe or 'default'}.json"
