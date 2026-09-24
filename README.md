# qi

[![PyPI](https://img.shields.io/pypi/v/qi-coding-agent)](https://pypi.org/project/qi-coding-agent/)
[![Python](https://img.shields.io/pypi/pyversions/qi-coding-agent)](https://pypi.org/project/qi-coding-agent/)

**用 Python 写的编码 agent 框架**,是 [pi-coding-agent](https://github.com/earendil-works/pi) 的 **Python 实现** ——
同一套形态与用法,换到 Python 生态。

**core 就是单 agent**:会话(JSONL + 分支树)、工具循环、系统提示词、CLI 与 TUI、压缩、认证都在里面,
还带一个扩展宿主。给它一个目标和一个工作目录,它会读文件、跑命令、改内容,一步步把任务做完。

> **发行名是 [`qi-coding-agent`](https://pypi.org/project/qi-coding-agent/)** —— PyPI 上的 `qi-agent`
> 属于**别的项目**,别装错;import 包名是 `qi_agent`,命令是 `qi`。

## 装

前置:**Python ≥ 3.12**。

```bash
uv tool install qi-coding-agent     # 推荐:装成全局命令,独立环境
# 或 pip install qi-coding-agent    # 装进当前 venv

qi --version
qi doctor                           # 冒烟:配置 / 凭证 / 扩展 / 包声明,一次全报
```

要改 qi 本身、跑测试:克隆仓库 → `uv sync` → `uv run qi …`(editable 安装,改完立即生效)。

## 快速开始

```bash
qi init                 # 引导:选 provider → 加模型 → 设为默认(写 models.json + settings.json + auth.json)
qi init --list-presets  # 看内置的预置 provider(国产为主:deepseek / qwen / kimi / glm / minimax …)
qi init --preset deepseek   # 一条命令物化预置并设为默认(只补缺、幂等)
qi doctor               # 校验配置与凭证
```

然后三种跑法:

```bash
qi                      # 交互界面(裸 qi 就是界面)
qi "分析这个仓库"        # 进界面,并把这句话作为首条消息发出
qi -p "分析这个仓库"     # 无头一次:stdout 只有答案(进度走 stderr)
qi --mode json "…"      # 事件流:一行一个 JSON,喂给上层程序
```

不用交互时:

```bash
qi init -y --provider my-proxy --model my-model \
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
| 安装、声明、分发扩展 | `qi install`、`settings.packages` | [扩展的安装与声明](docs/packages.md) |

可跑的样例:[`examples/extensions/hello/`](examples/extensions/hello/) —— 一份 `extension.py` 把主要的面各走一遍。

## 安全

三条要知道的(完整模型见 [docs/security.md](docs/security.md)):

- **`bash` 不筛命令**:内置 bash 以 qi 进程权限执行任意命令 —— 所谓"半沙箱"最容易被误当成安全边界。
  要收紧就用工具白名单(`-t` / `-xt` / `-nt` / `-nbt`、`settings.defaultTools`),
  要真边界就**把 qi 放进容器 / VM / 受限用户**([docs/containerization.md](docs/containerization.md))。
- **扩展就是任意代码**:只装可信来源。项目级 `.qi/extensions/` **未信任不加载**
  (`qi -a` 信任、`-na` 拒绝;没表态时看 `settings.defaultProjectTrust`,`ask` 在无头下保守判不信任)。
- **凭证三源**:auth store(`~/.qi/agent/auth.json`,0600)→ 约定环境变量 → `models.json` 的 `apiKey` 引用;
  配置文件与导出包**零明文**([docs/providers.md](docs/providers.md))。

## 文档

| 去处 | 内容 |
| --- | --- |
| [docs/index.md](docs/index.md) | **手册**:开始 / 指南(运行 · 定制 · 构建)/ 参考 |
| [docs/how-qi-works.md](docs/how-qi-works.md) | agent loop、上下文、会话、工具、信任 —— 先读这一页 |
| [examples/](examples/) | 教学样例(不自动加载) |

`docs/` 是唯一的手册:它随 wheel 一起发布,索引会注入提示词;**正文不塞进上下文** ——
模型按需用 `read` 打开。

## 许可

MIT(见 [LICENSE](LICENSE))。
