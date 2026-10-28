# 应用助手目录与训练评估推荐（落地记录）

> 状态：**P0 + P1 已实现（2026-10-26）**。`qi-lab` 的「设置 → 应用助手」负责增删改查；
> 角色「训练评估」在对话里推荐；`url` 是纯外链。
>
> 相关：`extensions/qi-assistants/README.md`、`docs/api/assistants.md`、
> `examples/agents/training-evaluator/agent.md`、`design/web.md` §16.2（`assistant_cards`）。

## 0. 最终形态

```text
<cwd>/.qi/assistants.json              ← 唯一事实源（项目级，单文件）
        │
        ├── qi-lab 设置页 ── 增删改查（POST/PATCH/DELETE/import）
        │
        └── qi-assistants 扩展
              ├── assistant_search     召回候选（不含 URL）
              └── assistant_recommend  校验 id、回填 URL、出卡片
                        │
                qi-agents 角色「训练评估」
                        │
                qi-lab 对话 → assistant_cards → 点击外链跳走
```

四条边界（都是用户拍板的）：

1. 跳转链接是**外部系统 URL**，qi 不跟目标页面交换任何数据；
2. 目录由 **qi 的 `.qi/assistants.json`** 拥有，不用数据库；
3. **只写项目级**，没有用户级、没有分层合并；
4. 不接截图里的「联合训练评估助手」原型，推荐直接发生在 qi-lab 对话里；
   推荐**一次完成，不追问、不依赖历史**。

## 1. 字段

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `id` | string | `as_` + 8 位 hex，服务端生成、不可改 |
| `name` | string | 1–40 字，全目录唯一（忽略大小写） |
| `description` | string | 1–400 字 |
| `tasks` | string[] | 1–8 条，每条 1–60 字 |
| `outputs` | string[] | 1–8 条，每条 1–60 字 |
| `url` | string | 只接受 `http(s)://` 绝对地址；无凭据、无空白、无反斜杠 |
| `keywords` | string[] | 0–12 条，只用于召回，不展示 |
| `enabled` | bool | 默认 `true`；`false` = 停用（不推荐，但记录还在） |

只有 `enabled: true` 参与推荐。文件写入是原子替换；坏 JSON / 坏字段 → 读端 503，
不静默跳过。

## 2. qi-lab API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/assistants` | 读整份目录（含已停用条目） |
| POST | `/api/assistants` | 新建（201，确认头） |
| PATCH | `/api/assistants` | 只改送来的键（确认头） |
| DELETE | `/api/assistants` | 硬删（确认头） |
| POST | `/api/assistants/import` | 导入包内示例，`merge` 跳过同名（确认头） |

- 写端点返回**整份列表视图**；`unavailable=true` 表示没装扩展；
- 能力位 `assistant_write` 由真的 import 一次 `qi_assistants.catalog` 得出；
- 请求模型 `extra="forbid"`：多一个键 / 传 `id` → 422；
- 没有 `POST /recommend`：推荐是对话的一部分，不做第二套一次性提示词。

## 3. 推荐链路

1. `assistant_search(query)`：只在 `enabled` 的条目里，对 `name`（×5）、`keywords`（×3）、`tasks`（×2）、
   `outputs`（×1.5）、`description`（×1）做中文 bigram + 英文词召回；
   空 query = 列出全部；零命中返回 `names`（目录里有什么），不硬推。
2. 模型在候选里选 1–3 个，给一句理由；
3. `assistant_recommend(ids, reasons)`：校验 id 必须来自目录，**URL 只从目录回填**，
   产出 `details["ui"] = [{"type": "assistant_cards", ...}]`。

模型**拿不到 URL**，所以编不出链接。工具不缓存，qi-lab 改完下一轮立刻生效。

## 4. 卡片

`assistant_cards` 是 `details["ui"]` 词汇表 v1.2 的新节点（纯加法：老宿主按既有
硬规则退回 JSON）。前端在 `Rows.tsx` 渲染，带外链二次校验
（`state/url.ts: isExternalHttpUrl`，拒绝非 http(s)/带凭据/空白/反斜杠）。
卡片**不在过程块里**：`groupProcess` 把工具 `details.ui` 里的 `assistant_cards` 提成
独立的 `cards` 块，放在过程块之后、结论之前。过程默认折叠，但卡片始终可见。
工具行仍留在过程块里（证据可展开），它的详情不再重复画卡，原始文本收进
「原始输出」折叠。

## 5. 安全

- 写操作：`guard` + `X-Qi-Confirm` + 校验；跨域写不存在（不做 CORS）；
- 卡片链接：后端校验 + 前端二次校验 + `target="_blank" rel="noopener noreferrer"`；
- 描述按纯文本渲染；
- 目录文件不含任何密钥；导入 body ≤ 200 条、单文件写入原子替换。

## 6. 测试与验收

- `extensions/qi-assistants/tests`：目录校验 / 原子写 / 合并导入 / 召回 / 工具 /
  角色可发现（45 例）；
- `tests/test_lab_assistants_api.py`：CRUD、确认头、422/409/503、
  能力位、导入幂等（17 例）；
- `test_lab_docs_api.py`：路由与能力位文档对拍；
- 前端：`state/url.test.ts`、`tsc --noEmit`、`vitest`、`check:design`、`npm run build`；
- 真链路（2026-10-26 实测）：在 `.qi/assistants.json` 放入 3 条示例，
  `qi -p --ext agent=训练评估 "下周要组织一次联合训练评估，先出评估方案"`，
  会话里出现 `assistant_search` ×2 + `assistant_recommend` ×1，
  `details.ui` 里是 3 张 `assistant_cards`，URL 与目录逐字一致。

## 7. 被否掉的方案

| 方案 | 为什么不做 |
| --- | --- |
| SQLite / 数据库 | 条目几十条、单写入者；目录是**内容**要跟仓库走、可 review；数据库只增加迁移与备份面 |
| `status` 三态（draft / published / archived） | `draft` 与 `archived` 行为完全相同，只是标签不同；单管理员不需要这层区分，收敛成 `enabled` 开关 |
| user / project 两层合并 | 用户明确只写项目级；少一层就少一半边界 |
| 站内路由 / 拉起 qi 角色 | 用户确认链接是外部 URL，纯跳转 |
| 一次性 `POST /recommend` | 原型不接；对话里由模型选，不需要第二套提示词 |
| 用 embedding 召回 | 几十条 + 人工 keywords 够用；多一个索引多一份失效面，且不可解释 |
| 让模型生成链接 | 模型只选 id，URL 由目录回填 —— 从根上杜绝幻觉链接 |

## 8. 后续（未做）

- 导入 / 导出任意 JSON（现在只导入包内示例）；
- 点击埋点（哪个助手被真正打开）；
- 面板里的「试推荐」；
- 多阶段成果传递（把上一环节产出带进下一环节）；
- 若未来条目数 / 并发 / 权限真的需要，再把 `catalog.py` 的存储层换成 SQLite，
  API 与工具不动。
