# qi-web

qi 的 **Web UI** 扩展:HTTP 宿主(REST + SSE)+ AG-UI 桥 + 官方界面。

它原本住在 core(`src/qi_agent/web/`,1762 行);P-E5 ③ 按"**core 一点不留**"的决定搬出来 ——
core 不再拖 fastapi/uvicorn,`qi web` 这个入口也由本扩展自己提供(经 core 的
`registerCliCommand` 面)。见 `docs/extensions.md` E25 / §6。

## 装

```bash
pip install qi-web        # 或 -e ./extensions/qi-web(开发)
```

装完 `qi web` 才有这条子命令 —— 没装时它不是"报错",而是**不存在**(core 里已经没有它了)。

## 用

```bash
qi web                        # 默认 127.0.0.1:30142
qi web -p 3456 --no-open      # 换端口、不开浏览器
qi web -H 0.0.0.0 --password 口令
```

参数由本扩展自己解析(argparse):core 只把**命令名之后的原始 argv** 交给 `serve()` ——
选项表是静态的,动态改 typer 的选项表会踩坑(E19 的实测结论)。

安全默认(见 `security.py`):只绑回环;要跨回环必须给口令(`--password` 或
`QI_WEB_PASSWORD`),否则拒启。

## 已知未收口

前端构建产物(`qi_web/static/`)与仓库根的 `web/`(React + Vite 源码)这次**没有一起搬** ——
那是切片 3b;前端里指向已删端点的两处(旧 Agent 切换器、MCP 节)在切片 3c 收。

## 与基座的接口(2026-09 重新接上 / 补全)

这几处此前**都在静默空转或压根不存在**(不报错、只是不生效),现在各有回归测试钉住:

1. **会话绑定**。`WebState.bind()` 每轮开跑前调 `runtime.bind_session()` +
   `start_session()`。没有它:换模型/级别没有落盘点、续会话不还原模型、
   `session_start` **从不派发** —— 而 qi-mcp 的直连工具正挂在那上面
   (`directTools` 在 web 端从未注册过)。见 `tests/test_web_api.py`。
2. **「智能体 chip」**。前端送的 `forwardedProps.agent` 现在走 `serve.pin_role()` →
   `before_agent_start`,把那个角色的正文拼进本轮系统提示词。**不再**用
   `stream(agent_override=…)`:那是 P-E4c 之前的接口,core 收窄成单 agent 之后
   流水线里已无人读它。见 `tests/test_web_extensions_api.py`。
3. **模型面**(`/api/models` + `/api/model`)。此前 web **只有 `/api/config` 的只读回声**:
   界面能显示模型、却换不了(TUI 有 `/model` / ctrl+l / ctrl+p)。现在补上清单与切换,
   清单口径与 TUI 的 `/model` **同一份**(core 的 `selectable_models()`,即
   **解析得出凭证的 provider 才列**:缺凭证的 provider 一条都不进菜单,要配凭证去设置页),
   切换走 `runtime.set_model()` / `set_thinking_level()`(事件与落盘都在里面)。
   输入卡右下那个只读的模型名因此变成了一个真 chip(`ModelMenu`)。

> 这三条都是"**一个 runtime 服务多条会话**"才会显形的形态 —— 而 web 正是这个形态
> (`WebState.runtime_for` 按 cwd 缓存)。补第 3 条时顺手在基座里挖出三处
> **静默串会话**的记账错误(切回会话不还原 / 级别跨会话泄漏 / 还原失败时留在上一条
> 会话的模型上),详见 `design/web.md` §18.29 与 `docs/session-format.md` §6.2。

## 端到端怎么验

```bash
python scripts/e2e_ui_model.py     # 真 Chrome/CDP:模型 chip 的可点性、菜单层叠、切换落盘
```

需要本机装了 Google Chrome(headless 起临时实例,不动你的 profile)。
