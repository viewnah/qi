# 技能:`/api/skills`(读 + 添加 / 删除 / 导入)

技能是 **一个目录 + 里面的 `SKILL.md`**:模型平时只看得到名字/描述/路径(渐进披露),
用到时才读正文。所以:

> **没有 `description` 的技能等于不存在** —— 顶层扫描读不到 description 就直接跳过它:
> 不进提示词、`GET /api/skills` 也看不见它。

这条规矩贯穿下面三个写端点:宁可 400,也不写一个看不见的东西出来。

写盘逻辑全部在 `qi_lab/skills.py`,**只有两个可写层**(`scope` 的取值就这两个):

| scope | 落点 | 是什么 |
| --- | --- | --- |
| `global` | `~/.qi/agent/skills/` | qi 全局私有(`QI_AGENT_HOME` 可覆盖) |
| `project` | `<cwd>/.qi/skills/` | qi 项目私有 |

**不碰**的另外四层:`~/.agents/skills` 与 `.agents/skills`(那是 Agent Skills 标准的
跨工具目录,别的工具也在读)、user/project settings 的 `skills[]`(用户手指定的路径,
可能 glob 到仓库外)。边界就一句话:**我建的我才删** —— 与 `/api/mcp` 写端点同一套词汇。

---

## 读:`GET /api/skills`

顶层技能清单(六层来源 + `settings[]` 追加 + `--skill` / 扩展提供的),按优先级合并
(高层盖低层),设置页展示用。带 `guard`,**不需要**确认头。

### 响应 `SkillList` (200)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `skills[].name` | `string` | 显示名 = frontmatter 的 `name`,没写就用目录名 |
| `skills[].description` | `string` | 没有 description 的技能**不会出现在这里** |
| `skills[].source` | `string` | 层标签,见下表 |
| `skills[].path` | `string` | `SKILL.md` 的绝对路径(根级单文件技能则是那个 `.md`) |

`source` 的取值(低 → 高优先级):

| source | 层 | 可写 |
| --- | --- | --- |
| `agents-global` | `~/.agents/skills` | ✗ |
| `qi-global` | `~/.qi/agent/skills` | ✓ `scope=global` |
| `settings-global` | user settings 的 `skills[]` | ✗ |
| `agents-project` | 祖先链上的 `.agents/skills` | ✗ |
| `qi-project` | `<cwd>/.qi/skills` | ✓ `scope=project` |
| `settings-project` | project settings 的 `skills[]` | ✗ |
| `cli` | `--skill <路径>` / 扩展的 `resources_discover` | ✗ |

同层同名 = **冲突**(装载时抛 `LoadError` → 宿主 503);跨层同名 = 合法覆盖
(高优先级盖低优先级,低的那份不再显示)。写端点的 409 就建在第一条上。

```json
{"skills": [{"name": "brave-search", "description": "搜网页",
             "source": "qi-project", "path": "/w/p/.qi/skills/brave-search/SKILL.md"}]}
```

---

## 三个写端点(**都要确认头**)

