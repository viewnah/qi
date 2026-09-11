"""运行时目录与路径约定。

全局:`~/.qi`(可用 QI_AGENT_HOME 覆盖,测试/CI 用)
项目:`<git根>/.qi`(无 git 仓库时用 cwd)

模型配置文件名 `models.json`,格式对齐 pi;凭证明文只在 `auth.json`。
"""

from __future__ import annotations

import os
from pathlib import Path

GLOBAL_DIR_NAME = ".qi"
MODELS_FILE_NAME = "models.json"

# 环境变量:同 PATH 分隔符,可指定多个额外配置/查找目录(预留)
QI_AGENT_HOME = "QI_AGENT_HOME"      # 覆盖全局 ~/.qi(如测试)
QI_AGENT_CONFIG = "QI_AGENT_CONFIG"  # 指向 models.json 具体文件(最高优先级)


def global_home() -> Path:
    """全局 qi 目录(~/.qi,可用 QI_AGENT_HOME 覆盖)。"""
    override = os.environ.get(QI_AGENT_HOME)
    return Path(override).expanduser() if override else Path.home() / GLOBAL_DIR_NAME


def find_project_root(start: Path | None = None) -> Path:
    """向上找含 .git 的目录作为项目根;找不到则用 cwd。"""
    cur = (start or Path.cwd()).resolve()
    if cur.is_file():
        cur = cur.parent
    for p in (cur, *cur.parents):
        if (p / ".git").exists():
            return p
    return cur


def project_home(cwd: Path | None = None) -> Path:
    """项目 qi 目录(<git根>/.qi)。"""
    return find_project_root(cwd) / GLOBAL_DIR_NAME


def models_file_candidates(cwd: Path | None = None) -> list[Path]:
    """按优先级从高到低返回可能的 models.json 路径(含 env 指定文件)。"""
    candidates: list[Path] = []
    env_file = os.environ.get(QI_AGENT_CONFIG)
    if env_file:
        candidates.append(Path(env_file).expanduser())
    candidates.append(project_home(cwd) / MODELS_FILE_NAME)
    candidates.append(global_home() / MODELS_FILE_NAME)
    return candidates
