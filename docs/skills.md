# 技能

技能是**按需加载的能力说明**:一个目录 + 一份 `SKILL.md`。它的正文**不进系统提示词** —— 提示词里
只放名字、描述与位置,模型在任务匹配时再用 `read` 去读全文。这叫**渐进披露**:一个技能占提示词的
成本是两三行,而不是一整篇。

`SKILL.md` 用 YAML frontmatter 开头:

```markdown
---
name: code-review-checklist
description: 审代码时按这份清单逐项检查;涉及并发、错误处理与测试覆盖时使用。
---

（正文:具体怎么做。可以很长。）
```

| frontmatter 字段 | 必需 | 说明 |
| --- | --- | --- |
| `description` | **是** | **没有 description 的技能不会被装载**(静默跳过,不是报错) |
| `name` | 否 | 缺省时用目录名(`SKILL.md` 所在目录)或文件名(根级 `*.md` 的 stem) |

**事实源:`src/qi_agent/loader.py`(发现与冲突)+ `src/qi_agent/system_prompt.py`(注入)。**

## 1. 放哪里:六层来源

按**优先级从低到高**排;同名时高的覆盖低的。

| # | 目录 | 层标签 | 说明 |
| --- | --- | --- | --- |
| 1 | `~/.agents/skills/` | `agents-global` | **跨工具**(Agent Skills 标准的 `.agents` 目录),全局共享 |
| 2 | `~/.qi/agent/skills/` | `qi-global` | qi 私有,全局 |
| 3 | user `settings.json` 的 `skills[]` | `settings-global` | 追加路径,相对 `~/.qi/agent` 解析 |
| 4 | `.agents/skills/`(cwd → git 根,远→近) | `agents-project` | 跨工具,项目级 |
| 5 | `<git根>/.qi/skills/` | `qi-project` | qi 私有,项目级 |
| 6 | project `settings.json` 的 `skills[]` | `settings-project` | 追加路径,相对 `<git根>/.qi` 解析 |

第 1、2、4、5 层的**具体目录**之所以这样排,是为了跟 pi 的发现规则对齐,再叠上 qi 自己的私有层
(`~/.qi/...` 与 `<git根>/.qi/...`)。跨工具目录(`.agents`)与 qi 私有目录的区别不只是名字:
**跨工具目录只认 `<name>/SKILL.md` 这种结构,不接受根目录下散着的 `*.md`**(见 §2)。

两条容易踩的:

- 第 3、6 层的相对路径**各按自己所在的目录解析**(user 的按 `~/.qi/agent`,project 的按
  `<git根>/.qi`)。用合并后的配置去解析,会让一侧的路径落到另一侧的基准上。
- 第 4、5 层是**项目级**(随仓库走)。**注意:项目级技能不受信任门控** —— 未信任的项目里
  `.agents/skills` 与 `<git根>/.qi/skills` **仍会被装载**。这与项目级*扩展*不同:后者未信任就不扫
  (因为扩展是可执行代码)。界线画在“可执行 vs 文本”上,而技能是文本指令。详见
  [security.md](security.md) §2。

## 2. 一个技能根的扫描规则

对每个来源目录:

### 3.1 强制加载:`/skill:<名>`

渐进披露的另一面是"模型不一定去读"(提示词里只有一行描述)。TUI 里可以自己把它叫起来:

```text
/skill:brave-search           # 加载并执行该技能
/skill:pdf-tools extract      # 带参数(作为技能的入参)
```

它把 SKILL.md **全文**提交给模型,效果与用户手贴内容等价,只是不用手抄 —— pi 的同一句话是
"use prompting or `/skill:name` to force it"。`settings.enableSkillCommands`(默认 `true`)关掉后
这批命令不存在。

- **含 `SKILL.md` 的目录即技能,不再向内递归** —— 也就是说技能目录里可以再放参考文件,不会
  被误当成子技能。
- **没有 `SKILL.md` 的子目录继续向内找** —— 支持分组目录(如 `skills/lang/python/SKILL.md`)。
- **根目录下带 `description` 的 `*.md` 也算独立技能** —— 但**只对 qi 侧目录生效**
  (`~/.qi/agent/skills`、`<git根>/.qi/skills`、以及 `settings.skills[]` 指定的路径与 CLI `--skill`)。
  跨工具的 `.agents/skills` **不开**这一条,以贴合 Agent Skills 标准。子目录里散着的 `*.md`
  一律不算。
