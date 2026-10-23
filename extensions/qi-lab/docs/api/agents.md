# 角色(agents):`/api/agents`(读 + 删除)

角色是 **qi-agents 的东西**:一个角色 = 一个目录里的一份 `agent.md` —— frontmatter 的
`name` / `description` 决定它算不算角色(缺一个,`_read_role` 就当没看见),正文即 system
prompt。这个宿主对角色只做两件事:**显示**(读 qi-agents 的发现结果)与**删除**。

> **为什么只有删除、没有创建**:角色的**内容**是一段提示词 + 工具/模型限制,写入格式与
> 校验都归 qi-agents(`tools` 白名单、`delegate`、`model` 都有它的语义),宿主不该发明一个
> "看起来能写"的格式。而"删掉一个放错了/不再用的角色"是边界清楚的文件操作:两层都是 qi
> 体系写出来的目录,删完读模型立刻少一条。

写盘逻辑在 `qi_lab/agents.py`,**只有两个可删层**(`scope` 的取值就这两个):

| scope | 落点 | 说明 |
| --- | --- | --- |
| `user` | `~/.qi/agent/agents/` | 用户级(跨项目) |
| `project` | `<cwd>/.qi/agents/` | 项目级,**同名覆盖** `user` |

**词汇跟 qi-agents,不跟技能**:读模型的 `AgentInfo.source`、界面上的角标写的都是
`user` / `project` —— 删除请求就用界面上显示的那个词。`scope="global"`(技能那套)在这里
`422`,不设翻译表。

---

## 读:`GET /api/agents`

角色列表,数据来自 **qi-agents** 的 `discover(cwd, "both")`。带 `guard`,**不需要**确认头。

### 响应 `AgentList` (200)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `agents` | `AgentInfo[]` | 按名字排序 |
| `unavailable` | `bool` | qi-agents 没装 → `true`(**不是 500**:界面说清楚"装了才有角色") |

`AgentInfo`:

| 字段 | 说明 |
| --- | --- |
| `name` | 显示名 = frontmatter 的 `name`(与目录名可以不是一个东西) |
| `description` | frontmatter 的 `description`(空的角色根本不会被发现) |
| `source` | `user` \| `project` —— **覆盖后剩下的那个**(同名时项目盖住用户) |
| `tools` | 角色的 `tools:` 白名单;**空 = 继承父**,不是"全部" |
| `model` | `"provider/model"`,空 = 继承父 |
| `path` | `agent.md` 的绝对路径 |
| `mcp` | 该角色目录里 `mcp.json` 声明了几个 server |
| `opening` | 开场白 + 选择期快捷提问，见下；没写 `opening` 时为 `null` |

`AgentOpening`:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `message` | `string` | 开场白正文（可选） |
| `suggestions` | `string[]` | **选择 agent 时就能显示**的快捷提问扁平清单（兼容旧前端）；点选后作为一条 user 消息提交 |
| `groups` | `AgentSuggestionGroup[]` | 两级展示：一级分类 → 二级问题；旧平铺写法为 `[]` |

`AgentSuggestionGroup`:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `title` | `string` | 一级标题（如“联合岛屿作战”）；空串 = 全域基线建议，不占一级分类 |
| `description` | `string` | 一级说明（可选，如“可跳过，直接提问即全域问答”） |
| `items` | `string[]` | 二级问题；点选后作为一条 user 消息提交 |

前端优先读 `groups`；`groups` 为空时回落到 `suggestions`，所以旧宿主/旧角色不受影响。
**初始只显示一级标题**，不选中任何标题时不显示二级问题；点击标题后显示该组 `items`。
支持多选；全部标题都选中时是“全域”（全域 = 所有 suggestions 的合集，不是一级标题）。
无标题组是无标题基线，会追加到任意已选标题下。

`opening` 必须随**元数据**回，而不是等 `POST /api/ag-ui`：切换 agent 时还没有 run，
不能为拿建议先开一轮。这也是它与 [`ag-ui.md`](ag-ui.md) 里旧的 `qi.opening` 事件的分工：
后者是运行期事件，前者是选择期数据。

**响应里没有提示词正文** —— 角色的 `prompt` 从来没有出过这个宿主,这里只回元数据。

```json
{"agents": [{"name": "coordinator", "description": "总协调…", "source": "user",
             "tools": ["read", "subagent"], "model": "",
             "path": "/Users/me/.qi/agent/agents/coordinator/agent.md",
             "mcp": 0,
             "opening": {"message": "我是协调员", "suggestions": ["拆一下这个任务", "先看仓库结构"],
                         "groups": []}}],
 "unavailable": false}
```

