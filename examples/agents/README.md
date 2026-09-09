# 示例 agents

本目录的 agent **不会自动加载**,作为样板参考。使用方式:拷贝到 agent 位置即可生效。

```bash
# 全局(所有项目可用)
cp -r code-analyst ~/.qi/agents/

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
