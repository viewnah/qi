# 系统提示词:代码内默认 + 可选整体替换 + 动态追加

> 对齐 pi 的 `core/system-prompt.js`。相关文档:
> [skills.md](skills.md)(技能如何进提示词)、[settings.md](settings.md)(`SYSTEM.md` / `AGENTS.md` 的位置)、
> [model-config.md](model-config.md)(模型)、[PLAN.md](../design/PLAN.md)(未决项)。

## 0. 一句话

qi 的 system prompt = **基座**(代码内默认,或被 `SYSTEM.md` **整体替换**)

+ 运行时**动态追加**:角色层 → 项目上下文 → 技能 → 数据源 → 工作目录。

## 1. 结构(与 pi 的对照)

pi 里其实是两个正交概念:编译进 dist 的**默认 prompt**(代码内字符串,永远存在)与
`SYSTEM.md` / `APPEND_SYSTEM.md`(可选覆盖 / 追加)。qi v2 采用同一形态:

| | pi 0.85.1 | qi v2 |
| --- | --- | --- |
| 默认基座 | 代码内字符串 + 运行时拼接 | `src/qi_agent/system_prompt.py`(同样代码内) |
| 覆盖 | `SYSTEM.md` **整体替换**默认 prompt | 同左(`<项目>/.qi/SYSTEM.md` > `~/.qi/agent/SYSTEM.md`) |
| 追加 | `APPEND_SYSTEM.md` | **不实现**(v1 决策保留;需要时用 `SYSTEM.md` 写全) |
| 动态块 | 工具清单、条件化 guidelines、`<project_context>`、`<available_skills>`、cwd | 同左 + **数据源清单**(qi 独有)+ **角色层** |
| 祖先链边界 | 走到文件系统根 | 止于 **git 根**(与 `.agents/skills` 的继承范围一致) |
| 自身文档索引 | README/docs/examples 绝对路径 + 12 条指路 | **暂不注入**(见 §6) |

qi 相对 pi 保留的唯一结构差异是**角色层**:`agent.md` 正文永远追加在基座之后,
所以「换 agent = 换角色层」,auto 分派语义不变。

```text
┌─ 基座 ─────────────────────────────────────────────┐
│ 有 SYSTEM.md → 该文件正文(整体替换)                 │
│ 无 → 代码内默认:身份 / 可用工具 / 指南 / 环境 / 做法  │
└────────────────────────────────────────────────────┘
┌─ 动态追加(两种情况都有)─────────────────────────────┐
│ 角色层      agents/<name>/agent.md 正文(含 include)  │
│ 项目上下文  <project_context>…AGENTS.md…            │
│ 技能        <available_skills>…(有 read/bash 才注入)│
│ 数据源      清单 + 先 schema 后 query、只读           │
│ 工作目录    当前工作目录: <绝对路径>                  │
└────────────────────────────────────────────────────┘
```

拼装入口:`qi_agent.system_prompt.build_system_prompt(unit, base_prompt, cwd=…, tools=…, context_files=…)`。
`qi_agent.runner` 里同名可导入(向后兼容)。

## 2. 基座来源与优先级

| 优先级 | 位置 | 说明 |
| --- | --- | --- |
| 高 | `<项目>/.qi/SYSTEM.md` | 跟项目走,可提交共享 |
| 中 | `~/.qi/agent/SYSTEM.md` | 全局,所有项目可用 |
| 低 | (无文件) | 代码内默认基座,永远存在 |

规则:

+ 取**第一个非空**的文件;空文件/读取失败视为未配置,继续往下一层找。
+ 都没有 → `resolve_base_prompt()` 返回 `("", "builtin")`,空串即「用代码内默认」;
  基座层永不为空,不存在「零配置没提示词」的状态。
+ **与 pi 的差异(语义)**:pi 的 `SYSTEM.md` 替换整个默认 prompt,qi 也一样 ——
  但 qi 的**角色层与动态块照旧追加**,所以不是「整条 prompt 只剩你的文件」。

## 3. 副作用:自定义基座会丢掉工具清单与指南

默认基座里的「可用工具」「指南」两块是**跟默认基座绑在一起**的(pi 同款行为):
写了 `SYSTEM.md` 就等于接管基座,那两块不再出现。

