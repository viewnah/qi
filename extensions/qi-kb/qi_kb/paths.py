"""qi-kb 的文件位置。

与 qi-store 的 `paths.py` 同一套命名空间规则，但只碰 qi-kb 自己的文件：

    ~/.qi/agent/kb.json        provider + knowledgeBases（不含令牌，可进仓库的项目级副本）
    ~/.qi/agent/kb-auth.json   provider 的 access/refresh（0600，永不进仓库）
    <cwd>/.qi/kb.json          项目级 provider/KB 覆盖

全局目录认 `QI_AGENT_HOME` 与 `QI_CONFIG_DIR`（与宿主、qi-store 一致）。
"""

from __future__ import annotations

import os
from pathlib import Path

GLOBAL_DIR_NAME = ".qi"
AGENT_DIR_NAME = "agent"
KB_FILE_NAME = "kb.json"
KB_AUTH_FILE_NAME = "kb-auth.json"

QI_AGENT_HOME = "QI_AGENT_HOME"
QI_CONFIG_DIR = "QI_CONFIG_DIR"


def config_root() -> Path:
    override = os.environ.get(QI_CONFIG_DIR)
    return Path(override).expanduser() if override else Path.home() / GLOBAL_DIR_NAME


def global_agent_dir() -> Path:
    override = os.environ.get(QI_AGENT_HOME)
    if override:
        return Path(override).expanduser()
    return config_root() / AGENT_DIR_NAME


def project_dir(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / GLOBAL_DIR_NAME


def user_config_file() -> Path:
    return global_agent_dir() / KB_FILE_NAME


def project_config_file(cwd: Path | None = None) -> Path:
    return project_dir(cwd) / KB_FILE_NAME


def auth_file() -> Path:
    return global_agent_dir() / KB_AUTH_FILE_NAME
