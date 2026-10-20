"""qi-lab 测试的运行基座:把扩展与宿主放进 `sys.path`,并**兜住三件绝不靠纪律的事**。

与 `tests/conftest.py` 同源(精简到 qi-lab 所需),但独立一份 —— pytest 的 conftest 按
**目录**生效,测试搬进 `extensions/qi-lab/tests/` 之后根目录那份就覆盖不到了;少了它:

1. 不 stub entry point → 本机 `pip install -e` 装过扩展的话,真实扩展会被装载,
   测试结果随"这台机器装了什么"漂移;
2. 不隔离 `~/.qi` → 测试会往开发机真实用户状态里写(出过 3000+ 空会话的事故);
3. 没有 `minimal_config` → 凡是要 `QiRuntime` 的用例在**没有 models.json** 时
   抛 `ConfigError` → 503,报错指向"缺默认模型",而不是它真正的病根。

qi-lab 的用例比 qi-agents 多一层依赖:app 直接 import `qi_mcp.config`(`/api/mcp`)
与 `qi_agent.*`,所以 path 里要把这几个源码目录都放进来 —— 与根 conftest 同一份清单。
"""

from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path

import pytest

# `<repo>/extensions/qi-lab/tests/conftest.py` → 仓库根
REPO = Path(__file__).resolve().parents[3]

# 这些是**独立发行包**(不装进 venv),连同宿主 `src/` 一起放进 path 才能 import 源码
for _rel in ("extensions/qi-agents", "extensions/qi-mcp", "extensions/qi-web",
             "extensions/qi-lab", "extensions/qi-a2a", "src"):
    _path = REPO / _rel
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


# ── 测试的确定性:默认不装载"本机真装的扩展" ──────────────────────

_ORIGINAL_ENTRY_POINTS = importlib.metadata.entry_points


@pytest.fixture(autouse=True)
def _no_ambient_extensions(request, monkeypatch):
    """只 stub `qi.extensions` 这一组;别的 `entry_points()` 调用(查 dist 版本等)原样放行。"""
    if request.node.get_closest_marker("real_extensions"):
        return

    def _only_what_the_test_asks_for(*, group=None, **kwargs):
        if group == "qi.extensions":
            return []
        return _ORIGINAL_ENTRY_POINTS(group=group, **kwargs) if group else _ORIGINAL_ENTRY_POINTS(**kwargs)

    monkeypatch.setattr(importlib.metadata, "entry_points", _only_what_the_test_asks_for)


# ── 测试绝不碰真实用户状态(~/.qi)──────────────────────────────
#
# 用 `QI_CONFIG_DIR` 而非 `QI_AGENT_HOME`:后者是"显式指定,别做迁移"的语义,某些测试
# **故意**测它被显式设置时的行为;`QI_CONFIG_DIR` 只管名字空间根,`global_home()` 仍派生
# `<root>/agent`,与生产路径同形 —— 技能写端点的 `scope=global` 落点因此也在临时目录里。

@pytest.fixture(autouse=True)
def _isolate_user_state(tmp_path_factory, monkeypatch):
    """把名字空间根钉进临时目录 —— 任何 `~/.qi` 写入都落在那里,不会污染开发机。"""
    root = tmp_path_factory.mktemp("qi-home")
    monkeypatch.setenv("QI_CONFIG_DIR", str(root))
    monkeypatch.delenv("QI_AGENT_HOME", raising=False)
    monkeypatch.delenv("QI_AGENT_CONFIG", raising=False)
    return root


#: 最小可用配置:一个 provider、一个模型、默认就指它。
#:
#: 直接构造 `QiRuntime` 的用例需要它 —— `load_config` 找不到 models.json 就抛
#: `ConfigError`,而那些用例关心的是**别的东西**(技能、会话、AG-UI 线格式…)。
#: 曾经它们能过,是因为 `~/.qi` 里躺着一份**开发机真实的**配置:那正是"同一份测试
#: 在不同机器上测的不是同一件事"(CI 上没有那份配置就红)。隔离夹具上线后暴露出来,
#: 这里显式给一份。
MINIMAL_MODELS = ('{"providers": {"stub": {"api": "openai-completions", '
                  '"models": [{"id": "m1"}]}}}')
MINIMAL_SETTINGS = '{"defaultProvider": "stub", "defaultModel": "m1"}'


@pytest.fixture
def minimal_config(_isolate_user_state):
    """在隔离 home 里写好最小 models.json + settings.json,返回 agent 目录。"""
    agent = _isolate_user_state / "agent"
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "models.json").write_text(MINIMAL_MODELS, encoding="utf-8")
    (agent / "settings.json").write_text(MINIMAL_SETTINGS, encoding="utf-8")
    return agent