| 端点 | 干什么 | 成功 | 见下 |
| --- | --- | --- | --- |
| `POST /api/skills` | 新建 `<scope 层>/<name>/SKILL.md` | `201 SkillList` | [添加](#post-apiskills-新建) |
| `DELETE /api/skills` | 按显示名删掉该层的一个技能(整目录) | `200 SkillList` | [删除](#delete-apiskills-删除) |
| `POST /api/skills/import` | 从本机路径导一个技能进来 | `201 SkillList` | [导入](#post-apiskillsimport-导入) |

三个都要求 `X-Qi-Confirm: yes`(缺 → `428`,与写凭证、写 MCP 同一道闸)。理由:
技能是**会进提示词的代码**,而项目层就在 `.qi/skills/` 里 —— 一次误写会被 `git add .`
带走,再在下一轮对话里被模型读进去。

三个都**回更新后的完整 `SkillList`**,并且**重扫 runtime**(见文末「写完为什么立刻生效」)。

### 共同的 400(名字与结构)

| 情况 | detail 片段 |
| --- | --- |
| 名字为空 / 首尾空白 | `技能名不能为空` / `技能名不能带首尾空白…` |
| 名字带 `/` 或 `\` | `技能名不能带路径分隔符…`(路径穿越就挡在这) |
| 名字以 `.` 开头(`.`、`..`、`.hidden`) | `技能名不能以点开头` |
| 名字带控制字符 / 超过 100 字 | `不能带控制字符` / `太长(最多 100 字)` |
| 没有 description | `没有 description 的技能扫描器看不见…` |

---

<a id="post-apiskills-新建"></a>
## `POST /api/skills` 新建

```json
{"scope": "project", "name": "brave-search", "description": "搜网页", "body": "先搜再答"}
```

| 字段 | 类型 | 必给 | 说明 |
| --- | --- | --- | --- |
| `scope` | `"global" \| "project"` | ✓ | 不在闭集 → `422` |
| `name` | `string` | ✓ | 技能名 = 目录名(空/非法 → `400`) |
| `description` | `string` | ✓ | 空白 → `400`(理由见开头那条不变量) |
| `body` | `string` | ✗ | `SKILL.md` 的正文(`---` 之后的部分);不给就空 |

写出来的是:

```markdown
---
name: brave-search
description: 搜网页
---

先搜再答
```

frontmatter 走 `yaml.safe_dump`(description 里出现冒号/引号很常见),其余原样。

### 状态码

| 码 | 什么时候 |
| --- | --- |
| `201` | 成功,响应体是更新后的 `SkillList` |
| `400` | 名字不合规 / description 空 / 磁盘写不进去 |
| `409` | **同层已有同名技能**;或该层已有 `<name>` 这个路径但不是技能 |
| `422` | `scope` 不在闭集、或 body 不是 JSON |
| `428` | 缺确认头 |

```bash
curl -X POST http://127.0.0.1:3456/api/skills \
  -H 'X-Qi-Confirm: yes' -H 'Content-Type: application/json' \
  -d '{"scope":"project","name":"brave-search","description":"搜网页","body":"先搜再答"}'
```

---

<a id="delete-apiskills-删除"></a>
## `DELETE /api/skills` 删除

```json
{"scope": "project", "name": "brave-search"}
```

**按显示名删**,不是按目录名扫:手改过 frontmatter 的技能(显示 `renamed`、目录叫
`olddir`)也删得掉,而且删的是 `SKILL.md` 的**父目录**(整目录,连同 `references/`
这类附件);根级单文件技能则删那个 `.md`。

落刀前先 `resolve()` 检查目标确实在本根之内 —— 符号链接指到根外面的"技能"会被拒绝
(`400`),否则按目录名删会删到家外面去。

**只删这一层**:同名的下层技能会露出来(覆盖关系没变),这与 `DELETE /api/mcp` 同语义。
别层的技能(比如 `.agents/skills` 里那个)一律 `404` —— 它不是 qi 建的,轮不到 qi 删。

| 码 | 什么时候 |
| --- | --- |
| `200` | 成功,响应体是更新后的 `SkillList` |
| `400` | 目标不在本根之内(符号链接越界)、删不掉(权限) |
| `404` | 该层没有这个名字的技能 |
| `428` | 缺确认头 |

```bash
curl -X DELETE http://127.0.0.1:3456/api/skills \
  -H 'X-Qi-Confirm: yes' -H 'Content-Type: application/json' \
  -d '{"scope":"project","name":"brave-search"}'
```

---

<a id="post-apiskillsimport-导入"></a>
## `POST /api/skills/import` 导入

```json
{"scope": "project", "source": "/w/packs/brave-search", "name": "brave-search"}
```

| 字段 | 类型 | 必给 | 说明 |
| --- | --- | --- | --- |
| `scope` | `"global" \| "project"` | ✓ | 落到哪一层 |
| `source` | `string` | ✓ | **服务器上的路径**(不存在 → `404`) |
| `name` | `string` | ✗ | 落点名;不给 → 源里的 `name` → 目录/文件名 |

### `source` 认三种形状

| 形状 | 处理 |
| --- | --- |
| **技能目录**(含 `SKILL.md`) | 整目录复制(`references/`、脚本都跟着走) |
| **单份 `.md`** | 包成 `<name>/SKILL.md` |
| **`.zip`** | 解到临时目录后按上面两条走,结束后删临时目录 |

包着一层的容器(技能仓库 / zip 解出来的目录)也认:会向下钻至多 3 层找 `SKILL.md`
(`skills/<名>/SKILL.md` 这种布局)。**定位不到唯一一个就报 400,不猜** —— 里面有
2 个技能、或有 2 份 `.md`,都要你先挑一个;一次导错包比拒绝导入贵得多。

### 名字的优先级

`name`(显式)→ 源 SKILL.md 的 frontmatter `name` → 目录名 / 文件名(zip 根就是技能
目录时用 zip 自己的名字,临时目录名没有信息量)。

落点名与源里的 `name` 不一致时**只改 `name` 这一个字段**(其余 frontmatter 按原顺序
重发,正文一字不动)—— 否则会出现"目录叫 A、显示叫 B"的技能,而 `DELETE` 是按显示名
找的。源文件本身**永远不改**(测试钉住:导入后源的字节不变)。

### 两条防御(zip)

- **zip slip**:成员名带 `..` 或绝对路径 → `400`,不解(否则 `extractall` 会写到
  目标目录外面去);
- **zip bomb**:成员数 > 200 或解压后 > 8 MiB → `400`;
- 软链接条目直接丢弃(它指向的目标在这里无法核实)。

### 状态码

| 码 | 什么时候 |
| --- | --- |
| `201` | 成功,响应体是更新后的 `SkillList` |
| `400` | 结构不对:没有 `SKILL.md` / 没有 description / 多个候选 / 不是 `.md` / zip 越界或超限 / 名字不合规 |
| `404` | `source` 不存在 |
| `409` | 该层已有同名技能(**不覆盖**:先 `DELETE` 再导入是两步可见的操作,静默覆盖会吞掉别人手改过的版本) |
| `422` | `scope` 不在闭集 |
| `428` | 缺确认头 |

```bash
curl -X POST http://127.0.0.1:3456/api/skills/import \
  -H 'X-Qi-Confirm: yes' -H 'Content-Type: application/json' \
  -d '{"scope":"project","source":"/w/packs/brave-search.zip"}'
```

> **只收路径,不收上传体**。这个宿主跑在本机,导入的意思是"从磁盘上拿";要让浏览器
> 把文件推上来得开 `multipart/form-data`,那是另一件事(本版没有)。

---

## 写完为什么立刻生效

`QiRuntime` **按 cwd 缓存**,`top_skills` 只在构造时读一次。少一次重扫就会出现最难查的
那种不一致:

* `GET /api/skills` 说它在(那是直接读磁盘),
* 模型却用不上(提示词取自 `runtime.top_skills`)。

所以三个写端点在落盘之后都调 `WebState.refresh_skills()` —— 只重扫技能,**不走**
`QiRuntime.reload()`:那条路还会派发 `resources_discover` / `session_start` 并重启
活动会话,一个几十行的 `SKILL.md` 不该惊动扩展。重扫失败时保留旧清单(那次写已经
落盘了,报 500 只会让调用方重试,然后撞上自己的 `409`)。

## 相关

- 读端点的另一半(`GET /api/skills` 也在 `config.md` 的能力位语境里出现):[`config.md`](config.md)
- 通用约定(确认头、错误形状、能力位 `skill_write`):[`README.md`](README.md)
- 回归测试:`extensions/qi-lab/tests/test_lab_skill_api.py`(35 条:闸、两个根、三不变量、重扫、zip 防御);
  文档与路由的双向对齐由 `extensions/qi-lab/tests/test_lab_docs_api.py` 钉住

## 重扫:`POST /api/skills/reload`

返回更新后的 `SkillList`。**不改任何文件**,只让 `runtime.top_skills` 跟上磁盘。

为什么需要它:`GET /api/skills` 与系统提示词读的都是 `runtime.top_skills`(按 cwd 缓存的
快照),而缓存只在**写端点**里重扫。于是任何"没经过本宿主"的变更 —— 手工往
`~/.qi/agent/skills/` 拷目录、另一个进程(CLI / `qi store`)装的、编辑器改了 frontmatter
—— 在页面上永远看不见,**刷新浏览器也没用**(那只是再发一次同一个 GET)。TUI 有
`/reload`,这是它的 web 对应物。

| 项 | 契约 |
| --- | --- |
| 确认头 | **要**(哪怕是只读的重扫)。理由不是防误操作,而是保住"`/api/skills` 系列的 POST 一律带确认头"这条一眼可记的规矩 |
| 副作用 | 只重建 `runtime.top_skills`;不派发 `resources_discover`、不重启会话(与 `QiRuntime.reload()` 的区别见 `state.py` 的 `refresh_skills`) |
| 失败 | 读盘失败保留旧清单(不 500):那次变更已经落盘了,报错只会让调用方反复重试 |

> 商店(`/api/store/install|uninstall`)装了/卸了 **skill** 时会自动调同一个
> `refresh_skills()`,不需要再点这颗键。MCP / A2A 没有等价缓存(读端点每次重读文件),
> 但模型侧的 MCP 清单在会话开始时取,装完要新会话才用得上。
