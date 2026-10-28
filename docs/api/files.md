# 文件(会话目录内)

右侧文件面板的数据源。通用约定见 [`README.md`](README.md)。

## 边界:与 bash 工具**同一条**

三个端点的根目录都是**会话的 `cwd`**(给了 `session` 时)或**默认工作目录**(没给时),
路径判定用同一份 `files.guard` —— 它抄自 `ToolContext.guard`:

- 相对路径按根解析;绝对路径也收,**但必须落在根内**(软链跟随后再判定);
- 越界 → 400 `路径越界(仅允许会话目录内)`。

**同一份数据不该有两个边界**:工具读不到的,界面也不读。

## `GET /api/files`

列**一层**目录(前端懒展开,一次只取一层)。

### 查询参数

| 名 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `path` | `string` | `""` | 相对根的 POSIX 路径;空 = 根 |
| `session` | `string \| null` | `null` | 会话 id → 根换成那条会话的 `cwd`。不存在 → 404 |

### 响应 `FileListing` (200)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `root` | `string` | 根的绝对路径(= 边界) |
| `path` | `string` | 这一层的绝对路径 |
| `parent` | `string \| null` | 上一层的**相对**路径;`null` = 已在根上(禁用"上级") |
| `entries` | `FileEntry[]` | |

`FileEntry`:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | `string` | |
| `path` | `string` | **相对根**的 POSIX 路径 —— 前端拿它当 key,也拿它回传 |
| `kind` | `"dir" \| "file"` | |
| `size` | `int` | 目录恒 0 |
| `mtime` | `float` | |
| `raw` | `bool` | 能否原样预览(扩展名在白名单里)→ 前端据此选预览方式 |

**排序在服务端做**:目录在前、文件在后,各自按**数字感知**排(`file2` 在 `file10` 前,
与 `Intl.Collator(…, {numeric: true})` 同一条)。只有这里知道"哪个是目录",所以由它给顺序。
跟随软链(指向目录的软链算目录);套接字/设备文件不给看;列到一半被删的条目跳过,不让整次列举失败。

| 码 | 什么时候 |
| --- | --- |
| 400 | 路径越界 / 不是一个目录 |
| 404 | 目录不存在 / 会话不存在 |
| 403 | 没有权限读取这个目录 |

## `GET /api/files/content`

文本预览。**二进制与超限不是错误**:`kind` 是判别位,前端显示一行说明而不是空白。

### 查询参数

同 `GET /api/files`(`path` **必填**,`session` 可选)。

### 响应 `FileContent` (200)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `path` | `string` | 回显请求里的路径 |
| `kind` | `"text" \| "binary"` | `binary` = 能看见这个文件、但不预览内容 |
| `size` | `int` | 文件总大小 |
| `truncated` | `bool` | 只回了前一段(文本上限 **512 000 字符**) |
| `lang` | `string` | 语言提示(高亮/标题用,**不参与安全判断**);`binary` 时为空 |
| `text` | `string` | 内容;`binary` 时为 `""` |

判成 `binary` 的两种情况:**含 NUL 字节**(比按扩展名猜准得多的"不是文本"信号)或
**非 UTF-8**。两者都回 `kind="binary"` 而不是抛异常 —— 这是正常结果,该由返回值表达。

| 码 | 什么时候 |
| --- | --- |
| 400 | 路径越界 / 不是一个文件 |
| 404 | 文件不存在 / 会话不存在 |
| 403 | 没有权限读取这个文件 |

## `GET /api/files/raw`

原字节预览(图片 / PDF / HTML)。

### 查询参数

同上(`path` 必填)。

### 响应 (200) —— 原始字节

`Content-Type` 按扩展名给。三条响应头是**安全设计的一部分,不是可选项**:

| 头 | 值 | 为什么 |
| --- | --- | --- |
| `X-Content-Type-Options` | `nosniff` | 别让浏览器把回的东西当别的类型解释 |
| `Content-Disposition` | `inline` | 是预览不是下载 |
| `Cache-Control` | `no-store` | |
| `Content-Security-Policy` | `sandbox`(**只对 HTML**) | 把预览的 HTML 放进不透明源:脚本不执行、拿不到本站的 cookie/凭证。前端那头还会再加一层 `<iframe sandbox>`(双保险) |

**白名单**(刻意收窄):

| 扩展名 | MIME |
| --- | --- |
| `.png` `.jpg` `.jpeg` `.gif` `.webp` `.bmp` `.ico` `.avif` | `image/*` |
| `.pdf` | `application/pdf` |
| `.html` `.htm` | `text/html` |

上限:**8 MiB**(超了回"太大"也不让浏览器去吞)。

| 码 | 什么时候 |
| --- | --- |
| 415 | 扩展名不在白名单(消息 `不支持原样预览的类型: …`) |
| 400 | 路径越界 / 不是一个文件 / **文件太大** |
| 404 | 文件不存在 / 会话不存在 |
| 403 | 没有权限读取这个文件 |

---

## 例

```bash
# 列根(或某条会话的目录)
curl -s 'http://127.0.0.1:3456/api/files?session=<sid>' | jq '{root, n: (.entries|length)}'
curl -s 'http://127.0.0.1:3456/api/files?path=src' | jq '.entries[] | select(.kind=="dir").name'

# 预览一个文件
curl -s 'http://127.0.0.1:3456/api/files/content?path=README.md' | jq '{kind, size, truncated, lang}'

# 图片原字节(直接落盘)
curl -s 'http://127.0.0.1:3456/api/files/raw?path=docs/logo.png' -o logo.png

# 越界会被拒(400)
curl -i 'http://127.0.0.1:3456/api/files/content?path=../../etc/passwd'
```
