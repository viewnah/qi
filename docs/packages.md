# 扩展的安装与声明

> 装扩展 = 装一个 pip 包,或放一个目录;`qi install` 帮你做这一步,`qi doctor` / `qi list` 负责「声明层 + 比对」。
> 相关:[cli.md](cli.md)(命令速查)、[extensions.md](extensions.md)(扩展机制与 API)、[settings.md](settings.md)(`packages` / `extensions` 字段)。

## 1. 谁负责跱这一步

`qi install` **存在**,但它只是**帮你跱一步**:调一次安装器,然后把声明写进
`settings.packages`。**它不替代“想清楚装到哪个环境”这件事** —— 三种目标环境的行为真的不同:

1. **目标环境有三种**:qi 自己的 venv、`uv tool` 托管的环境、只读的系统解释器(Homebrew / 系统 Python)。
   目标始终是 **qi 自己的解释器**(`sys.executable`),所以不会装到 cwd 的项目 venv。
   `uv tool` 的环境里**没有 pip**(uv 自己解 wheel,不 seed pip),qi 会自动改用 uv 的安装器
   (`uv pip install --python <sys.executable>`,目标不变)。qi 每次都把要跑的命令**先打出来**;
   只读解释器下安装器会自己报错,qi 把输出原样给你并补上 `uv tool install qi-coding-agent --with <包>` 那条出路。
2. **`uv tool` 会整个重建环境**。uv 文档的原话:tool 环境 "may be upgraded via `uv tool upgrade`,
   or **re-created entirely** via subsequent `uv tool install`"。所以 pip 装进去的扩展**会被抹掉**
   —— 声明还在,`qi doctor` 会把它报出来(这正是“声明层”存在的意义)。
3. **`remove` / `uninstall` 真卸包**(对齐 pi):先从指定作用域删声明,若没有别的作用域
   还声明它,就用安装器把包装卸掉;还有别的作用域声明着就只删声明、留包(除非 `--force`)
   —— pip 只有一个环境,包是共享的。
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
| `qi_mcp-0.1.1.tar.gz` / `./x.whl` | pip | **本地归档**(wheel / sdist):从文件名取 `qi_mcp-0.1.1.tar.gz` → `qi-mcp` |
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
| pip | entry point 组 `qi.extensions`(`importlib.metadata`);**只有 `settings.packages` 声明过的才会装载** |
| 目录 | `~/.qi/agent/extensions/<名>/extension.py`、项目 `.qi/extensions/<名>/extension.py` |
| 附加 | `qi -e <dir>`、`settings.extensions[]` |

> **pip 通道的声明是装载门** —— 对齐 pi:`settings.packages` 是事实来源。`pip` 装了但没声明的
> 扩展(`qi list` 会列在「已装但未声明」)**不会被加载**,写进声明才生效。
>
> **未信任的项目目录不扫**,项目级声明也不计入 —— 与装载路径同一条门控。否则未信任项目的
> `.qi/extensions/` 会让报告显得"已装",而实际一个都没加载。

同名去重:entry point 先到先得。

## 4. 两条装法,失效面不同

`qi doctor` / `qi list` 会把两条都打出来,**不替你选**。嫌两条麻烦就直接 `qi sync` ——
它按 `settings.packages` 把缺的装回来:

```bash
# ① 装进 qi 自己的解释器环境(uv tool 环境里自动改用 uv 的安装器)
qi install "qi-mcp>=0.2"

# ② 写进 uv tool 的托管依赖(uv tool 重建 / 升级后仍然在 —— 推荐)
uv tool install qi-coding-agent --with "qi-mcp>=0.2"
```

| | ① `qi install` | ② uv 托管依赖 |
| --- | --- | --- |
| 装到哪 | qi 自己的解释器环境(`sys.executable`;uv tool 环境里自动走 `uv pip --python <sys.executable>`) | uv 的 tool 环境 |
| uv tool 重建后 | **丢**(声明还在,所以 `qi doctor` 会把它报出来) | 保留 |
| 只读解释器 | 失败(安装器自己报错) | 可行 |
| 适合 | 自己管 venv 的人 | 用 `uv tool` 装 qi 的人 |

## 5. 怎么读输出

装好了但没声明(这份环境里三个扩展都是 pip 装进来的):

```text
$ qi list
                           扩展
┏━━━━━━━━━━━┳━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━┓
┃ 扩展      ┃ 通道 ┃ 版本  ┃ 来源                  ┃ 声明 ┃
┡━━━━━━━━━━━╇━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━┩
│ qi-mcp    │ pip  │ 0.1.0 │ qi-mcp 0.1.0 · env    │ 否   │
│ qi-agents │ pip  │ 0.1.0 │ qi-agents 0.1.0 · env │ 否   │
│ qi-web    │ pip  │ 0.1.0 │ qi-web 0.1.0 · env    │ 否   │
└───────────┴──────┴───────┴───────────────────────┴──────┘
  已装但未声明(3): qi-mcp、qi-agents、qi-web —— pip 扩展只有声明了才会加载;
写进 settings.packages 才生效(也是 uv tool 重建后能补回的依据)
```

> **两个「作用域」别混**:`来源` 列的 `· env` 说的是**装在哪** —— pip 扩展都装进 qi 的
> **同一个解释器环境**,所以固定是 `env`(没有“项目一份 / 全局一份”)。`声明` 列才是
> **哪一级 `settings.packages` 写了它**:`user` = `~/.qi/agent/settings.json`,
> `project` = `<git 根>/.qi/settings.json`(`qi install -l` 写的就是后者)。

声明里有一条装不上、一条认不出,并且同一个包两级都声明时:

```text
                               扩展
┏━━━━━━━━━━━┳━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┓
┃ 扩展      ┃ 通道 ┃ 版本  ┃ 来源                        ┃ 声明               ┃
┡━━━━━━━━━━━╇━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━┩
│ qi-mcp    │ pip  │ 0.1.0 │ qi-mcp 0.1.0 · env          │ 否                 │
│ qi-agents │ pip  │ 0.1.0 │ qi-agents 0.1.0 · env       │ 是(user + project) │
│ qi-web    │ pip  │ 0.1.0 │ qi-web 0.1.0 · env          │ 否                 │
│ qi-todo   │ pip  │ —     │ pip:qi-todo · settings:user │ 未安装             │
└───────────┴──────┴───────┴─────────────────────────────┴────────────────────┘
包声明:
  ✗ 声明了但没装: pip:qi-todo (settings:user)
      qi install "qi-todo"
      uv tool install qi-coding-agent --with "qi-todo"
  ⚠ 无法解析的声明: git+https://host/repo
      用 `名字 @ URL` 写法才认得出名(如 qi-mcp @ git+https://host/repo)
  已装但未声明(2): qi-mcp、qi-web —— pip 扩展只有声明了才会加载;
写进 settings.packages 才生效(也是 uv tool 重建后能补回的依据)
  qi-agents 被多级声明(user + project),以项目级为准
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
  那件事在**装载时**按「扩展有没有把宿主写进依赖」单独报(见 [extensions.md §10](extensions.md))。
- **目录通道的 PEP 723 声明**:`# /// script` 里的 `dependencies` 目前只用于上面那条宿主依赖检查;
  `qi doctor` 还**不**据它去查"这些依赖装齐了没有"。
- `settings.packages`(包声明)与 `settings.extensions`(资源路径)是**两件事**:
  前者既决定这个环境该装什么,**也是 pip 通道的装载门**(声明了才加载),后者是"到哪里去找扩展目录"。
