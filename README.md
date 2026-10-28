# qi-lab

qi-lab 是以 **[qi-coding-agent](https://pypi.org/project/qi-coding-agent/)** 为运行时的 Web UI / 产品壳:
HTTP 宿主(REST + SSE)+ AG-UI 桥 + React 界面,并内置 MCP、角色、A2A、商店、知识库、应用助手等扩展。

从源码运行(会自动从 PyPI 安装 `qi-coding-agent` 运行时):

```bash
git clone https://github.com/viewnah/qi.git
cd qi
uv sync
uv run qi-lab               # 默认 127.0.0.1:3456
```

常用参数:

```bash
uv run qi-lab -p 4321 --no-open    # 换端口、不自动开浏览器
uv run qi-lab -H 0.0.0.0 --password 口令
uv run qi-lab --cwd ~/your/project  # 指定工作目录
```

`qi-lab` 是独立命令,不再注册成 core 的 `qi lab` 子命令。core 的 `qi` CLI 仍然由
`qi-coding-agent` 提供,二者可以各自使用。

## 结构

```text
src/qi_lab/              # Web 宿主、AG-UI 桥、前端适配层
ui/                      # React + Vite 前端源码
extensions/              # 内置扩展,每个都是可独立发布的 qi 扩展
  bundle.toml            # 装载顺序 / 依赖 / 开关
  qi-mcp/
  qi-agents/
  qi-a2a/
  qi-store/
  qi-kb/
  qi-assistants/
tests/                   # qi-lab 集成测试
docs/                    # API 契约与使用文档
```

`extensions/*` 随 qi-lab wheel 一起打包,运行期作为**目录通道扩展**加载;它们不是独立的
pip distribution,所以 `pip list` 里只有 `qi-lab` 和 `qi-coding-agent`。

每个内置扩展同时保留完整的独立扩展结构(`extension.py` + `pyproject.toml` + 包 + tests),
以后功能成熟时可以直接移出仓库、独立发布。外部已安装并声明的同名扩展会优先于内置副本。

## 开发

```bash
uv sync
uv run qi-lab
.venv/bin/python -m pytest            # qi-lab + 内置扩展测试
cd ui && npm install && npm run build # 前端产物 → src/qi_lab/static/
```

前端源码改动需要重新 build;Python 侧 editable 安装,改完即生效。

## 与 core 的关系

- `qi-coding-agent` 从 PyPI 安装,`qi-lab` 声明兼容范围(`>=0.4,<0.5`);
- core 源码不在此仓库,core 的 CLI / TUI 能力由它自己的发行包提供;
- 内置扩展**不把 `qi-coding-agent` 写进 dependencies**,避免 pip 解析时把宿主降级;
  它们只依赖 core 的公开扩展面。

## 许可证

MIT(见 [LICENSE](LICENSE))。
