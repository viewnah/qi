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
