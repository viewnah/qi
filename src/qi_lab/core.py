"""与 `qi-coding-agent` 的唯一耦合层。

qi-lab 不是 core 的扩展,而是把 core 当运行时库用。core 从 PyPI 独立发版,所以把
`qi_agent.*` 的 import 集中在这里:core 升级时只改一个文件,也方便在 CI 里对
最低/最新 core 版本做兼容性测试。

公开扩展面(`qi_agent.extensions`)不经过这里 —— 那是内置扩展自己的契约。
"""

from __future__ import annotations

from qi_agent import __version__ as core_version
from qi_agent import paths
from qi_agent.auth import AuthStore, resolve_key
from qi_agent import config as _config
from qi_agent.config import (
    DEFAULT_API,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_TOKENS,
    SUPPORTED_APIS,
    ConfigError,
    load_config,
    load_models_file,
    models_file_for_provider,
    resolve_default_model,
    resolve_router_model,
    save_models_file,
    selectable_models,
)
from qi_agent.llm import THINKING_LEVELS
from qi_agent.loader import (
    SKILL_FILE,
    LoadError,
    load_top_level_skills,
    scan_skill_root,
    split_frontmatter,
)
from qi_agent.models import AgentEvent, Skill
from qi_agent.packages import declared_pip_extensions
from qi_agent.paths import AGENTS_DIR_NAME, SKILLS_DIR_NAME
from qi_agent.presets import get_preset
from qi_agent.runtime import CORE_AGENT_NAME, QiRuntime, RuntimeConfig
from qi_agent.session import Session, SessionStore, usage_summary
from qi_agent.settings import load_settings_by_scope, resolve_project_trust
from qi_agent.trust import TrustStore
from qi_agent.workspaces import WorkspaceStore, normalize

def in_play_providers(cfg, *, default_provider: str, router_provider: str):
    """旧 core 没有这个符号时返回 None,调用方回落到"全部 provider"。"""
    hook = getattr(_config, "in_play_providers", None)
    if callable(hook):
        return hook(cfg, default_provider=default_provider, router_provider=router_provider)
    return None


__all__ = [
    "AGENTS_DIR_NAME",
    "AuthStore",
    "CORE_AGENT_NAME",
    "ConfigError",
    "DEFAULT_API",
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_MAX_TOKENS",
    "SUPPORTED_APIS",
    "LoadError",
    "QiRuntime",
    "RuntimeConfig",
    "SKILLS_DIR_NAME",
    "SKILL_FILE",
    "Session",
    "SessionStore",
    "Skill",
    "THINKING_LEVELS",
    "TrustStore",
    "WorkspaceStore",
    "AgentEvent",
    "core_version",
    "declared_pip_extensions",
    "get_preset",
    "in_play_providers",
    "load_config",
    "load_models_file",
    "models_file_for_provider",
    "save_models_file",
    "load_settings_by_scope",
    "load_top_level_skills",
    "normalize",
    "paths",
    "resolve_default_model",
    "resolve_key",
    "resolve_project_trust",
    "resolve_router_model",
    "scan_skill_root",
    "selectable_models",
    "split_frontmatter",
    "usage_summary",
]
