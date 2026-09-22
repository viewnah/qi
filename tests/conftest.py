"""把三个扩展包放进 `sys.path` —— 它们是**独立发行包**(不装进 venv),测试要能 import 源码。

放一处而不是每个测试文件各写一遍:本轮就踩到了这个坑 —— 逐文件插入时漏掉三个文件,
直接变成 collection error(而报错信息只说 "No module named 'qi_web'",不指路)。

生产环境里这三个包是经 entry point 发现的(装上去的);这里只是让**源码树**里的测试能跑。
"""

from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

for _name in ("qi-agents", "qi-mcp", "qi-web"):
    _path = REPO / "extensions" / _name
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# `src/`(宿主)也放这里,免得个别测试忘了插
_SRC = REPO / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── 测试的确定性:默认不装载"本机真装的扩展" ──────────────────────
#
# 为什么必须这样:三个官方扩展一旦 `pip install -e` 装上(开发机上很常见),entry point 就会
# 在每个测试的 runtime 里被装载 —— 于是**测试结果取决于这台机器上装了什么**。那种漂移最难查:
# 同一条测试在你这里是绿的、在 CI 上也是绿的,只是因为它们测的不是同一件事。
#
# 要测 entry point 行为本身(谁被装载、入口形态对不对)的文件,用
# `pytestmark = pytest.mark.real_extensions` 退出这个夹具 —— 显式退出比隐式依赖清楚。

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
# 为什么要**全局**兜住,而不是让每个测试自己 `monkeypatch.setenv`:
# 这里出过一次真实事故 —— 开发机上 `~/.qi/agent/sessions/` 攒了 3000+ 个空会话,全部是
# 测试建的。根因是"构造时机"与"设环境变量时机"的顺序:
#
#     runtime = RenderFakeRuntime()   # ← 此时构造 SessionStore(),路径就已定死为真 ~/.qi
#     _boot(tmp_path, monkeypatch)    # ← 这里才 setenv(QI_AGENT_HOME),来不及了
#
# 一个测试文件 21 条用例就漏 22 个文件;跑一次全量测试漏 22 个。**每个文件都记得隔离**
# 这种事靠纪律是守不住的(新写的测试文件不会记得),所以放在 autouse 夹具里兜底:
# 谁都不用记得,谁也都绕不过去。
#
# 用 `QI_CONFIG_DIR` 而不是 `QI_AGENT_HOME`:后者是"显式指定,别做迁移"的语义
# (见 `paths.migrate_legacy_layout`),某些测试**故意**测它被显式设置时的行为;
# 而 `QI_CONFIG_DIR` 只管名字空间根,`global_home()` 仍会派生出 `<root>/agent`,
# 与生产路径同形,也让那些自己设 `QI_AGENT_HOME` 的测试照常工作。
#
# 要测真实 home 的用例(`test_settings_skills.py` 的迁移测试)自己
# `monkeypatch.delenv("QI_CONFIG_DIR")` —— 显式退出比隐式依赖清楚。


@pytest.fixture(autouse=True)
def _isolate_user_state(tmp_path_factory, monkeypatch):
    """把名字空间根钉进临时目录 —— 任何 `~/.qi` 写入都落在那里,不会污染开发机。

    返回该目录(少数直接 `QiRuntime(...)` 的测试要往它里面放 models.json)。
    """
    root = tmp_path_factory.mktemp("qi-home")
    monkeypatch.setenv("QI_CONFIG_DIR", str(root))
    monkeypatch.delenv("QI_AGENT_HOME", raising=False)
    monkeypatch.delenv("QI_AGENT_CONFIG", raising=False)
    return root


#: 最小可用配置:一个 provider、一个模型、默认就指它。
#:
#: 直接构造 `QiRuntime` 的测试需要它 —— `load_config` 找不到 models.json 就抛
#: `ConfigError`,而这些测试(自动命名 / 用 entry 的持久化)关心的是**别的东西**。
#: 以前它们能过,是因为 `~/.qi` 里躺着一份**开发机真实的**配置:那正是"同一份测试
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
