# 示例 agents

本目录的 agent **不会自动加载**,作为样板参考。使用方式:拷贝到 agent 位置即可生效。

```bash
# 全局(所有项目可用)
cp -r code-analyst ~/.qi/agent/agents/

# 或项目私有(跟项目走,可提交共享)
cp -r code-analyst <项目>/.qi/agents/
```

同名覆盖规则:项目版静默覆盖全局版。

## code-analyst

代码分析师:只读分析型角色(硬约束:未授予 write/edit)。

目录结构即「一个 agent」的完整形态:

```
code-analyst/
├── agent.md                          # frontmatter(配置)+ 正文(system prompt)
├── assets/review-rules.md            # 通过 frontmatter include 拼入 system prompt
├── skills/code-review-checklist/     # 私有技能:自动绑定,渐进披露
│   └── SKILL.md
└── mcp.json                      # 私有 MCP server(示例 github;按需替换/删除,v1)
```

> 注:`mcp.json` 中的 server 仅本 agent 可用(私有自动绑定);示例为 github MCP,不需要就删掉,或换成你自己的 server(凭证用 `{env:XXX}` 引用)。

## 样例列表

| agent | 用途 | 工具 |
| --- | --- | --- |
| code-analyst | 代码分析(只读) | read, ls, grep, find, bash |
| writer | 文档撰写 | read, write, edit, ls, grep |
| general | 全能兜底(auto fallback) | 全部 |
| code-reviewer | 代码审查与重构建议 | read, ls, grep, find, bash |
| data-analyst | 数据查询与分析 | read, ls, find, grep, write, edit, bash, clarify |
| training-data（训练数据） | 联合训练数据知识助手（两级快捷提问示例） | 继承宿主全部可用工具 |
| training-evaluator（训练评估） | 联合训练评估助手：按任务推荐应用助手（一次、不追问） | assistant_search, assistant_recommend |
| training-situation（训练态势） | 联合训练态势研判：态势总览、推演预判、风险预警与处置建议 | 继承宿主全部可用工具 |
| doc-writer | 编写与维护文档 | read, ls, find, grep, bash, write, edit, clarify |
| test-runner | 跑测试并汇报 | read, ls, find, grep, bash |

> 后五个原本是 qi 仓库自己的 `.qi/agents/`(本地 dogfood 角色)。P-E4c 把 core 里的
> “角色”概念整体移出之后,它们先搬到这里当样例(见 design/extensions-design.md 的 E15)。
> 其中 `training-data` / `training-evaluator` / `training-situation` 现在**随 qi-agents
> 包内置发布**（`source=builtin`），零配置就能在 qi-lab / TUI 里选到；这里保留一份样例，
> 方便对照与修改。用户/项目里的同名角色会覆盖内置版本。

`training-evaluator` 依赖 **qi-assistants** 扩展：工具从项目级
`<项目>/.qi/assistants.json` 读目录，推荐卡片的链接由目录回填（模型拿不到 `url`）。
目录在 qi-lab 的「设置 → 应用助手」里维护，或先点「导入示例」。
角色名是 `训练评估`（文件名保持英文，方便命令行引用）。

`训练数据` 演示 **`opening.suggestions` 的两级结构**:一级是标题
（联合训练任务 / 联合岛屿作战 / 联合海上机动作战 / 联合边境自卫作战 /
联合跨域控要作战 / 联合防空反导作战 / 太空作战），二级是每个标题下的推荐问题。
页面初始只显示一级标题，点击标题后才展开二级问题；支持多选，全部选中即“全域”。
**全域不是一级分类**，不要把“全域”写成 title。
把它拷到 `~/.qi/agent/agents/` 或 `<项目>/.qi/agents/` 后，在 qi-lab 选择该智能体即可看到。

`training-situation`（角色名 `训练态势`）与 `训练数据` / `训练评估` 组成联合训练的三个入口：
查数据、看态势、找评估助手。它同样使用两级快捷提问，按“综合态势 / 各作战方向 /
风险与复盘”分组；默认输出是“结论 + 关键态势表 + 风险预警 + 处置建议 + 依据与假设”，
推演部分必须写清假设、时间步长和置信度，不能把推演当成已发生事实。

路由回归集:`tests/router_cases.yaml`(输入 → 期望 agent)。
**注**:它测的是 core 里的 Dispatcher(auto 分派);那个模块已随 P-E4c 移出 core,
所以这个文件在 qi-agents 落地前**不再有对应的执行者**。
