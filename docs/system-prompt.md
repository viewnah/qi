# 系统提示词:基座层 + 角色层

> 状态:v1 已实现。相关文档:[agent-config.md](agent-config.md)(agent = 角色层)、
> [dispatcher.md](dispatcher.md)(auto 分派)、[model-config.md](model-config.md)(模型)。

## 0. 一句话

qi 的 system prompt 由**两层**拼成:**基座层**(框架级,永远存在)+ **角色层**(agent 级,可以有多个)。
`SYSTEM.md` 覆盖基座层,不覆盖角色层。

## 1. 为什么要分层

对齐 pi 时容易忽略一点:pi 里其实是**两个正交概念**:

| pi | 作用 | 语义 |
|---|---|---|
| 内置默认 system prompt(编译进 dist) | 底座指令 | **永远存在**,不可缺失 |
| `SYSTEM.md` / `APPEND_SYSTEM.md` | 替换 / 追加底座 | 可选覆盖 |

qi 早期把"角色"完全外包给了 `agents/<name>/agent.md`,等于丢掉了"内置默认"那一档 ——
结果 `~/.qi/agents/` 为空时没有任何提示词可用,只能硬失败。分层就是把这个坑补上:

```
┌─ 基座层 ────────────────────────────────────────────┐
│ 身份(运行在 qi 中)/ 环境(会话目录、工具、bash 只读)     │
│ 通用做法(先看再做、小步验证、不编造)/ 输出风格          │
│ 来源:SYSTEM.md 覆盖 → 否则包内置                      │
└──────────────────────────────────────────────────────┘
┌─ 角色层 ────────────────────────────────────────────┐
│ agent.md 正文(含 include 拼入的资产)                   │
│ 私有技能清单(渐进披露)                                │
│ 数据源清单(先 schema 后 query、只读)                   │
└──────────────────────────────────────────────────────┘
```

**换 agent = 换角色层;基座层共享。** auto 分派语义不变。

## 2. 基座层来源与优先级

| 优先级 | 位置 | 说明 |
|---|---|---|
| 高 | `<项目>/.qi/SYSTEM.md` | 跟项目走,可提交共享 |
| 中 | `~/.qi/SYSTEM.md` | 全局,所有项目可用 |
| 低 | `src/qi_agent/SYSTEM.md` | **包内置默认**,随 wheel 发布,永远存在 |

规则:

- 取**第一个非空**的文件;空文件/读取失败视为未配置,继续往下一层找(不会静默降级成空提示词)。
- 包数据万一缺失(裁剪安装/打包故障),代码内有精简兜底文本,保证基座层永不为空。
- 与 pi 的差异:pi 的 `SYSTEM.md` 替换**整个** system prompt;qi 只替换**基座层**,角色层照旧追加。

## 3. 用法

### 3.1 用默认的(零配置)

什么都不用做。`qi -p "你好"` 会用包内置基座提示词 + 内置 `general` 角色。

### 3.2 全局覆盖

```bash
$EDITOR ~/.qi/SYSTEM.md
```

```markdown
你是我们团队的内部助手。

## 约定
- 代码风格遵循仓库 AGENTS.md;提交信息用中文。
- 改动前先跑 `uv run pytest -q`。
```

### 3.3 项目覆盖(可提交,团队共享)

```bash
mkdir -p .qi && $EDITOR .qi/SYSTEM.md
```

```markdown
你是本仓库(订单服务)的开发助手。

## 本仓库环境
- 跑测试:`uv run pytest -q`;起服务:`uv run uvicorn app:main --reload`
- 数据库迁移在 `migrations/`,只允许新增文件,禁止改历史迁移。
```

### 3.4 查看当前生效的是哪个

```python
# 编程接口
from qi_agent.loader import resolve_base_prompt
text, source = resolve_base_prompt()      # source: "project:<path>" | "user:<path>" | "builtin"
```

`QiRuntime` 实例上也直接可见:`rt.base_prompt` / `rt.base_prompt_source`。

## 4. 与角色层的关系(示例)

```markdown
# 基座层(SYSTEM.md 或内置)
你是运行在 qi 框架中的 AI 助手。……(环境 + 通用做法)

# 角色层(agents/writer/agent.md 正文)
你是「文档工程师」。……(这个角色特有的职责与约束)

# 角色层附加(技能 / 数据源,自动生成)
可用技能(需要时用 read 读取其 SKILL.md 全文执行):
- code-review-checklist: 评审核对
```

拼装入口:`qi_agent.runner.build_system_prompt(unit, base_prompt)`。

## 5. 决策记录

| 决策 | 结论 |
|---|---|
| 基座层形态 | 包数据 Markdown(`src/qi_agent/SYSTEM.md`),非代码内联字符串;便于阅读/评审/改文案 |
| 覆盖文件位置 | `<项目>/.qi/SYSTEM.md` + `~/.qi/SYSTEM.md`(与 agents/plugins/sessions 的 `.qi` 约定一致) |
| 语义 | 替换**基座层**(非整个 system prompt);角色层始终追加 |
| 空文件 | 视为未配置,继续向下找;不产生空基座 |
| `APPEND_SYSTEM.md` | **v1 不实现**(pi 有);需要时叠加在基座层之后 |
| 模板化 | **v1 不做占位符替换**(如 `{workdir}`);基座文本是静态的 |
