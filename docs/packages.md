# 扩展的安装与声明

> 装扩展 = 调 pip(或放一个目录);qi **不做**这件事,只做「**声明层 + 比对**」。
> 相关:[cli.md](cli.md) §4(命令速查)、[extensions.md](extensions.md)(扩展机制与 API)、[settings.md](settings.md)(`packages` / `extensions` 字段)。

## 1. 谁负责跱这一步

`qi install` **存在**(对齐 pi),但它只是**帮你跱一步**:调一次 pip,然后把声明写进
`settings.packages`。**它不替代“想清楚装到哪个环境”这件事** —— 三种目标环境的行为真的不同:

1. **目标环境有三种**:qi 自己的 venv、`uv tool` 托管的环境、只读的系统解释器(Homebrew / 系统 Python)。
   qi 每次都把要跑的命令**先打出来**,所以你能看出装到了哪里;只读解释器下 pip 会自己报错,
   qi 把输出原样给你并补上 `uv tool install qi-agent --with <包>` 那条出路。
2. **`uv tool` 会整个重建环境**。uv 文档的原话:tool 环境 "may be upgraded via `uv tool upgrade`,
   or **re-created entirely** via subsequent `uv tool install`"。所以 pip 装进去的扩展**会被抹掉**
   —— 声明还在,`qi doctor` 会把它报出来(这正是“声明层”存在的意义)。
3. **`remove` 不卸包**(与 pi 同义):它只从声明里移除,并把 `pip uninstall` 命令给你。
   装/卸包是 pip 的事,声明是你的事。
4. **声明与实装始终是两件事**:声明说"这个环境该有什么",实装由 pip 决定。所以 `qi list` /
   `qi doctor` 永远做**两个方向**的比对 —— 即使你用的是 `qi install`。

## 2. 声明怎么写

`settings.json` 的 `packages` 是字符串数组(也容忍 `{"name": …}` 这种结构体写法):

```json
{ "packages": ["pip:qi-mcp>=0.2", "local:./extensions/qi-agents"] }
```

| 写法 | 通道 | 名字从哪来 |
| --- | --- | --- |
| `pip:qi-mcp` | pip | 包名 |
| `qi-mcp` | pip | 同上(前缀可省) |
| `qi-mcp>=0.2` | pip | 名字 + 版本约束(整个 spec 会进装法) |
| `Qi.MCP` | pip | **PEP 503 归一** → `qi-mcp`(与「已装」侧同一规则,否则一边算命中一边不算) |
| `qi-mcp @ git+https://host/repo` | pip | **显式名字**(PEP 508 的 `名字 @ URL`) |
| `local:./extensions/qi-mcp` | 目录 | 目录名 |
| `/abs/path`、`./rel`、`~/x`、`file:…` | 目录 | `Path(...).name` |
| `{"name": "pip:qi-mcp"}` | 按内容判 | 取 `name` / `spec` / `source` 里第一个非空的 |

> **裸 URL 故意不猜名字**:把 `git+https://host/qi-mcp` 读成一个叫 `git` 的包,比认不出更坏。
> 这种声明会被**原样回显**并提示改成 `名字 @ URL` —— 不静默丢。

作用域:用户级 `~/.qi/agent/settings.json` 与项目级 `<git 根>/.qi/settings.json` **都读**,
同名以**项目**为准(近者胜)。两份**分别解析**而不是合并数组 —— 合并会让一侧的声明落到另一侧的基准上。

## 3. 已装扩展从哪来

三个通道,与**装载路径**是同一套(所以报告不会与"实际能加载什么"错位):

| 通道 | 位置 |
| --- | --- |
| pip | entry point 组 `qi.extensions`(`importlib.metadata`) |
| 目录 | `~/.qi/agent/extensions/<名>/extension.py`、项目 `.qi/extensions/<名>/extension.py` |
| 附加 | `qi -e <dir>`、`settings.extensions[]` |

> **未信任的项目目录不扫** —— 与装载路径同一条门控。否则未信任项目的 `.qi/extensions/`
> 会让报告显得"已装",而实际一个都没加载。

同名去重:entry point 先到先得。

## 4. 两条装法,失效面不同

`qi doctor` / `qi list` 会把两条都打出来,**不替你选**:

```bash
# ① 直接装进 qi 所在的那个解释器(最直接)
/path/to/venv/bin/python -m pip install "qi-mcp>=0.2"

# ② 写进 uv tool 的托管依赖(uv tool 重建 / 升级后仍然在 —— 推荐)
uv tool install qi-agent --with "qi-mcp>=0.2"
```