代价是真实的,写自定义基座时请自己交代工具约定,例如:

```markdown
你是本仓库(订单服务)的开发助手。

可用工具:`read` / `ls` / `find` / `grep` / `edit` / `write` / `bash` / `clarify`。
- `bash` 不做命令级过滤,行为由本仓库约定约束(见 docs/tools.md §4)。
- 需求不明确时先用 `clarify` 提问。

## 本仓库环境
- 跑测试:`uv run pytest -q`;起服务:`uv run uvicorn app:main --reload`
- 数据库迁移在 `migrations/`,只允许新增文件,禁止改历史迁移。
```

动态块(角色层 / 项目上下文 / 技能 / 数据源 / 工作目录)**不受影响**,不用重复声明。

## 4. 默认基座的内容

代码内(见 `system_prompt.py` 的 `DEFAULT_IDENTITY` / `DEFAULT_ENVIRONMENT` / `DEFAULT_METHOD`):

+ **身份**:运行在 qi 框架中的 AI 助手;qi 是多 agent 框架,下面追加的角色说明才是此刻身份。
+ **可用工具**:按该 agent **解析后**的工具集逐行列出(name + description)。
  清单即模型可调用的全集 —— 所以不写 pi 那句「你可能还有别的工具」:qi 不会给出清单外的工具。
+ **指南**:按实际工具集生成(pi 的条件化 guidelines)。当前规则:
  有 `bash`/`powershell` 但没有 `grep`/`find`/`ls` 时提示「用 shell 做文件操作」,
  措辞分三种(只有 bash / 只有 powershell / 两者都有 —— 与 pi 同形);
  恒定三条:结论先行、路径写清楚、与用户一致的语言(默认中文)。
+ **环境**:会话工作目录 + 「需要事实时才用工具」+ 需求不明确用 `clarify`。
+ **做法**:先看再做 / 小步验证 / 不编造 / 务实。

**没有任何 bash 白名单文案**:bash 不做命令级过滤(见 [bash-allowlist.md](../design/bash-allowlist.md)),
提示词不得声称「只读」——`tests/test_system_prompt.py` 有防回归断言。

## 5. 项目上下文(AGENTS.md)

对齐 pi:每级目录按候选顺序取**第一个**命中的文件,注入 `<project_context>`:

```text
AGENTS.override.md > AGENTS.md > AGENTS.MD > CLAUDE.md > CLAUDE.MD
```

顺序 = **全局**(`~/.qi/agent/`)→ 项目祖先链**由远到近**(含 cwd);按路径去重。
祖先链止于 git 根(`paths.project_context_ancestors`,与 `.agents/skills` 一致)。

+ 空文件视为未配置(与 `SYSTEM.md` 同一约定;pi 会注入空块)。
+ 显式关闭:程序接口传 `context_files=[]`(测试/嵌入方要确定性时用)。
+ 未实现:pi 的 `git worktree` 影子文件去重(嵌套 worktree 与主仓库同作用域时只取一份)。

## 6. 技能与手册索引

技能清单是 pi 式的 XML(渐进披露:只给名字/描述/路径,正文用时再读):

```xml
<available_skills>
  <skill>
    <name>code-review-checklist</name>
    <description>评审核对</description>
    <location>/abs/path/code-review-checklist/SKILL.md</location>
  </skill>
</available_skills>
```

+ 措辞随可用工具切换:有 `read` 就说「用 read 读取它的 SKILL.md 全文」,只有 `bash` 就说 bash。
+ **两者都没有 → 整块不注入**(注入了也读不到,只会诱导模型调用不存在的工具);pi 同款。
+ 相对路径以 SKILL.md 所在目录为基准解析,并在工具命令里用绝对路径。

数据源**不在 core**:core 不注入数据源 —— `data_sources.json` 住在角色目录里,归提供该配置
种类的扩展自己读(见 [extensions-design.md §13](../design/extensions-design.md));v1 那条
`id (type)` 列表的实现已经不存在。

### 6.1 手册索引(pi 的 `Additional docs`)

默认基座末尾还会给一段**手册索引**:根目录(绝对路径)+ 按主题列出的手册文件名。

