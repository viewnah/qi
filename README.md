# qi

**用 Python 写的编码 agent 框架**。给它一个目标和一个工作目录,它会读文件、跑命令、改内容,一步步把任务做完。

**core 就是单 agent**:会话(JSONL + 分支树)、工具循环、系统提示词、CLI 与 TUI、压缩、认证都在里面;
角色 / MCP / Web 这类增值功能全部走**扩展**,不塞进 core。

> **现状**:`qi-agent` 还没发布到 PyPI,以**从源码安装**为主(见下)。
> `qi-mcp` / `qi-agents` / `qi-web` 是三个独立官方 pip 包,各自带自己的手册。

## 装

前置:**Python ≥ 3.12** 与 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/viewnah/qi.git
cd qi
uv sync                  # 把 qi 装成 editable;`uv run qi …` 直接可用
uv run qi doctor         # 冒烟:配置 / 凭证 / 扩展 / 包声明,一次全报
```

官方扩展**按需单独装**(core 不内置、不默认装):

```bash
pip install -e extensions/qi-agents -e extensions/qi-mcp -e extensions/qi-web
uv run qi doctor         # 「声明了但没装」「装了但没声明」都在这里报出来,并给出装法
```

装法(自家 venv / `uv tool` / 只读解释器)与声明层(`settings.packages`)的差别见
[docs/packages.md](docs/packages.md)。

## 快速开始

```bash
uv run qi init                 # 引导:选 provider → 加模型 → 设为默认(写 models.json + settings.json + auth.json)
uv run qi init --list-presets  # 看内置的预置 provider(国产为主:deepseek / qwen / kimi / glm / minimax …)
uv run qi init --preset deepseek   # 一条命令物化预置并设为默认(只补缺、幂等)
uv run qi doctor               # 校验配置与凭证
```

然后三种跑法:

```bash
uv run qi                      # 交互界面(裸 qi 就是界面)
uv run qi "分析这个仓库"        # 进界面,并把这句话作为首条消息发出
uv run qi -p "分析这个仓库"     # 无头一次:stdout 只有答案(进度走 stderr)
uv run qi --mode json "…"      # 事件流:一行一个 JSON,喂给上层程序
```

不用交互时:

```bash
uv run qi init -y --provider my-proxy --model my-model \
    --base-url https://my-proxy.internal/v1 --api openai-completions --api-key sk-xxx
```

**零配置也能跑**:基座提示词在代码里,不写任何配置就是一个能用的单 agent。
逐步说明见 [docs/quickstart.md](docs/quickstart.md)。

## 怎么定制

| 想做 | 用 | 看 |
| --- | --- | --- |
| 改偏好(渲染模式、压缩、思考级别…) | `/settings`、`settings.json` | [设置](docs/settings.md) · [配置](docs/configuration.md) |
| 换模型 / 接本地模型 / 自建代理 | `models.json`、`qi init --preset` | [选一个模型](docs/models.md) |
| 给它一份按需加载的检查清单 | 一个含 `SKILL.md` 的目录 | [技能](docs/skills.md) |
| 加自己的工具 / 命令 / 事件钩子 | `extension.py` 里的 `register(api)` | [写扩展](docs/extensions.md) · [终端 UI 组件](docs/tui.md) |
| 换配色 | `theme`、`QI_THEME` | [主题](docs/themes.md) |
| 装 / 声明 / 分发扩展 | `qi install`、`settings.packages` | [扩展的安装与声明](docs/packages.md) |

可跑的样例:[`examples/extensions/hello/`](examples/extensions/hello/) —— 一份 `extension.py` 把主要的面各走一遍。

## 官方扩展

| 扩展 | 提供什么 | 手册 |
| --- | --- | --- |
| `qi-agents` | 角色(`agent.md`)+ 委派子任务 | [README](extensions/qi-agents/README.md) |
| `qi-mcp` | MCP server 接入(代理工具 / 直连工具) | [README](extensions/qi-mcp/README.md) |
| `qi-web` | HTTP 宿主 + 官方 Web UI | [README](extensions/qi-web/README.md) |

装完之后:`qi list` 看装了哪些、`qi doctor` 看声明与实装是否一致。

## 安全

三条要知道的(完整模型见 [docs/security.md](docs/security.md)):

- **`bash` 不筛命令**:内置 bash 以 qi 进程权限执行任意命令 —— 所谓"半沙箱"最容易被误当成安全边界。
  要收紧就用工具白名单(`-t` / `-xt` / `-nt` / `-nbt`、`settings.defaultTools`,或角色 `tools:`),
  要真边界就**把 qi 放进容器 / VM / 受限用户**([docs/containerization.md](docs/containerization.md))。
- **扩展就是任意代码,角色正文是仓库控制的提示词**:只装可信来源。项目级 `.qi/extensions/` **未信任不加载**
  (`qi -a` 信任、`-na` 拒绝;没表态时看 `settings.defaultProjectTrust`,`ask` 在无头下保守判不信任)。
- **凭证三源**:auth store(`~/.qi/agent/auth.json`,0600)→ 约定环境变量 → `models.json` 的 `apiKey` 引用;
  配置文件与导出包**零明文**([docs/providers.md](docs/providers.md))。

## 文档

| 去处 | 内容 |
| --- | --- |
| [docs/index.md](docs/index.md) | **手册**:开始 / 指南(运行 · 定制 · 构建)/ 参考 |
| [docs/how-qi-works.md](docs/how-qi-works.md) | agent loop、上下文、会话、工具、信任 —— 先读这一页 |
| [design/overview.md](design/overview.md) | 总设计:架构总览、既定决策索引、技术选型、目录布局、安全总原则 |
| [design/PLAN.md](design/PLAN.md) | 开发计划、阶段表、未决清单 |
| [design/pi-alignment.md](design/pi-alignment.md) | 与上游 pi 的逐节对照(设计口径,不进手册) |
| [design/development.md](design/development.md) | 怎么跑源码 / 测试 / 静态检查 / 仓库布局 |
| [extensions/](extensions/) | 官方扩展与各自的手册 |
| [examples/](examples/) | 教学样例(不自动加载) |

**两轨边界**:`docs/` 写给**使用者与模型**(随 wheel 发布,索引会注入提示词);`design/` 写给**改动者**
(不进 wheel、不注入提示词)。扩展的手册归**扩展自己**,core 的 `docs/` 只讲机制。

## 许可

MIT(见 [LICENSE](LICENSE))。
