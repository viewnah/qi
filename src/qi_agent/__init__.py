"""qi-agent:多 agent 编码框架。"""

import os

# LiteLLM 在 **import 时** 会去 GitHub 拉一份模型价目表
# (model_prices_and_context_window.json),超时 5 秒后失败才降级到本地备份。
# 那个请求对我们的模型(自定义 provider)没有价值,却会让每次启动白等 5 秒,
# 并在网络不通时打印一条吓人的 WARNING。
#
# 这里在 litellm 被 import 之前把它钉到本地备份:
#   - qi_agent/__init__.py 一定先于任何子模块执行,而 litellm 是懒加载的
#     (见 llm.py 的 `import litellm`),所以顺序有保证;
#   - 用 setdefault:用户显式设 LITELLM_LOCAL_MODEL_COST_MAP=False 时尊重用户,
#     仍可拉远端最新价目表。
# 开关语义见 litellm/litellm_core_utils/get_model_cost_map.py。
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

__version__ = "0.1.0"
