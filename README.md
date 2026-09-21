# qi

**用 Python 写的编码 agent 框架**,形状对齐 [pi](https://pi.dev):core 只留 pi 也有的东西
(**单 agent** + 工具 + 会话 + TUI),增值功能(MCP / 多 agent / web)全部走 **extension**。

> 现状:**core 就是单 agent**;`qi-mcp` / `qi-agents` / `qi-web` 是三个独立官方 pip 包,各自带
> 自己的手册。
>
> [总设计](design/overview.md) · [手册](docs/index.md) · [开发计划](design/PLAN.md) ·
> [怎么跑源码](docs/development.md)

## 装

```bash
pip install qi-agent                 # 或: uv tool install qi-agent
```

官方扩展按需装 —— **core 不内置、不默认装**:

```bash
pip install qi-mcp qi-agents qi-web  # 或: uv tool install qi-agent --with qi-mcp
qi doctor                            # 「声明了但没装」「装了但没声明」在这里报出来
```

## 快速开始

```bash
qi init                              # 引导配置:选 provider/模型 → 写 models.json + settings.json + auth.json
qi init --list-presets               # 内置的预置 provider(国产为主:deepseek / qwen / kimi / glm / minimax …)
qi init --preset deepseek            # 一条命令写进 models.json 并设为默认模型
qi doctor                            # 校验配置与凭证
qi                                   # 进 TUI(单 agent)
qi -p "分析这个仓库"                  # 无头一次执行
qi "分析这个仓库"                     # 进 TUI 并把这句话作为首条发出(对齐 pi)
```

CI / 脚本里跳过交互:

```bash
qi init --preset deepseek            # 预置:baseUrl + 约定环境变量 + 几个模型(只补缺、幂等)
qi init -y --provider my-proxy --model my-model \
    --base-url https://my-proxy.internal/v1 --api openai-completions --api-key sk-xxx
```

**零配置也能跑**:core 带一份**代码内默认基座提示词**。想定制基座就写 `.qi/SYSTEM.md`(整体替换,
项目 > 全局 > 代码内默认);想装角色就装 qi-agents,把角色目录放进 `.qi/agents/`。

逐步说明见 [quickstart.md](docs/quickstart.md);完整选项与交互流程见 [cli.md](docs/cli.md)、
[model-config.md](docs/model-config.md);提示词与项目上下文的推出顺序见
[system-prompt.md](docs/system-prompt.md)。

## 索引

| 去处 | 内容 |
| --- | --- |
| [docs/index.md](docs/index.md) | **手册**:使用 / 扩展与定制 / 参考 / 程序化 / 开发 |
| [design/overview.md](design/overview.md) | 总设计:架构总览、既定决策索引、技术选型、目录布局、安全总原则 |
| [design/PLAN.md](design/PLAN.md) | 开发计划、阶段表、未决清单 |
| [design/](design/) | 其余设计记录:web / 角色 / 扩展 / dispatcher / bash 策略(**改动者**读) |
| [extensions/](extensions/) | 官方扩展与各自的手册(qi-mcp / qi-agents / qi-web) |
| [examples/agents/](examples/agents/) | 角色样例(不自动加载) |
| [docs/development.md](docs/development.md) | 怎么跑源码 / 测试 / 自检 |

**两轨边界**:`docs/` 写给**使用者与模型**(随 wheel 发布:force-include 到 `qi_agent/docs/`,索引注入提示词);`design/` 写给
**改动者**(不进 wheel、不注入)。扩展的手册归**扩展自己**([extensions.md §6](docs/extensions.md)),
core 的 `docs/` 只留指针。

## 安全(三条)

- **bash 不筛命令**(对齐 pi):内置 bash 以 qi 进程权限执行任意命令。要收紧就在角色 `tools:`
  里只列需要的工具(白名单),或本次运行用 `-nt`(全禁)/ `-nbt`(只留扩展工具)/ `-t` / `-xt`
  收窄,或把进程放进容器/VM —— 进程内的半吊子过滤容易被误当成安全边界
- 扩展与角色是**全权限代码 / 可执行指令**:仅可信源。项目级 `.qi/extensions/` 需信任,未信任时
  **不加载**(`settings.defaultProjectTrust`;`ask` 现在保守判不信任)
- 凭证三源:auth store(`~/.qi/agent/auth.json`,0600)→ 约定环境变量 → `models.json` 的 `apiKey`
  引用;配置文件与导入包**零明文**

完整安全模型见 [security.md](docs/security.md);口径与已知缺口见
[design/overview.md §5](design/overview.md)。
