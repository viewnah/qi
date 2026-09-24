# 插件机制(v1 —— 已被取代)

> ⚠️ **本文描述的「插件」已被 [extensions.md](../docs/extensions.md) 取代(v3)**。保留它的原因是:本文的**双通道发现**、**消费型配置门控**两项机制会被 extensions.md 原样继承(只改名),且它们是**已实现**代码的说明。
> 差异:v1 只有 `add_tool` + `provides_config`;v3 扩到 pi 的整套 hook 面(事件 / 命令 / UI / 会话),并改名 **extension**。
>
> 状态:设计讨论中。相关文档:agent-config-design.md(agent 如何引用)、[how-qi-works.md](../docs/how-qi-works.md)(ToolCatalog)、[models.md](../docs/models.md)。

## 1. 定位

工具/能力是**代码**。插件 = 可分发、可安装的代码包:注册工具(进 ToolCatalog)+ **声明它消费的 agent 配置种类**。内容(skills/assets/agent.md)不属于插件,跟 agent 走。

## 2. 安装与发现双通道(pip 生态,非 npm)

两条**独立**的发现通道,互不转换:

| 通道 | 怎么装 | 装到哪 | 发现机制 | 场景 |
| --- | --- | --- | --- | --- |
| **pip 包** | `qi install qi-db-tools`(PyPI / git / 本地) | venv 的 site-packages(环境级) | **entry point 扫描**(`importlib.metadata`,启动时自动发现) | 分发、团队共享、锁依赖 |
| **本地目录** | 手动放置/复制源码目录 | `~/.qi/plugins/<name>/`(全局)或 `<项目>/.qi/plugins/<name>/`(项目,需信任) | **目录扫描** | 未发布/私有/本地开发,拷贝即用 |

pip 包通过 **entry point** 声明(对齐 pytest/uvicorn 模式),无需维护安装清单:

```toml
# 插件包 pyproject.toml
[project.entry-points."qi.plugins"]
db-tools = "qi_db_tools:register"
```

```python
# qi_db_tools/__init__.py
def register(registry):            # 框架启动时发现并调用
    registry.add_tool(db_schema)
    registry.add_tool(db_query)
    registry.provides_config("data_sources")      # 声明消费的 agent 配置种类
    registry.provides_types("data_sources", ["mysql", "postgresql", "mongodb", "neo4j"])
```

## 3. 能力声明与消费型配置(动态装载)

插件除了给工具,还**声明它消费哪些 agent 目录配置种类**(如 `data_sources`;未来其他插件自有配置如 `embedding.json` 同理):

装载 agent 时,对每个候选配置文件:

```text
有插件声明消费它 → 装载:用插件校验器校验 → 注入 ctx + system prompt
无提供者         → 静默跳过(该配置种类"不存在"),agent 正常启动
```

- 没装 db 插件:`data_sources.json` 不装载、不报错、不悬空;db 工具也不在 catalog。
- 装上插件(下次装载自动发现):配置自动生效,**agent 文件无需改动**。
- 与 tools 校验不冲突:agent 的 `tools:` 是**显式按名引用**,引用缺失 → 启动报错;配置种类是**按插件存在性**判定 → 静默跳过。

## 4. 数据源示例(db 插件)

db 插件提供 `db_schema` / `db_query` 工具 + type 解析/校验/连接;实例(带凭证 env 引用)在 agent 的 `data_sources.json`,见 [agent-config-design.md](agent-config-design.md) §9。工具从运行时 ctx 取当前 agent 的实例清单做权限校验(`data_source_id` 不在清单 → 拒绝),工具不持有配置。只读强制在插件侧校验(对齐 hikqin `db_query` 的"禁止一切写操作")。

## 5. 安全与信任

- 插件代码 = 全权限:仅从可信源安装,先审后装(对齐 pi 对扩展的安全警告)。
- 项目级 `.qi/plugins/` 需要**信任确认**(对齐 pi:项目含本地扩展/配置时启动询问信任)。

## 6. 预留 / 待定

- ~~`qi install/remove/list` 命令封装(pip 包/目录双通道、写配置)~~ → **已撑销(2026-09)**。
  当时的理由:封装 pip 会把「qi 自己的 venv / uv tool 托管 / 只读的系统解释器」三种环境的差异藏起来,
  且 uv tool 重建后 `--sync` 也补不回。**现在按 pi 补上了** `install` / `remove` / `uninstall` / `update`
  ——那条顾虑没有消失,而是换了个位置落地:每次都把要跑的 pip 命令**先打出来**、失败时补
  `uv tool install --with` 那条出路、`remove` 不卸包只改声明。声明层与双向比对(`qi list` /
  `qi doctor`)**保留不变**,所以即使走了 `qi install`,"声明了没装"仍然会被报出来。
  见 [docs/cli.md](../docs/cli.md) 与 [docs/packages.md](../docs/packages.md) §1。
- 运行时热装载(hot reload:运行中装插件立即生效,而非下次装载)
- 插件内嵌内容分发(agent 样板/技能随插件带出,与"内容跟 agent 走"的边界)

## 7. 决策记录

| 决策 | 结论 |
| --- | --- |
| 包生态 | pip 包(entry point `qi.plugins`)+ 本地目录双通道;不仿 npm |
| 能力声明 | `register(registry)`:add_tool / provides_config / provides_types |
| 消费型配置 | 插件声明消费的 agent 配置种类;无提供者 → 配置不装载(静默) |
| 实例归属 | 数据源实例(含凭证 env 引用)始终在 agent 目录,不进插件包 |
