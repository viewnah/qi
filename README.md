# qi

[![PyPI](https://img.shields.io/pypi/v/qi-coding-agent?style=flat-square)](https://pypi.org/project/qi-coding-agent/)
[![Python](https://img.shields.io/pypi/pyversions/qi-coding-agent?style=flat-square)](https://pypi.org/project/qi-coding-agent/)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)

用 Python 写的编码 agent 框架 —— **pi-coding-agent 的 Python 实现**:同一套形态与用法(单 agent core + 扩展宿主),换到 Python 生态。

* **[qi-coding-agent](https://pypi.org/project/qi-coding-agent/)**:交互式编码 agent CLI(`qi` 命令)
* **core 就是单 agent**:会话(JSONL + 分支树)· 工具循环 · 系统提示词 · CLI + TUI · 自动压缩 · 认证 · 扩展宿主

想了解 qi:

* 读[手册](docs/index.md),先看[qi 是怎么工作的](docs/how-qi-works.md)
* 也可以**直接问 qi 自己** —— 自带手册的索引会进提示词,它按需 `read` 那些文件来回答

## 安装

前置:**Python ≥ 3.12**。

```bash
uv tool install qi-coding-agent     # 推荐:装成全局命令,独立环境
# 或 pip install qi-coding-agent    # 装进当前 venv

qi --version
qi doctor                           # 冒烟:配置 / 凭证 / 扩展 / 包声明,一次全报
```

## 快速开始

第一次用,**在界面里两步**就能跑起来:

```bash
cd /path/to/folder      # 先切到你要处理的项目目录(qi 的工作目录 = 启动时的目录)
qi                      # 裸 qi 就是界面(装完直接敲)
```

进去之后:

```text
/login                  # 选 provider(预置都在列表里)→ 遮罩输入 API key → 写 ~/.qi/agent/auth.json
/model                  # 选模型(只列解析得出凭证的;刚才没有模型时 /login 会顺带选中第一个)
```

然后直接说你要做什么。三种跑法:

```bash
qi                      # 交互界面
qi "分析这个仓库"        # 进界面,并把这句话作为首条消息发出
qi -p "分析这个仓库"     # 无头一次:stdout 只有答案(进度走 stderr)
qi --mode json "…"      # 事件流:一行一个 JSON,喂给上层程序
```

想**在命令行里把 provider 与模型配好**(脚本 / CI / 不想进界面):

```bash
qi init                     # 引导:选 provider → 加模型 → 设为默认
qi init --list-presets      # 看内置的预置 provider(国产为主:deepseek / qwen / kimi / glm / minimax …)
qi init --preset deepseek   # 一条命令物化预置并设为默认(只补缺、幂等)
qi doctor                   # 校验配置与凭证
```

全部斜杠命令见[斜杠命令](docs/slash-commands.md)。

**零配置也能跑**:基座提示词在代码里,不写任何配置就是一个能用的单 agent。
逐步说明见[快速开始](docs/quickstart.md)。

## 扩展

qi 的能力可以按需加装 —— 命令、快捷键、工具、事件钩子、终端 UI 组件都走同一套扩展机制。
**core 不内置、也不默认装**,裸 qi 就是一个能用的单 agent。

```bash
qi install <来源> [-l]  # 装:调 pip(本地目录只登记)+ 把声明写进 settings.packages(带 -l 写项目)
qi list                 # 看装了哪些(含目录通道),并与声明双向比对
qi doctor               # 「声明了没装」「装了没声明」都报出来,并给出可复制的装法
qi remove <来源>        # 移除声明;没有别的作用域还声明它就连包一起卸(--force 强制卸)
qi -e <目录>            # 本次运行临时加载一个扩展目录(TUI 与无头都生效,只本进程)
```

手工放目录也行:用户级 `~/.qi/agent/extensions/<名>/extension.py`,项目级 `.qi/extensions/<名>/extension.py`
(项目级**未信任不加载**;入口文件名固定)。

三条要点:

* **装到哪**:进的是 **qi 所在的那个解释器环境**(不是当前项目的 venv)—— `uv tool install` 会重建环境,
  所以**声明**要写在 `settings.packages` 里,重建后用 `qi doctor` 发现并补回。
* **声明与实装是两件事**:`settings.packages` 说"这个环境该装什么",`settings.extensions` 说"到哪里找扩展目录";
  `qi list` / `qi doctor` 永远做**两个方向**的比对。
* **扩展就是任意代码**:只装可信来源。写一个扩展见[写扩展](docs/extensions.md)与[终端 UI 组件](docs/tui.md);
  装法与声明层的完整写法见[扩展的安装与声明](docs/packages.md)。

## 权限与沙箱

qi **不内置**限制文件系统 / 进程 / 网络 / 凭证访问的权限系统 —— 它默认以启动它的那个用户与进程的权限运行,
`bash` 工具不做命令级过滤。需要更强的边界就**把它放进容器或沙箱**:见
[隔离运行](docs/containerization.md),以及完整的安全模型与已知缺口 [安全地运行](docs/security.md)。

## 文档

| 去处 | 内容 |
| --- | --- |
| [docs/index.md](docs/index.md) | 手册总入口:开始 / 指南(运行 · 定制 · 构建)/ 参考 |
| [docs/how-qi-works.md](docs/how-qi-works.md) | agent loop、上下文、会话、工具、信任 —— 先读这一页 |
| [docs/cli.md](docs/cli.md) | 命令行:全部选项与行为(输出 / 模型 / 会话 / 工具 / 资源 / 凭证 …) |
| [docs/slash-commands.md](docs/slash-commands.md) | TUI 里的斜杠命令(模型与设置 / 会话与上下文 / 导出 / 运行时) |
| [docs/extensions.md](docs/extensions.md) | 写扩展:工具、事件、命令、终端 UI 组件 |

手册随 wheel 发布,索引会注入提示词;**正文不塞进上下文** —— 模型按需用 `read` 打开。

## 许可证

MIT(见 [LICENSE](LICENSE))。qi 里有两处素材移植自上游 pi(两份内置调色板与压缩/分支摘要的提示词),
它们的 MIT 声明与许可全文见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md);
**qi 与 pi 的维护者没有隶属关系**,也不是官方移植。