| | ① 直接 pip | ② uv 托管依赖 |
| --- | --- | --- |
| 装到哪 | `sys.executable` 对应的环境 | uv 的 tool 环境 |
| uv tool 重建后 | **丢**(声明还在,所以 `qi doctor` 会把它报出来) | 保留 |
| 只读解释器 | 失败(pip 自己报错) | 可行 |
| 适合 | 自己管 venv 的人 | 用 `uv tool` 装 qi 的人 |

## 5. 怎么读输出

装好了但没声明(这份环境里三个扩展都是 editable 装进来的):

```text
$ qi list
                            扩展
┏━━━━━━━━━━━┳━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━┓
┃ 扩展      ┃ 通道 ┃ 版本  ┃ 来源                   ┃ 声明 ┃
┡━━━━━━━━━━━╇━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━┩
│ qi-mcp    │ pip  │ 0.1.0 │ qi-mcp 0.1.0 · user    │ 否   │
│ qi-agents │ pip  │ 0.1.0 │ qi-agents 0.1.0 · user │ 否   │
│ qi-web    │ pip  │ 0.1.0 │ qi-web 0.1.0 · user    │ 否   │
└───────────┴──────┴───────┴────────────────────────┴──────┘
  已装但未声明(3): qi-mcp、qi-agents、qi-web —— 写进 settings.packages 才能在 uv
tool 重建后补回
```

声明里有一条装不上、还有一条认不出时(`packages: ["pip:qi-todo", "qi-agents", "git+https://host/repo"]`):

```text
                               扩展
┏━━━━━━━━━━━┳━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┓
┃ 扩展      ┃ 通道 ┃ 版本  ┃ 来源                        ┃ 声明   ┃
┡━━━━━━━━━━━╇━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━┩
│ qi-mcp    │ pip  │ 0.1.0 │ qi-mcp 0.1.0 · user         │ 否     │
│ qi-agents │ pip  │ 0.1.0 │ qi-agents 0.1.0 · user      │ 是     │
│ qi-web    │ pip  │ 0.1.0 │ qi-web 0.1.0 · user         │ 否     │
│ qi-todo   │ pip  │ —     │ pip:qi-todo · settings:user │ 未安装 │
└───────────┴──────┴───────┴─────────────────────────────┴────────┘
包声明:
  ✗ 声明了但没装: pip:qi-todo (settings:user)
      /…/bin/python3 -m pip install "qi-todo"
      uv tool install qi-agent --with "qi-todo"
  ⚠ 无法解析的声明: git+https://host/repo
      用 `名字 @ URL` 写法才认得出名(如 qi-mcp @ git+https://host/repo)
  已装但未声明(2): qi-mcp、qi-web —— 写进 settings.packages 才能在 uv tool 重建后补回
```

四类信息各自的含义:

| 输出 | 含义 | 该怎么办 |
| --- | --- | --- |
| 表格里的普通行 | 实际能加载的扩展(通道 / 版本 / 来源 / 是否声明) | — |
| `未安装` 的行 + `✗ 声明了但没装` | 声明在、加载不到 —— 最可能是 `uv tool` 重建把它抹了 | 复制下面给出的命令 |
| `⚠ 无法解析的声明` | 这条声明 qi 不认识(典型:裸 URL) | 改成 `名字 @ URL` |
| `已装但未声明` | 装了,但 `settings.packages` 里没有 | 写上 —— 否则下次重建后没人知道它该在 |

`qi doctor` 的包声明节与 `qi list` 用**同一个**打印函数,所以两边一致;doctor 还会顺带查配置、
凭证与扩展的宿主依赖契约(见 §7)。

## 6. 报告的数据结构

嵌入方拿到的是 `packages.PackageReport`:

| 字段 | 含义 |
| --- | --- |
| `installed` | 实际可加载的扩展(三个通道,去重后) |
| `declared` | 可解析的声明(项目覆盖用户) |
| `missing` | 声明了但没装 —— **每一个都带可复制的装法** |
| `undeclared` | 装了但没声明 |
| `unparsed` | 认不出的声明(保留**原始 spec**,回显给用户) |
| `consistent` | 上面三个偏差列表都空 |

两个方向**不合成一个数**:`missing` 与 `undeclared` 的处置完全不同(一个是"装回来",
一个是"补声明"),合成一个"不一致"会让人无从下手。

## 7. 边界

- 这是**声明与实装的比对**,不是依赖求解器:qi 不去读每个扩展的 `requires()` 算冲突树。
  那件事在**装载时**按「扩展有没有把宿主写进依赖」单独报(见 [extensions.md §5.5](extensions.md))。
- **目录通道的 PEP 723 声明**:`# /// script` 里的 `dependencies` 目前只用于上面那条宿主依赖检查;
  `qi doctor` 还**不**据它去查"这些依赖装齐了没有"。
- `settings.packages`(包声明)与 `settings.extensions`(资源路径)是**两件事**:
  前者是"这个环境该装什么",后者是"到哪里去找扩展目录"。
