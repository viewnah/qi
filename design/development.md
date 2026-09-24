# 开发与测试

面向要改 qi 本身的人。用户手册在 [docs/index.md](../docs/index.md);**其余设计与决策记录在 [PLAN.md](PLAN.md)**
(那是给改动者看的,不是手册)。

## 1. 拉起来

前置:**Python ≥ 3.12** 与 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/viewnah/qi.git
cd qi
uv sync
```

`uv sync` 会把 **qi 本体装成 editable**(`.venv/lib/python3.12/site-packages/_editable_impl_qi_agent.pth`)
并装上 dev 依赖(`pytest` / `pytest-asyncio`)。

**三个扩展要单独装**——它们各自是独立 pip 包,而仓库**没有配 uv workspace**(故意的:core 不该
默认拖上 MCP / FastAPI 那些依赖):

```bash
pip install -e extensions/qi-agents -e extensions/qi-mcp -e extensions/qi-web
# 或: uv pip install -e extensions/qi-agents -e extensions/qi-mcp -e extensions/qi-web
```

不装也能跑测试(见 §3);但要在开发机上**手工验证扩展行为**,这三个装上是必须的。

## 2. 从源码跑

```bash
uv run qi doctor              # 或 .venv/bin/qi doctor
uv run qi --mode json "你好"   # 改完想看一眼事件流时最快
```

`uv run` 用的是仓库里的 `.venv`,所以改 `src/qi_agent/` 立即生效(editable),不需要重装。

> **`qi` 保持调用者的工作目录**,所以从任何地方跑都不会把你的 cwd 换掉。

## 3. 测试

```bash
.venv/bin/python -m pytest            # 全量(53 个测试文件,约 50 秒)
.venv/bin/python -m pytest -q tests/test_session.py       # 单个文件
uv run pytest -q                      # 等价写法
```

**qi 没有 `test.sh`** —— 上面就是唯一入口(别去敲不存在的包装脚本)。

### 默认不装载"本机真装的扩展"

`tests/conftest.py` 里有一个 autouse 夹具,把 `qi.extensions` 这组 entry point **stub 成空**。
理由写在那里的注释里,值得抄一遍:

> 三个扩展一旦 `pip install -e` 装上(开发机上很常见),entry point 就会在每个测试的 runtime
> 里被装载 —— 于是**测试结果取决于这台机器上装了什么**。那种漂移最难查:同一条测试在你这里是
> 绿的、在 CI 上也是绿的,只是因为它们测的不是同一件事。

要测 entry point 行为本身的文件显式退出:

```python
pytestmark = pytest.mark.real_extensions      # 只对这类文件生效
```

跑它们:

```bash
.venv/bin/python -m pytest -m real_extensions
```

夹具只 stub `qi.extensions` 这一组,其它 `entry_points()` 调用(查 dist 版本等)原样放行 ——
所以"扩展有没有把宿主写进 `dependencies`"这类检查照样被测到。

## 4. 静态检查

`pyrightconfig.json` 把三处都纳进来了:

```jsonc
"include":    ["src", "tests", "extensions"],
"extraPaths": ["extensions/qi-agents", "extensions/qi-mcp", "extensions/qi-web", "src"]
```

`extraPaths` 是为了让跨包 import(`from qi_agents.discovery import discover`)在类型检查器里也能解析 ——
仓库不是 uv workspace,所以没有安装声明可依。

**约定:不为工具链的问题改产品代码。** 分析器偶尔会对新装的 editable 包报旧判定(会话内缓存了
环境/路径);记录在 [extensions-design.md](extensions-design.md) 的实测表里。处理原则是**等分析器重启再复核**,
而不是加 `# type: ignore` 或改运行期代码。

## 5. 仓库布局

```text
src/qi_agent/        # core:单包(一个 wheel)。裸启动 = 单 agent
extensions/          # 三个独立 pip 包,各自有自己的 pyproject.toml / README
  qi-agents/         #   角色系统 + subagent 委派
  qi-mcp/            #   MCP 支持
  qi-web/            #   HTTP 宿主 + 官方 UI(UI 在 qi-web/ui/,自带 package.json)
design/              # 内部设计、决策记录、变更记录 —— **不进 wheel、不注入提示词**
docs/                # 手册(见 docs/index.md)+ docs.json(导航清单)
examples/            # 教学样例(不自动加载)
tests/               # pytest
```

