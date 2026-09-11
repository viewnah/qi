# Web 能力设计(宿主 + UI 插件)

> 状态:设计定稿(v2 实现)。相关文档:[plugins.md](plugins.md)(插件机制)、[tools.md](tools.md)(ToolCatalog)。参考:pi 的 RPC 模式与 pi-web(独立 Web 应用)的取舍。

## 1. 结论与形态

**宿主在框架,UI 是插件**:

| 层 | 内容 | 归属 |
|---|---|---|
| HTTP 宿主 | uvicorn、基础 `/api`(会话/发消息)、SSE 事件流、静态资源托管、安全(回环/密码/信任) | **框架(v2)** |
| UI 前端 | 页面资源(HTML/JS/CSS:聊天 + 分派可视化) | **插件**(pip 包,随包分发) |

理由:web 是 `runtime.stream()` 的**第三个 consumer**(CLI / TUI / HTTP,同进程零协议),框架内置宿主即可免掉 pi-web 那套子进程 RPC 桥;而前端属于"内容/可选能力",插件化与"框架零内置内容"哲学一致,核心不养前端。

## 2. 方案取舍(为何不选外置 / 纯内置)

| | 外置应用(pi-web 模式) | 纯内置 UI | **宿主 + UI 插件(采用)** |
|---|---|---|---|
| 进程/协议 | 子进程 + JSONL-RPC | 同进程 | 同进程,零协议 |
| 核心养前端 | 不养 | 要养 | 不养(官方出参考 UI 插件) |
| UI 升级/换皮 | 独立升级 | 跟框架发布 | pip 升级/换插件 |
| 复杂度 | 两套部署 | 最小 | UI 与宿主 `/api` 需契约版本化 |

## 3. HTTP 宿主(v2)

```
qi web                          # 起本地服务;默认 127.0.0.1 + 端口配置 + 可选密码
▼
FastAPI/Starlette + uvicorn     # 复用 hikqin server.py 思路
├── /api/sessions · /api/agent/<id>     # REST:查会话 / 发消息
├── /api/agent/<id>/events(SSE)         # 事件流 = stream() 直连(同 CLI/TUI 的 consumer)
├── /api/health
└── 静态托管:已安装 UI 插件的页面资源
```

- 应用级配置(host/port/password_env)属应用配置主题(v2 待定;模型/凭证已在 `models.json` + `auth.json`)
- 安全默认:仅 `127.0.0.1`;远程需显式 hostname + 密码(参考 pi-web 的警告语义:暴露可执行高权限操作的 agent)
- **API 契约版本化**:`/api` 形状是 UI 插件的依赖,语义版本化(v1/v2),宿主保证兼容,防止契约漂移
- 不装 UI 插件:`qi web` 只提供 API/健康检查

## 4. UI 插件形态

```python
# qi-web-ui 插件的 register()
def register(registry):
    registry.add_static("/", package_dir / "ui")   # 前端资源挂到宿主
    # 基础 /api + SSE 复用宿主内置;需要自定义后端才 add_route
```

```toml
# 插件 pyproject.toml
[tool.setuptools.package-data]
qi_web_ui = ["ui/**/*"]
```

- 前端技术栈:v2 起 **零构建轻量**(vanilla + SSE),避免构建链进 pip 包;需要重 UI 再引入构建产物
- 深度定制(如 DB 管理台代理):插件 `add_route` 扩展(见 plugins.md),需安全审计
- 官方维护一个参考 UI 插件,与框架 `/api` 契约同步演进

## 5. 与其他主题的关系

- **插件机制**:web UI 是消费型能力之一(`add_static/add_route` 与 plugins.md 的 register 扩展);安全/信任规则同 plugins.md §5
- **headless RPC**(JSONL over stdio,对齐 pi `--mode rpc`):内置宿主让 Web 不需要它;它留给**外部客户端**(IDE/独立工具),作为独立未来项,不在 v2 web 范围内
- 数据共享:前端读会话 JSONL / `.qi` 配置渲染历史与管理视图(与 agent 进程解耦),格式已定

## 6. 决策记录

| 决策 | 结论 |
|---|---|
| 形态 | 宿主在框架(v2)+ UI 插件化,同进程直连 stream(),不做外置 RPC 桥 |
| 宿主 | `qi web`:uvicorn + 基础 REST + SSE + 静态托管;默认回环 + 可选密码 |
| 契约 | `/api` 语义版本化,防 UI 插件契约漂移 |
| UI | 零构建轻量前端,官方参考插件;深度定制走 add_route |
| RPC | 不进 v2 web;留给外部客户端(IDE)的未来项 |

## 7. 待定(并入对应主题)

- `[web]` 配置字段与端口默认(应用配置主题)
- UI 插件首发范围(会话聊天 + 分派可视化 + agent/配置查看?)
- SSE vs WebSocket(事件量大时的取舍,v2 实现时定)
