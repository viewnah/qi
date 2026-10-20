"""qi(发行名 `qi-coding-agent`):编码 agent 框架 —— 单 agent core + 扩展。"""

import os
import warnings

# LiteLLM 在 **import 时** 会去 GitHub 拉一份模型价目表
# (model_prices_and_context_window.json),超时 5 秒后失败才降级到本地备份。
# 那个请求对我们的模型(自定义 provider)没有价值,却会让每次启动白等 5 秒,
# 并在网络不通时打印一条吓人的 WARNING。
#
# 这里在 litellm 被 import 之前把它钉到本地备份:
#   - qi_agent/__init__.py 一定先于任何子模块执行,而 litellm 是懒加载的
#     (见 llm.py 的 `load_litellm`),所以顺序有保证;
#   - 用 setdefault:用户显式设 LITELLM_LOCAL_MODEL_COST_MAP=False 时尊重用户,
#     仍可拉远端最新价目表。
# 开关语义见 litellm/litellm_core_utils/get_model_cost_map.py。
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# litellm 的 `acompletion` 把同步 `completion()` 丢进线程池,线程里再创建
# `OpenAIChatCompletion.async_streaming()` 协程交回事件循环。中止(escape / ctrl+c /
# 关窗口)恰好落在“线程还没把协程交回来”这段时,协程会在取消之后才被创建、永远没人
# await,解释器退出时由 GC 打出:
#     sys:1: RuntimeWarning: coroutine 'OpenAIChatCompletion.async_streaming' was never awaited
# 这是 litellm 取消路径的固有产物(请求已经放弃,不是真正的资源泄漏)。qi 在 llm.py 里
# 已把 import 挪出事件循环来缩小这个窗口;这里只屏蔽这两个确切的协程名,其它
# "never awaited" 仍然照常暴露。
warnings.filterwarnings(
    "ignore",
    message=r"coroutine 'OpenAIChatCompletion\.(async_streaming|acompletion)' was never awaited",
    category=RuntimeWarning,
)

__version__ = "0.3.0"
