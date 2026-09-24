# 第三方声明(Third-Party Notices)

qi 是独立实现,但有**两处素材移植自上游项目 pi**。它们同样在 MIT 许可下分发,因此随附下面的
版权声明与许可全文 —— 这是 MIT 的明确要求(“the above copyright notice and this permission
notice shall be included in all copies or substantial portions of the Software”)。

## pi(pi-coding-agent)

- 项目:<https://github.com/earendil-works/pi>(`packages/coding-agent`;本仓记录在案的参照版本:pi v0.85.1)
- 许可:**MIT License,Copyright (c) 2025 Mario Zechner**
- 移植清单:

| qi 里的位置 | 情况 |
| --- | --- |
| `src/qi_agent/themes/dark.json`、`src/qi_agent/themes/light.json` | 两份内置调色板,取自上游 `theme/dark.json` / `theme/light.json`;键名与语义一致,qi 侧只调整了个别颜色值 |
| `src/qi_agent/compaction.py` 的 `SUMMARIZATION_SYSTEM_PROMPT` / `SUMMARIZATION_PROMPT` / `UPDATE_SUMMARIZATION_PROMPT` / `TURN_PREFIX_SUMMARIZATION_PROMPT` | 上下文压缩、分支摘要与 split-turn 的**提示词文本**,逐字移植 |
| 其余部分 | Python 代码、CLI / TUI、扩展宿主、打包与手册都是本仓自己写的;`docs/` 与 `design/pi-alignment.md` 里的上游对照是本仓重写的说明文字,只含少量短引用 |

> qi 与 pi 的维护者**没有隶属关系**,也不是官方移植。README 里"pi-coding-agent 的 Python 实现"
> 只是说明它对齐上游的形态与用法。

## pi 的 MIT 许可全文

```text
MIT License

Copyright (c) 2025 Mario Zechner

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

qi 自身:MIT License, **Copyright (c) 2026 viewnah**(见仓库根目录的 `LICENSE`)。
运行时依赖(typer / rich / textual / pydantic / litellm / PyYAML 等)各有自己的许可,
由 pip 在安装时随各自的 distribution 一并提供。