判据很简单:**只有 `src/qi_agent/` 进 wheel**;`docs/` 目前也不进(要把手册随包发布得改
`[tool.hatch.build.targets.wheel]`)。**扩展的手册归扩展自己**(各自的 README);core 的 `docs/` 只讲机制,
不复述任何扩展自己的用法。

## 6. 改完自检

```bash
.venv/bin/python -m pytest -q                    # 全量
.venv/bin/python -m pytest -m real_extensions    # 真扩展那几条
uv run qi doctor                                 # 冒烟:配置/凭证/扩展/包声明
```

改到文档时,顺手确认链接没断:`docs/docs.json` 的 `navigation` 每一条 path 都必须真实存在
(它是导航清单,也是"文档还缺哪几篇"的账本;补完一篇就从 `planned` 移进 `navigation`)。

## 7. 发布(四个 distribution)

仓库里有**四个** PyPI distribution,锁步同版本:

| 包 | 发行名 | 说明 |
| --- | --- | --- |
| core | **`qi-coding-agent`** | import 包名仍是 `qi_agent`,命令仍是 `qi` |
| 扩展 | `qi-mcp` / `qi-agents` / `qi-web` | 各自独立;`qi-agents` 依赖 `qi-mcp` |

> **为什么发行名不是 `qi-agent`**:PyPI 上的 `qi-agent`(以及 `qi`)**已被别的项目占用**(前者 2026-09 还在发版,不是抢注),PEP 503 归一后也不能靠大小写/下划线绕开。发行名只影响 `pip install` 的那串名字 —— import 包名与命令都没变。旧名写进扩展依赖会被装载时单独报出来(见 `registry.warn_host_dependency`)。

### 发布前

1. **四个 `pyproject.toml` 的 `version` 一起改**(core + 三个扩展):
   `python3 scripts/check_versions.py`(可加 `TAG=v0.2.0` 顺便核 tag)。
2. 本地过一遍全量测试与构建:

```bash
.venv/bin/python -m pytest -q
uv build --out-dir dist && uv build extensions/qi-mcp --out-dir dist \
  && uv build extensions/qi-agents --out-dir dist && uv build extensions/qi-web --out-dir dist
```

### 走 workflow(推荐)

`.github/workflows/publish.yml`:

- **打 tag 发布**:`git tag v0.1.0 && git push origin v0.1.0` → 构建 + 发布到 **PyPI**;
- **手动触发**:Actions → publish → Run workflow,两个输入:
  - `target`:`testpypi`(默认,试跑)/ `pypi`;
  - `packages`:**`core`(默认,只发 `qi-coding-agent`)** / `all`(连三个扩展一起发)。
    选 `core` 时版本检查只核 core(四个锁步那条不适用);扩展还没发布时保持默认。
- **action 全部按 commit SHA 固定**(发布 job 持有 PyPI 权限,不吃可变 tag),并关掉 uv 缓存
  (`cache-poisoning`)、`checkout` 不带 git 凭证(`persist-credentials: false`);
- 用的是 **Trusted Publishing(OIDC)**,不需要 API token —— 但要在两处各配一次 publisher:

| 索引 | Publisher 配置 |
| --- | --- |
| PyPI | Owner `viewnah` · Repository `qi` · Workflow `publish.yml` · Environment **`pypi`** |
| TestPyPI | 同上,Environment **`testpypi`** |

workflow 里两个 job 都声明了 `environment`,与这里的名字必须一致;`id-token: write` 是 OIDC 的前提。
TestPyPI 那步带 `skip-existing`,所以同一个版本可以反复试跑。

从 TestPyPI 验装(依赖仍在正式索引):

```bash
uv tool install --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple qi-coding-agent
```

### 手动发布(不想用 CI 时)

```bash
uv build --out-dir dist && uv build extensions/qi-web --out-dir dist   # …四个都构建
uv publish --token "$UV_PUBLISH_TOKEN"        # 或 twine upload dist/*
```

**首次发布尤其建议先走 TestPyPI** —— 名字是否可用、metadata 是否合法、`README` 能否渲染成描述页,
这些在正式索引上改不了(同一个版本号不能重发,只能 bump)。
