# 开发与测试

面向要改 qi-lab 的人。qi-lab 是产品仓库;`qi-coding-agent` 是它从 PyPI 安装的运行时,
core 源码不在这里。

## 1. 拉起来

前置:**Python ≥ 3.12** 与 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/viewnah/qi.git
cd qi
uv sync
```

`uv sync` 会把 **qi-lab 装成 editable**,并装上 `qi-coding-agent>=0.4,<0.5` 与内置扩展
需要的第三方依赖(mcp / fastapi / a2a-sdk / httpx / PyYAML)。

内置扩展不是独立 distribution,不需要单独 `pip install`;qi-lab 运行期通过目录通道加载
`extensions/*/extension.py`。

## 2. 从源码跑

```bash
uv run qi-lab                  # 默认 127.0.0.1:3456
uv run qi-lab --list-extensions
uv run qi-lab -p 4321 --no-open
```

`qi` 命令由 `qi-coding-agent` 提供,与 qi-lab 无关。想单独用终端 agent:

```bash
uv run qi --version
uv run qi -p "你好"
```

## 3. 测试

```bash
.venv/bin/python -m pytest            # qi-lab + 内置扩展
.venv/bin/python -m pytest -q tests/test_lab_api.py
uv run pytest -q                      # 等价写法
```

布局:

```text
tests/                    # qi-lab 的集成测试(根 conftest 在这里)
extensions/qi-*/tests/    # 每个内置扩展自己的测试
```

`pyproject.toml` 的 `testpaths = ["tests", "extensions"]`。新增扩展或测试目录时同步检查这里,
否则那批测试会**静默不跑**。

### 默认不装载"本机真装的扩展"

`tests/conftest.py` 里有 autouse 夹具,把 `qi.extensions` 这组 entry point stub 成空。
这样开发机上 `pip install -e` 装过的外部扩展不会让测试结果漂移。要测外部扩展装载行为时显式退出:

```python
pytestmark = pytest.mark.real_extensions
```

```bash
.venv/bin/python -m pytest -m real_extensions
```

### 内置扩展的加载

`qi_lab/bundled.py` 负责:

- 读 `extensions/bundle.toml`;
- 按 `requires` 拓扑排序(qi-mcp 先于 qi-agents);
- 如果同名扩展已经通过 `settings.packages` 声明、且 entry point 存在,或项目/用户级
  `extensions/` 里有同名目录,跳过内置副本;
- 把生效的内置扩展目录作为 `extra_extension_paths` 传给 `QiRuntime`。

调试时看状态:

```bash
uv run qi-lab --list-extensions
# 或 GET /api/meta 的 bundled_extensions
```

## 4. 前端

```bash
cd ui
npm install
npm run typecheck
npm test
npm run check:design
npm run build          # 产物 → src/qi_lab/static/
```

前端源码改动必须重新 build;Python 侧是 editable,改完即生效。

## 5. 静态检查

```jsonc
// pyrightconfig.json
"include":    ["src", "tests", "extensions"],
"extraPaths": ["src", "extensions/qi-mcp", "extensions/qi-agents", …]
```

约定:**不为工具链的问题改产品代码**。分析器缓存导致旧判定时,重启后再复核,不要加
`# type: ignore`。

## 6. 仓库布局

```text
src/qi_lab/          # Web 宿主 / AG-UI 桥 / 内置扩展 loader / 适配层
ui/                  # React + Vite 前端源码
extensions/          # 内置扩展,每个都是完整、可独立发布的 qi 扩展
  bundle.toml        #   顺序 / 依赖 / 开关
  qi-mcp/            #   extension.py + pyproject.toml + qi_mcp/ + README + tests
  qi-agents/
  qi-a2a/
  qi-store/
  qi-kb/
  qi-assistants/
tests/               # qi-lab 集成测试
docs/                # /api 契约与使用文档
design/              # 内部设计/决策记录(不进 wheel)
scripts/             # 版本检查、e2e
```

判据:**只有 `src/qi_lab/` 和 `extensions/*` 的运行文件进 qi-lab wheel**;
内置扩展的 `tests/`、`pyproject.toml`、README、docs 不进 wheel。

## 7. 改完自检

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -m real_extensions
uv run qi-lab --list-extensions
cd ui && npm run typecheck && npm test && npm run build
python3 scripts/check_versions.py
```

## 8. 构建

qi-lab 目前**不发布到 PyPI**,也没有发布 workflow。构建只用于本地/内部分发:

1. 改版本:根 `pyproject.toml` 与 `extensions/*/pyproject.toml` 必须同版本
   (`python3 scripts/check_versions.py`;`TAG=v0.4.0` 时顺带核 tag)。
2. 构建: `uv build --out-dir dist`。
3. 产物:`dist/qi_lab-<version>-py3-none-any.whl` 与 sdist;内置扩展在 wheel 的
   `qi_lab/extensions/` 里。内部分发时直接发 wheel,或 `uv pip install dist/*.whl`。

`qi-coding-agent` 独立发版,qi-lab 只声明 `>=0.4,<0.5` 的兼容范围;不要把 core 加进
内置扩展的 `dependencies`(E11:会让 pip 把宿主降级)。

### 提取一个内置扩展

1. `git mv extensions/qi-mcp ../qi-mcp`;
2. 在独立仓库里 `uv build`(pyproject / entry point / README / tests 已经齐了);
3. 按需发布到私有索引或直接分发 wheel;
4. 用户 `qi install qi-mcp`,qi-lab 检测到已声明且 entry point 存在后自动使用外部版本,
   不再加载同名的内置副本。

提取演练是这套结构的验收标准:如果某个扩展移出去后需要改 qi-lab 主代码才能用,说明边界
还没守住。
