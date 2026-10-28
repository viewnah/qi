# MCP 声明:全局 / 项目 / 角色私有

读端点(结构视图)+ 三个写端点(增删改)。通用约定见 [`README.md`](README.md)
—— 这一节的写操作**都要 `X-Qi-Confirm: yes`**,缺则 428。

> 角色本身(它的列表与删除)在 [`agents.md`](agents.md):那一节讲 `agent.md`,这一节讲
> 各角色目录里的 `mcp.json`。

## 声明表有三层

| layer | 位置 | 谁管 |
| --- | --- | --- |
| `global` | 用户级 `mcp.json` | qi-mcp(`scope_files()`) |
| `project` | 项目 `.qi/mcp.json`(跟着仓库走) | qi-mcp |
| `role` | 每个角色目录里的 `mcp.json` | qi-agents |

覆盖规则由 **`load_layers()`** 说了算,本扩展**只读它、只写它指的那一层文件** ——
读 A 写 B 是"改了却没生效"里最贵的一种。跨层同名是**合法的**(那正是覆盖关系),
所以新建只拒绝**同层**重名。

## `GET /api/mcp`

MCP 声明表:两层 qi + 各角色私有那层。

**值一律不出宿主**(只有键名)—— 由 `tests/test_lab_api.py` 钉住。

### 响应 `McpList` (200)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sources` | `McpSource[]` | 每层一项(global → project → 各角色) |
| `unavailable` | `bool` | qi-mcp 没装 |

`McpSource`:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `scope` | `string` | `global` \| `project` \| `role` |
| `path` | `string` | 这一层的文件绝对路径(弹层要显示"改的是哪个文件") |
| `exists` | `bool` | 文件在不在 —— 区分"没这个文件"与"文件在但没写 server" |
| `role` | `string` | `scope=="role"` 时是哪个角色 |
| `servers` | `McpServerInfo[]` | |

`McpServerInfo`:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | `string` | |
| `transport` | `string` | 展示名:写了 `type` 就照它显示(`stdio` / `streamable-http` / `sse`);没写 `type` 的老声明按字段推断成 `stdio` / `http` / `socket`,都没有 → `unknown` |
| `url` | `string` | **只在 HTTP 家族(`http` / `sse`)才给**;stdio 的 `command`/`args` 一律不出 |
| `socket` | `string` | socket 传输的路径。**是结构不是密钥**(与 `url` 同档)—— 不给的话那种 server 界面上永远改不动 |
| `env_keys` | `string[]` | `env` 的键名(**值不给**),已排序 |
| `header_keys` | `string[]` | `headers` 的键名(**值不给**),已排序 |
| `disabled` | `bool` | 只认真正的 JSON `true`(`"true"` / `1` 都不算) |
| `direct_tools` | `bool \| string[]` | `true` = 全直连;数组 = 只这几个;`false` = 走代理 |
| `unknown_fields` | `string[]` | 读端不认识的字段(界面显示"未识别字段 N 个") |
| `source` | `string` | 这条声明来自哪一层 |

```bash
curl -s http://127.0.0.1:3456/api/mcp | jq '.sources[] | {scope, exists, n: (.servers|length)}'
```

---

# 写端点(三个)

三条共同的规矩:

1. **`X-Qi-Confirm: yes`**(缺 → 428),并且都绑在 `guard` 上(开着口令就要口令);
2. 返回**更新后的 `McpList`** —— 一个值都不带,`curl` 写完直接看到结果;
3. **写下去就得连得上**:传输必须在闭集里、载荷字段必须齐,否则 400。
   连不上的声明不会报错,它只是**永远不生效** —— 那是最难查的一种故障。

可写传输**闭集**(`schemas.MCP_TRANSPORTS`,顺序即下表):

| 值 | 载荷字段 |
| --- | --- |
| `stdio` | `command`(必给)、`args`、`cwd` |
| `streamable-http` | `url`(必给) |
| `sse` | `url`(必给) |
| `socket` | `socket`(必给) |
| `http` | `url`(必给)—— **老写法的推断名**,与 `streamable-http` 同族;不收下,一份老声明在界面上改一次就存不回去 |

落盘细节(三个端点共用):

- **原子写**:临时文件 + `os.replace` —— 留下半份 JSON 会让"我配了但没生效"没法诊断;
- 新文件 **0600**(声明表里可能直接写着 `env`/`headers` 的明文密钥),已有文件保留原权限;
- **顶层其它键原样保留**(`$schema` 这类):不能因为改一个 server 就把别的键抹掉;
- 缩进 2 空格、`ensure_ascii=False`、结尾换行(与手写文件同形)。

## `POST /api/mcp`

在 `scope` 那一层新增一个 server(文件不存在就建)。

### 请求体 `McpWrite`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `scope` | `"global" \| "project" \| "role"` | 写哪一层 |
| `role` | `string` | `scope=="role"` 时必给 = 角色目录名(不给 → 400;没有这个角色 → 404) |
| `server` | `McpServerEdit` | 见下 |

`McpServerEdit`:

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `name` | `string` | 必填 | 不能带首尾空白、≤200 字(一个手滑的空格会造出两个"同名" server,而界面上一模一样) |
| `transport` | `string` | `""` | 闭集之一,写进声明的 `type` |
| `url` | `string` | `""` | HTTP 家族的目标 |
| `command` | `string` | `""` | stdio 的命令(**只进不回**) |
| `args` | `string[]` | `[]` | stdio 参数(只进不回) |
| `cwd` | `string` | `""` | |
| `socket` | `string` | `""` | socket 路径 |
| `env` | `object` | `{}` | **只进不回** |
| `headers` | `object` | `{}` | **只进不回** |
| `disabled` | `bool` | `false` | 关掉(声明留着、不启用) |
| `direct_tools` | `bool \| string[]` | `false` | 直连注册(绕过代理) |

