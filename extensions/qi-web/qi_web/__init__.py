"""qi-web:HTTP 宿主(REST + SSE + 静态托管),见 design/web.md。

分层:

* `app.py`      —— FastAPI 应用与路由(`/api` v1)
* `state.py`    —— 宿主状态:每 cwd 一个 QiRuntime + 每会话一个活跃 run + 事件日志
* `schemas.py`  —— 请求/响应模型(`CONTRACT_VERSION` 是前端依赖的契约号)
* `security.py` —— 暴露面控制(默认回环 + 可选口令 + Host 允许表)
* `static/`     —— 前端构建产物(由 `web/` 用 Vite 构建后拷入;不进版本库)
"""

from __future__ import annotations


def __getattr__(name: str):
    """惰性转出 `create_app`,让 `import qi_web` 不强制拉起 fastapi。

    这样在没装 `[web]` extra 的环境里,`qi doctor` 等命令仍可正常导入本包。
    """
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(name)
