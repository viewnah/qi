# 工作区与目录选择器

通用约定见 [`README.md`](README.md)。

## 一条前置认识:工作区是**派生**的

项目列表来自**会话的 `cwd`**(会话文件里记着),后端**不维护一份名单** ——
否则就会出现"名单说有三个工作区、会话文件只有一个"的两份真相。所以:

- **没有**"列表工作区"端点(`GET /api/workspaces` 只回**改过的显示名**);
- **没有**"新建工作区"端点 —— **在某个目录下新建会话就是新建工作区**;
- 目录没有显示名条目时,用目录名本身。

三种方法的语义见 `design/web.md` §18.3。

## `GET /api/workspaces`

### 响应 `WorkspaceNames` (200)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `names` | `object<string, string>` | **目录绝对路径 → 显示名**。只有改过的那些才有条目 |

```json
{"names": {"/Users/you/proj": "主项目"}}
```

## `PATCH /api/workspaces`

改**显示名**。目录不动:项目就是目录,改目录名会影响所有会话的 `cwd`。

### 请求体 `WorkspaceRename`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `cwd` | `string` | **至少 1 个字符**(必填) |
| `name` | `string` | **空串 = 恢复成目录名**(删掉这个覆盖项) |

### 响应 `WorkspaceNames` (200) —— 改完后的整张名表

## `DELETE /api/workspaces`

删除工作区:**连同它名下的会话一起删**(不可恢复)。**目录本身不删** —— qi 不动用户的文件夹。

### 查询参数(必填)

| 名 | 类型 | 说明 |
| --- | --- | --- |
| `cwd` | `string` | ≥1 字符。内部按 `normalize()` 规整(老会话的 header 可能没规范化过;macOS 上 `/tmp` 与 `/private/tmp` 是同一个目录两种写法) |

### 响应 `WorkspaceDeleted` (200)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ids` | `string[]` | **真正被删掉**的会话 id(前端据此判断"我正开着的那条是不是也没了") |
| `names` | `object<string, string>` | 删完剩下的显示名表(顺手忘掉这个目录的显示名,不留悬空覆盖项) |

| 码 | 什么时候 |
| --- | --- |
| 409 | 该工作区下**任一会话在运行中** → 整体拒绝(删掉正在写的文件会毁掉那一轮) |

```bash
curl -s 'http://127.0.0.1:3456/api/workspaces?cwd=/Users/you/proj' -X DELETE | jq '.ids | length'
```

---

## `GET /api/fs/dirs`

目录选择器(「添加工作区」用)。形状照 pi-web 的 `GET /api/cwd/browse`:
**只读、只列目录**,不列文件、不读内容 —— 少回一类信息,就少一类泄露面。

### 查询参数

| 名 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `path` | `string` | `""` | 省略 = **主目录**(前端首屏就这样)。支持 `~` 与 `~/x`;内部 `expanduser` + `realpath` 归一(`..`/软链都归到真实路径 —— 否则同一个目录会被点出两种写法,而 qi 的"项目"按 `cwd` 字符串分组,两种写法就是两个项目) |

### 响应 `DirectoryListing` (200)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `path` | `string` | 这一层的**绝对真实路径** |
| `parent` | `string \| null` | 上一级;`null` = 已在根上(前端据此禁用"上级") |
| `home` | `string` | 主目录绝对路径 |
| `roots` | `DirEntry[]` | **只有 Windows 给盘符**,其余平台为空数组 |
| `entries` | `DirEntry[]` | 直接子目录,按名字(小写)排序 |

`DirEntry` = `{name, path}`。**符号链接指向目录也算目录**(开发者常把源码放在软链后面;
`os.scandir` 的 `is_dir()` 会跟随软链)。

| 码 | 什么时候 |
| --- | --- |
| 404 | 目录不存在 |
| 400 | 指向的不是目录 |
| 403 | 没有权限读这个目录 |

```bash
curl -s 'http://127.0.0.1:3456/api/fs/dirs?path=~' | jq '.entries[0:3]'
curl -s 'http://127.0.0.1:3456/api/fs/dirs' | jq '.path'      # 主目录
```