### 响应 (201) `McpList`

| 码 | 什么时候 |
| --- | --- |
| 428 | 缺 `X-Qi-Confirm: yes` |
| 409 | **同一层**已有同名 server(跨层同名是合法的,那是覆盖) |
| 400 | 名字空白/超长;传输不在闭集;stdio 没给 `command`、HTTP 家族没给 `url`、socket 没给路径;`mcp.json` 读不了或顶层不是对象;没装 qi-mcp / 没装 qi-agents(写角色层) |
| 404 | `scope=role` 但没有这个角色 |

```bash
curl -s -X POST http://127.0.0.1:3456/api/mcp \
  -H 'Content-Type: application/json' -H 'X-Qi-Confirm: yes' \
  -d '{
    "scope": "project",
    "server": {
      "name": "docs", "transport": "stdio",
      "command": "npx", "args": ["-y", "docs-mcp"],
      "env": {"DOCS_TOKEN": "sk-…"},
      "disabled": false, "direct_tools": false
    }
  }' | jq '.sources[].servers[].name'
# 响应里不会出现 sk-…:值只进不回
```

## `PATCH /api/mcp`

改一个 server:**只动送来的键**。

### 请求体 `McpPatch`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `scope` / `role` | 同上 | 定位**哪一层** |
| `name` | `string` | **改之前**的名字(定位用) |
| `patch` | `McpServerPatch` | 见下 |

`McpServerPatch` 的每个字段都默认 `None`,**全部可选**:

> **只改 `model_fields_set` 里出现过的键**;"没送这个键"与"送了 `null`"都读作不改。

这条正是「**值看不见也能改**」的实现:读端不给 `env`/`command` 的值,前端就不送这些键,
旧值原样留着 —— 保存一次不会把凭证弄丢。**空串 = 删掉那个键**(不写 `{"url": ""}`
这种看着像"未声明"、其实是"被改没了"的值)。

| patch 字段 | 语义 |
| --- | --- |
| `name` | 改名(同层重名 → 409;**按原顺序重建**,改名不搅乱这一层的排列) |
| `transport` | 换传输:`type` 改掉;若按 **kind** 判定换了种类,旧载荷字段(`command`/`args`/`cwd`/`url`/`socket`)一律清掉 —— 留着会躺一份"按 type 走 http 却还带 command"的残骸,读端按结构猜传输时会被误导 |
| `url` / `command` / `cwd` / `socket` | 字符串;空串 → 删键 |
| `args` | `string[]`;`[]` → 删键 |
| `env` / `headers` | **整体替换**(不是合并);`{}` → 删键 |
| `disabled` | `true` → 写 `disabled: true`;`false` → 删键(空键比 `false` 更诚实,读端只认真 `true`) |
| `direct_tools` | 数组非空 → 数组;`true` → `true`;`false`/`[]` → 删键 |

### 响应 (200) `McpList`

| 码 | 什么时候 |
| --- | --- |
| 428 | 缺确认头 |
| 404 | 这一层没有这个 server |
| 409 | 改成的名字在同层已存在 |
| 400 | 名字/传输/载荷校验不过(同上) |

```bash
# 只改名 + 只换 env:别的键一个都不送,command/args 就地保留
curl -s -X PATCH http://127.0.0.1:3456/api/mcp \
  -H 'Content-Type: application/json' -H 'X-Qi-Confirm: yes' \
  -d '{"scope":"project","name":"docs",
       "patch":{"name":"docs2","env":{"DOCS_TOKEN":"sk-new"},"disabled":true}}' \
  | jq '.sources[] | select(.scope=="project").servers[] | select(.name=="docs2")'
```

## `DELETE /api/mcp`

删一个 server。**只删这一层的键** —— 同名的下层声明会**露出来**(覆盖关系没变,
所以这不是"把它从 qi 里删干净",而是"这一层不再遮着下层")。

### 请求体 `McpDelete`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `scope` / `role` | 同上 | |
| `name` | `string` | 要删的名字 |

### 响应 (200) `McpList`

| 码 | 什么时候 |
| --- | --- |
| 428 | 缺确认头 |
| 404 | 这一层没有这个 server |

```bash
curl -s -X DELETE http://127.0.0.1:3456/api/mcp \
  -H 'Content-Type: application/json' -H 'X-Qi-Confirm: yes' \
  -d '{"scope":"project","name":"docs2"}' | jq '.sources[1]'
# 删空之后文件仍在:{ "mcpServers": {} } —— 404 与"文件不存在"是两件事
```

---

## 为什么值不回显(读写两侧同一条)

- **读**:`env` 值、`headers` 值、stdio 的 `command`/`args` 一律不出宿主 ——
  后三个字段是最容易直接写进明文密钥的地方(`npx -y x --token=sk-…`)。
- **写**:值必须进得来(不然没法写),但**响应回的仍是 `McpList`**,一个值都不带。

两侧各有回归测试钉住:`tests/test_lab_api.py` 的读侧、
`tests/test_lab_mcp_write_api.py` 的写侧。界面上那几个框在编辑态留空即不改,
`ui/src/components/McpPanel.test.ts` 钉的是同一条规则的表单侧。
