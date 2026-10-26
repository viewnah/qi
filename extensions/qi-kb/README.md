# qi-kb

把 **hikqin 知识库**接成 qi 的 provider/model：一个 hikqin 账号 = 一个 `provider`，
一个知识库 = 该 provider 下的一个 `knowledgeBase`。模型通过一个 `kb` 工具检索 / 问答。

> **`store.json` 不变。** 账号信息在安装 KB 时从 qi-store 复制到 qi-kb 自己的
> `kb.json` / `kb-auth.json`；之后 qi-kb 运行时不读 `store.json`，`qi store logout`
> 也不影响已安装的知识库。

## 装

```bash
qi install qi-kb          # 或 -e ./extensions/qi-kb（开发）
```

装完 `qi` 下次启动自动发现（entry point `qi.extensions`），顶层多出 `qi kb` 子命令。

## 配置

| 文件 | 内容 | 是否含令牌 |
| --- | --- | --- |
| `~/.qi/agent/kb.json` | `providers`（账号/baseUrl/instance）+ `providers.<p>.knowledgeBases` + `defaults` | 否 |
| `<cwd>/.qi/kb.json` | 项目级覆盖（同名 provider / 同 kbId 覆盖全局） | 否 |
| `~/.qi/agent/kb-auth.json` | provider 的 access / refresh | 是，0600，只用户级 |

`kb.json` 形状：

```json
{
  "providers": {
    "alice": {
      "baseUrl": "http://10.1.43.6:7091",
      "apiPrefix": "/api",
      "instance": "hikqin-7091",
      "login": "alice",
      "knowledgeBases": {
        "54aa228821324031876bc06d94b538e9": {
          "id": "54aa228821324031876bc06d94b538e9",
          "name": "智能体平台相关案例材料",
          "docCount": 2,
          "chunkCount": 6
        }
      }
    }
  },
  "defaults": {"topK": 5, "retrievalStrategy": "hybrid", "useRerank": true},
  "defaultProvider": "alice"
}
```

- provider 名只允许字母、数字、点、下划线、短横线；
- `instance` 可选；省略时由 `baseUrl + apiPrefix` 归一化。同一个实例的内网/公网两个地址
  可以写同一个 `instance`，这样 `(instance, kbId)` 去重仍然生效；
- `kb.json` 里出现 `auth` / `access` / `refresh` 会被忽略并警告；
- **同一个 `(instance, kbId)` 只保留一份**：重复安装会提示“保留 / 覆盖（rebind）”。

## 用

```bash
# 1. 添加并登录账号（不碰 store.json）
qi kb provider add alice --base-url http://10.1.43.6:7091 -u alice
qi kb provider login alice

# 2. 看可用 KB
qi kb list --provider alice

# 3. 安装（只写绑定，不下载文档）
qi kb install 54aa228821324031876bc06d94b538e9 --provider alice

# 4. 检索 / 问答 / 文档
qi kb search "情报分析" --kb 54aa…@alice
qi kb ask "这份材料讲什么" --kb 54aa…@alice
qi kb ask "这份材料讲什么" --stream   # 边生成边打印
qi kb docs 54aa…@alice

# 5. 状态 / 已绑定
qi kb status
qi kb list --bound
```

通过商店安装（账号会从 store 源复制到 qi-kb 自己的配置）：

```bash
qi store login --store alice http://10.1.43.6:7091 -u alice
qi store install kb:<kb_id>@alice
qi store uninstall kb:<kb_id>@alice
```

重复安装同一个 KB：

```bash
qi kb install <kb> --provider bob
# → 已由 alice 安装。覆盖为 bob？[y/N]
# 非交互必须显式：
qi kb install <kb> --provider bob --rebind   # 覆盖（迁移归属）
qi kb install <kb> --provider bob --keep     # 保留原样
```

卸载 KB 不会删 provider；删账号用：

```bash
qi kb provider remove alice            # 有 KB 时拒绝
qi kb provider remove alice --cascade  # 连 KB 一起删
```

## 模型侧

注册一个 `kb` 工具：

```text
kb({"action":"list"})
kb({"action":"search","query":"情报分析"})
kb({"action":"search","query":"情报分析","kb":"项目手册@alice"})
kb({"action":"ask","question":"这份材料讲什么"})
kb({"action":"docs","kb":"54aa…@bob"})
```

- 省略 `kb` / `kb_ids` = 所有已绑定 KB；
- 跨 provider 同名时用 `名称@provider` 消歧；
- **严格引用**：`kb ask` 的答案里带 `[来源N]` 时，最终回答必须原样保留；提示词禁止模型另写 `来源：[1][2]` 或自行重编号；
- 检索/问答结果的 source 会带 `file_ref`（`kb://provider/kb_id/file_id`）、
  `filename` 与稳定的 `url`（`/api/kb/files/provider/kb_id/file_id`），
  qi-lab 代理下载，URL 可以随历史消息落盘；
- 输出有单段 / 单次字符上限，知识库正文用 `<<<KB_CONTENT … KB_CONTENT>>>` 包起来，
  提示词声明它是数据不是指令；
- 某个 provider 失败只影响它自己，结果里带 `warnings`。

## 与 qi-store / qi-lab

- **qi-store**：新增 `kb` kind，目录来自 `GET /api/rag/kb/available`；`install` 把账号复制到
  qi-kb 配置。`store.json` 的 schema 不变。
- **qi-lab**：`/api/kb/*` + 设置页「知识库」面板（provider / KB / 检索问答）。
  契约见 `extensions/qi-lab/docs/api/kb.md`。

## 设计文档

完整设计（多 provider、去重、rebind、失败矩阵、测试计划）：
[`docs/design.md`](docs/design.md)。
