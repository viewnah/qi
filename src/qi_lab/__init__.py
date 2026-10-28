"""qi-lab:以 qi-coding-agent 为运行时的 Web UI 与内置扩展包。

分层:

* `app.py`         —— FastAPI 应用与路由(`/api` v1)
* `state.py`       —— 每 cwd 一个 QiRuntime + 会话串行闸门
* `bundled.py`     —— 内置扩展发现 / 外部覆盖 / 拓扑排序
* `core.py`        —— `qi_agent.*` 的唯一 import 面
* `integrations/`  —— 各内置扩展的唯一 import 面
* `schemas.py`     —— 请求/响应模型(`CONTRACT_VERSION` 是前端依赖的契约号)
* `security.py`    —— 暴露面控制(默认回环 + 可选口令 + Host 允许表)
* `static/`        —— 前端构建产物(由 `ui/` 用 Vite 构建后放入)
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("qi-lab")
except PackageNotFoundError:          # 源码树未安装时(比如直接跑测试)
    __version__ = "0.0.0"


def __getattr__(name: str):
    """惰性转出 `create_app`,让 `import qi_lab` 不强制拉起 fastapi。

    这样在没装 `[web]` extra 的环境里,`qi doctor` 等命令仍可正常导入本包。
    """
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(name)
