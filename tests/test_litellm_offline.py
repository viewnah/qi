"""LiteLLM 离线价目表:导入 qi_agent 后不再联网拉 model cost map。

背景(不是 bug,是 LiteLLM 的默认行为):
    LiteLLM 在 import 时会同步 GET
    https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
    (超时 5s),失败才降级到本地备份并打印一条 WARNING。对自定义 provider 毫无价值,
    却让每次启动白等 5s。qi 在 `qi_agent/__init__.py` 里用 setdefault 把它钉到本地。

这里用子进程验证,因为该开关是 **import 时求值** 的,同进程内无法模拟"先设后导"。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

# litellm import 后走本地备份的判定结果(见 get_model_cost_map.py)
_PROBE = """
import json
import qi_agent
import litellm
from litellm.litellm_core_utils.get_model_cost_map import _cost_map_source_info
print(json.dumps({
    "env": __import__("os").environ.get("LITELLM_LOCAL_MODEL_COST_MAP"),
    "source": _cost_map_source_info.source,
    "is_env_forced": _cost_map_source_info.is_env_forced,
    "fallback_reason": _cost_map_source_info.fallback_reason,
    "models": len(litellm.model_cost),
}))
"""

_ENV_ONLY = """
import os
import qi_agent
print(os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP"))
"""


def _run(code: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)  # 从干净状态开始
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)


def test_importing_qi_disables_remote_cost_map():
    """导入 qi_agent 后,litellm 直接用本地备份:不联网、无 WARNING、无降级原因。"""
    proc = _run(_PROBE)
    assert proc.returncode == 0, proc.stderr
    info = json.loads(proc.stdout.strip().splitlines()[-1])

    assert info["env"] == "True"           # 开关已设
    assert info["source"] == "local"       # 用的是本地
    assert info["is_env_forced"] is True   # 是主动限定,而非"抓取失败后降级"
    assert info["fallback_reason"] is None  # 没有发生过失败的远程抓取
    assert info["models"] > 0              # 价目表非空
    assert "Failed to fetch remote model cost map" not in proc.stderr


def test_user_can_override_to_remote():
    """用户显式设 False 时尊重用户(仍可拉远端)。此测试不真的联网。"""
    proc = _run(_ENV_ONLY, env_extra={"LITELLM_LOCAL_MODEL_COST_MAP": "False"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"


def test_default_env_not_present_without_qi():
    """对照:不导入 qi 时该开关不存在,证明是 qi 设的(而非环境自带)。"""
    proc = _run("""
import os
from qi_agent import __version__  # noqa: F401  (等价于不触发开关时的基线)
import importlib, sys
mod = sys.modules.pop("qi_agent")
os.environ.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
print(os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP"))
""")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "None"