```text
文档(qi 自带的手册;要查用法、或要改 qi 自身,先读对应文件,不要凭记忆猜):
- 根目录: /…/qi_agent/docs
- 开始: index.md、quickstart.md、usage.md、cli.md、tui.md、settings.md、security.md
- 参考: sessions.md、session-format.md、compaction.md、model-config.md、tools.md、providers.md
```

- 表**由 `docs/docs.json` 的 navigation 生成**(pi 把"主题 → 文件"写死在源码里,qi 从索引生成
  —— 手册增删时不会漂)。
- 路径取**实际解析结果**(`paths.docs_dir()`):先找 wheel 里的 `qi_agent/docs/`,再退源码树的
  `<repo>/docs/`;两处都找不到就**整块不注入**,不往提示词里写不存在的路径。
- **只在默认基座分支出现**:自定义 `SYSTEM.md` 是"整体替换",作者自己决定要不要提手册。
- 与技能同规矩:没有能读文件的工具(`read` / `bash`)时整块不注入。
- 正文**不塞进上下文** —— 只给路径,模型自己用 `read` 打开(渐进披露)。

### 6.2 追加自己的段落(`--append-system-prompt`)

`qi --append-system-prompt "<文本>"`(可重复)把文本追加到**每回合** system prompt 的**末尾**,
多段之间空行连接(空白段忽略)。值是**可读文件路径**时读的是文件内容(pi 的
"text or file contents" 口径)。与另外三个入口的区别:

| 入口 | 语义 | 落盘 |
| --- | --- | --- |
| `.qi/SYSTEM.md` | **整体替换**基座 | 是(仓库文件) |
| `--system-prompt "<文本\|文件>"` | **整体替换**基座(同上,只是从命令行给) | 否(只在本次运行) |
| `--append-system-prompt "<文本\|文件>"` | **追加**到末尾 | 否 |
| 扩展的 `before_agent_start` | 改 `system_prompt`(链式,看到的是已拼好的全文) | 否 |

## 7. 用法

### 7.1 用默认的(零配置)

```bash
qi -p "你好"     # 代码内默认基座(单 agent)
```

### 7.2 全局覆盖 / 项目覆盖

```bash
$EDITOR ~/.qi/agent/SYSTEM.md        # 全局
mkdir -p .qi && $EDITOR .qi/SYSTEM.md  # 项目(可提交共享)
```

注意 §3:接管基座后请自己交代工具约定。

### 7.3 查看与编程接口

```python
from qi_agent.loader import resolve_base_prompt, load_project_context

text, source = resolve_base_prompt()      # source: "project:<path>" | "user:<path>" | "builtin"
                                          # text == "" 表示用代码内默认基座
load_project_context()                    # [(AGENTS.md 路径, 正文), …] 全局 → 远 → 近

from qi_agent.system_prompt import default_base_prompt, build_system_prompt

default_base_prompt(tools)                # 默认基座(传入解析后的工具集)
build_system_prompt(unit, text or None, cwd=…, tools=…)   # 完整 prompt
```

`QiRuntime` 实例上:`rt.base_prompt`(自定义基座,可能是 `""`)/ `rt.base_prompt_source`。

## 8. 决策记录

| 决策 | 结论 |
| --- | --- |
| 基座形态 | **代码内字符串**(`system_prompt.py`),按工具集动态生成「可用工具 / 指南」——v1 的包数据 Markdown 方案废弃(静态、无法按工具集变化) |
| `SYSTEM.md` 语义 | **整体替换**默认基座(对齐 pi 的 customPrompt);角色层与动态块照旧追加 |
| `APPEND_SYSTEM.md` | 不实现(pi 有);需要追加就写进 `SYSTEM.md` |
| 空文件 | 视为未配置,继续向下找;不产生空基座 |
| 工具清单 | 列**解析后**的工具集(即模型可调用全集);不写 pi 的「可能还有其他工具」 |
| 项目上下文 | `AGENTS.override.md > AGENTS.md > AGENTS.MD > CLAUDE.md > CLAUDE.MD`;全局 + 祖先链,止于 git 根;空文件跳过 |
| 技能注入 | pi 式 XML(含 location);无 `read`/`bash` 则整块不注入 |
| 自身文档索引 | 暂不注入(未随 wheel 发布),列入 PLAN 未决 |
| 模板化 | 不做 `{{cwd}}` 之类的占位符;动态块由代码追加 |