两级写法的响应（一级 `groups`，二级 `items`；`suggestions` 仍然是扁平合集）：

```json
{"opening": {
  "message": "您好，我是数据知识助手",
  "suggestions": ["查询当前业务领域（全域）包含的数据产品",
                  "美军介入台海方向的第一岛链核心前沿基地群有哪些？"],
  "groups": [
    {"title": "联合训练任务", "description": "任务级通用查询入口",
     "items": ["查询当前业务领域（全域）包含的数据产品"]},
    {"title": "联合岛屿作战", "description": "岛屿方向的力量编成与前沿基地",
     "items": ["美军介入台海方向的第一岛链核心前沿基地群有哪些？"]}
  ]}}
```

---

## 写(一个):`DELETE /api/agents`(**要确认头**)

```json
{"scope": "user", "name": "coordinator"}
```

| 字段 | 类型 | 必给 | 说明 |
| --- | --- | --- | --- |
| `scope` | `"user" \| "project"` | ✓ | 删哪一层;不在闭集 → `422` |
| `name` | `string` | ✓ | **显示名**(frontmatter 的 `name`),不是目录名 |

```bash
curl -X DELETE http://127.0.0.1:3456/api/agents \
  -H 'X-Qi-Confirm: yes' -H 'Content-Type: application/json' \
  -d '{"scope":"project","name":"repo-helper"}'
```

### 它做对的四件事

1. **按显示名找,不拼路径** —— 目标由 `qi_agents.discovery.discover()` 给出,与
   `GET /api/agents` 同一条代码路径:界面上显示什么名字,就删什么名字。于是 `name`
   里带 `../` 只会"查不到"(`404`),压根没有拼路径这一步;
2. **整目录删** —— 角色目录里还有属于它的东西(`mcp.json` 那层私有声明、笔记等)。
   只删 `agent.md` 会留下一个"看不见但还在生效"的孤儿声明,所以确认层里写明:**整个
   `<目录>/` 都没了**;
3. **只删这一层** —— 跨层同名是覆盖关系:删 `user` 那份,`project` 同名的会**露出来**
   而不是被一起带走(与 `DELETE /api/mcp`、`DELETE /api/skills` 同语义);
4. **落刀前 `resolve()` 查一遍** —— 角色目录本身是指向根外的符号链接时拒绝
   (`400`),理由里带**两个路径**、能直接进确认层。`rmtree` 自己也会拒绝这种目标,
   但它只会甩一句 `Cannot call rmtree on a symbolic link`(没有根也没有目标);
   更要紧的是 `rmtree` 只检查**最后一级**,“目标不是链接、父链里有链接”那种形态它
   会顺着删下去 —— 只有 resolve 看得出来。

### 状态码

| 码 | 什么时候 |
| --- | --- |
| `200` | 成功,响应体是更新后的 `AgentList`(形状与读端点完全一样) |
| `400` | 目标不在该层根之内(符号链接越界)、删不掉(权限) |
| `404` | **该层**没有这个名字的角色(在另一层?那不归这次删) |
| `422` | `scope` 不在闭集、或 body 不是 JSON |
| `428` | 缺确认头(`X-Qi-Confirm: yes`)—— 与写凭证、写 MCP、写技能同一道闸 |
| `503` | 没装 qi-agents:读可以降级成 `unavailable=true`,删没有可删的对象,不能装作删成功 |

### 能力位

`GET /api/meta` → `capabilities.agent_write = true`。没有这位的老宿主,前端把 Agent
那一节当**只读**展示(读端点一直在)—— 少一排按钮比点了才吃 404 好。

### 删完要不要重扫

不用。技能有 `WebState.refresh_skills()`,是因为 `QiRuntime` 按 cwd 缓存 `top_skills`;
**角色不进那个缓存** —— 每次派单都现 `discover()`(`qi_agents.subagent` / `qi_agents.__init__`),
所以"读模型立刻少一条"就等于"下一次调用立刻看不见它"。

---

## 相关

- MCP 的三层声明(含**角色私有**那层 `mcp.json`):[`mcp.md`](mcp.md)
- 技能的添加 / 删除 / 导入(同一道确认闸,但 scope 词汇是 `global` / `project`):[`skills.md`](skills.md)
- 通用约定(确认头、错误形状、能力位):[`README.md`](README.md)
- 回归测试:`extensions/qi-lab/tests/test_lab_agent_write_api.py`(15 条:闸、两层、四条边界、
  503、读写同源);文档与路由的双向对齐由 `extensions/qi-lab/tests/test_lab_docs_api.py` 钉住
