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
|---|---|---|
| code-analyst | 代码分析(只读) | read, ls, grep, find, bash |
| writer | 文档撰写 | read, write, edit, ls, grep |
| general | 全能兜底(auto fallback) | 全部 |
| code-reviewer | 代码审查与重构建议 | read, ls, grep, find, bash |
| data-analyst | 数据查询与分析 | read, ls, find, grep, write, edit, bash, clarify |
| doc-writer | 编写与维护文档 | read, ls, find, grep, bash, write, edit, clarify |
| test-runner | 跑测试并汇报 | read, ls, find, grep, bash |

> 后四个原本是 qi 仓库自己的 `.qi/agents/`(本地 dogfood 角色)。P-E4c 把 core 里的
> “角色”概念整体移出之后,它们先搬到这里当样例(见 docs/extensions.md E15);
> qi-agents 扩展落地后可以再搬回 `.qi/agents/` 作为该扩展的实测样例。

路由回归集:`tests/router_cases.yaml`(输入 → 期望 agent)。
**注**:它测的是 core 里的 Dispatcher(auto 分派);那个模块已随 P-E4c 移出 core,
所以这个文件在 qi-agents 落地前**不再有对应的执行者**。