- 一个 `--skill` / `settings.skills[]` 条目**可以直接指向一个 `.md` 文件**,也可以指向目录。

## 3. 优先级与冲突

两种同名,处置**刻意不同**:

| 情况 | 行为 |
| --- | --- |
| **同一层内**同名 | **报错**(`LoadError`),列出冲突文件。同一层出现两个同名技能,说明配置本身有问题,不猜 |
| **跨层**同名 | **高优先级静默覆盖**,不提示。这是"项目覆盖全局"的正常用法 |

覆盖发生在装载时:`merged[name]` 被替换。所以最终只有一个同名技能存在,且是优先级最高的那个。

**排除项**:`settings` 的 `skills[]` 里以 `!` 或 `-` 开头的条目是排除项,作用于**整个发现集** ——
不只是"数组里纳入的那些根"。判定方式是路径匹配:技能的 `SKILL.md` 路径等于该排除路径,**或**
位于其下。

## 4. 模型怎么看到它

`system_prompt.render_skills()` 生成的块长这样:

```xml
<available_skills>
  <skill>
    <name>code-review-checklist</name>
    <description>审代码时按这份清单逐项检查…</description>
    <location>/Users/x/proj/.qi/skills/code-review-checklist/SKILL.md</location>
  </skill>
</available_skills>
```

三条规则:

1. **只给名字、描述、位置**,正文由模型按需 `read`。描述写得准不准,直接决定模型会不会去读它 ——
   所以 `description` 里应写清"**什么时候用**",而不只是"这是什么"。
2. **没有能读文件的工具时(`read` / `bash` 都不在),整块不注入**。注入了也读不到,只会诱导模型
   去调用不存在的工具。
3. 提示词里明确要求:**`SKILL.md` 里出现相对路径时,以 `SKILL.md` 所在目录为基准解析,并在工具
   命令里用绝对路径**。

## 5. 开关

| 方式 | 作用 |
| --- | --- |
| `qi --skill <路径>` | 追加技能(可重复,**叠加**);优先级**高于全部六层**(标签 `cli`) |
| `qi --no-skills` / `-ns` | 关闭**自动发现**(六层全跳过),但 **`--skill` 仍然生效** |
| `settings.json` 的 `skillsEnabled: false` | 等价于 `--no-skills`(只关发现) |
| `settings.json` 的 `skills: [...]` | 追加来源路径(第 3 / 6 层),支持 glob、`~`、相对路径与排除项 |

`--no-skills` 与 `--skill` 的组合语义是刻意的:"把自动发现的都关掉,只跑我指定的这一个" ——
调试某个技能时最常用的姿势。

## 6. 角色私有技能(归 qi-agents)

角色(agent)可以有**自己的**技能目录:`<角色目录>/skills/<名>/SKILL.md`。它与顶层六层是两回事:

- **单层**:只扫 `skills/<名>/SKILL.md`,不递归、不接受根级 `*.md`;
- **同一角色内同名直接报错**;
- 只在跑那个角色时可见。

这部分语义归 **qi-agents** 扩展(`runtime` 里那条途径),core 只提供不认角色的 `scan_skills()`。见
[agent-config.md](agent-config.md) 与 [extensions.md](extensions.md) §7。

## 7. 与 pi 的对应

| | pi | qi |
| --- | --- | --- |
| 发现规则 | `~/.agents/skills` + 项目 `.agents/skills`(递归、含 SKILL.md 即技能) | 同构(第 1、4 层) |
| 私有层 | 无 | **多 4 层**:`~/.qi/agent/skills`、`<git根>/.qi/skills`、以及两侧 `settings.skills[]` |
| 注入方式 | `<available_skills>` + 渐进披露 | 同(共用同一形态) |
| `--skill` / `--no-skills` | 有 | 同,且组合语义一致(`--no-skills` 不影响 `--skill`) |
| 同层同名 | 报错 | 同 |
| 跨层同名 | 高优先覆盖 | 同 |

**照 pi 写会错的地方**:qi 的层数与顺序不同(§1 那张表),跨工具目录不接受根级 `*.md`,而 qi 的私有
层接受且相对路径按各自基准解析。要判断一个技能实际会不会被加载,以 `top_level_skill_dirs()` 的
顺序为准。
