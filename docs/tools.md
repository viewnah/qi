# 工具

> 相关文档:[agent-config-design.md](../design/agent-config-design.md)(v1 的 agent 如何引用工具)、[extensions.md](extensions.md)(扩展注册工具)、[model-config.md](model-config.md)。

## 1. 定位与原则

- 代码工具**全局注册**进 `ToolCatalog`;`tools` 三态(对齐 Claude Code):省略或 `["*"]` = 全部可用;显式名单 = allowlist 收窄;名单含未知名 → 启动报错。**硬约束**:想限制某角色,靠工具名单,不靠 description 软约束。
- 技能/资产是内容(跟 agent 走);工具是代码(全局一份)。
- 技能加载**不需要专用工具**:真实文件系统路线下,agent 用自带 `read` 读 SKILL.md,渐进披露(描述进 system prompt,用时读全文)。

## 2. v1 内置 8 个(对齐 pi core tools,含 powershell)

| # | 工具 | 作用 | 实现要点 |
| --- | --- | --- | --- |
| 1 | `read` | 读文件 | 路径 + 行范围;超长截断并提示续读 |
| 2 | `ls` | 列目录 | 目录下文件/子目录列表 |
| 3 | `find` | 文件名搜索 | 路径 + 名称/glob 模式(跟 pi,不跟 hikqin 的 `glob`) |
| 4 | `grep` | 文本搜索 | 路径 + 正则/关键字 + 行号 + 上下文;限制输出条数 |
| 5 | `write` | 整写文件 | 新建/覆盖;返回写入字节数 |
| 6 | `edit` | diff 精确编辑 | `old_text → new_text` 精确匹配替换;匹配到多处报错要求唯一化 |
| 7 | `bash` | 执行命令 | 工作目录 = 会话 cwd;解析**真正的 bash**(非系统默认 shell);输出截断 + timeout;安全策略见 §4 |
| 8 | `powershell` | 执行 PowerShell 命令 | **仅 Windows**(pi 同款);`pwsh.exe` 优先,回退 `powershell.exe`;参数与 UTF-8 前缀照搬 pi;非 Windows 给可照做的错误 |

- `bash` 的 shell 解析顺序对齐 pi(`utils/shell.js`):`settings.shellPath` → Windows 上 Git Bash 已知路径 → PATH 上的 bash → Unix `/bin/bash` → `sh` 兜底。老版 WSL 的 `C:\Windows\System32\bash.exe` 只认 stdin,走 `-s` + 管道(pi 的 `commandTransport`)∘
- 非 Windows 上 `powershell` **在册但不可用**:描述里明说“仅 Windows”,真调用时返回可照做的错误。与 pi 一致(pi 也是总是注册、调用时才报错)。
- `edit` v1 即按 pi 的精确编辑方案实现(小改动不整写,避免覆盖)。

## 3. 工具接口与结果规范

```python
class Tool:
    name: str
    description: str          # 给 LLM 看的用法说明
    parameters: dict          # JSON Schema(pydantic 生成)
    async def execute(self, args, ctx) -> ToolResult   # text + is_error

class ToolResult:
    text: str                 # 结果/错误消息
    is_error: bool = False
```

- 所有工具结果统一截断策略(超长只返回前 N 行 + 提示),对齐 pi 的 `output-accumulator`/hikqin `truncate_if_too_long`。
- 错误以结果形式返回(不进异常),让模型可读、可重试。

## 4. bash 策略(已定 v1;审批细化在 v2)

> **变更历史与设计取舍见 [bash-allowlist.md](../design/bash-allowlist.md)。**

- **已定:内置 bash 不做命令级过滤**——不筛子命令、不拦重定向、不做只读 allowlist。与 pi 取向一致(`pi docs/security.md`:*A partial in-process sandbox would be easy to misunderstand as a security boundary*)。
- 限制手段 = **工具级收窄**(对齐 pi 的 `--tools` / `defaultTools`):`tools` 三态(省略或 `*` = 全部 / 名单 = allowlist)。要“连 bash 都不给”就用**白名单**(别列 `bash`),或者用 **`disallowed_tools`**(denylist,支持 `mcp__server__*` 通配)。
  会话级还有 CLI 的四个旗标:**`-t`** 严格白名单 / **`-xt`** 排除 / **`-nt`** 全禁 / **`-nbt`** 只去内置(见 [cli.md](cli.md) §1)。
- **执行器是解析出来的真 shell**,不是系统默认 shell:`bash` 永远走 bash 系二进制(Windows 上找 Git Bash/PATH,Unix 上 `/bin/bash` → PATH → `sh`),Windows 原生走 `powershell`。`settings.shellPath` 可显式指定(对齐 pi)。旧实现用 `create_subprocess_shell`,在 Windows 上等于 cmd.exe――工具名叫 bash 却跑 cmd。
- 子进程的 stdin 是 `DEVNULL`(对齐 pi 的 `ignore`):命令读不到 TUI 的按键;超时时按**进程组**回收(子进程不会逃逸成孤儿)。
- 需要真边界时把 qi 整个进程放进容器/VM(对齐 pi `docs/containerization.md` 的路线)。
- 已定:文件路径限制在会话目录内(防越界,`validate_path` 思想,参考 hikqin filesystem.py)。
  现状:该约束只覆盖 `read`/`ls`/`find`/`grep`/`write`/`edit` 六个文件工具,**bash 不受限**(与 pi 同为进程权限模型)。
- 现状:真实文件系统 + 会话 cwd(pi 路线),非 hikqin 虚拟沙箱。
- 现状:TUI 的 `!` 手动命令不经任何过滤(用户亲手敲的操作不算模型越权)。

> 变更记录(2026-09):v1 早期实现是"命令首词只读白名单"(`READ_ONLY_FIRST` + `GIT_READ_ONLY`),
> 拦掉了 `mkdir`/`mv`/`cp`/包安装等大量正常命令,却能被 `&&` / `;` / `>` / 裸 `python` 绕过,
> 且错误提示指向一个不存在的 `[runtime] bash` 配置项。已删除,与 pi 对齐。详见 [bash-allowlist.md](../design/bash-allowlist.md)。

## 5. 预留(二期)

- 审批/确认机制(破坏性操作的交互前钩子)

MCP 已是 v1,见 [agent-config.md](agent-config.md) §8。

## 6. 决策记录

| 决策 | 结论 |
| --- | --- |
| v1 内置 | **8 个**:read / ls / find / grep / write / edit / bash / powershell(对齐 pi core tools) |
| 命名分歧 | 文件名搜索用 `find`(跟 pi),不用 `glob`(hikqin) |
| edit 方案 | v1 即 diff 精确编辑,不做整写简化版 |
| tools 三态 | 省略 / `["*"]` = 全部;显式名单 = allowlist;未知名报错(对齐 Claude Code) |
| denylist | `disallowed_tools`(角色级,Claude 同款:先 denylist 后 allowlist;两边都列到就移除) |
| clarify | v1 内置全局通用工具 |
| bash 策略 | 与 pi 对齐:**无命令级过滤**;限制靠 `tools`/`disallowed_tools` 收窄或容器/VM;文件工具路径限会话目录 |
| bash 执行器 | 解析**真 bash**(`shellPath` → Git Bash → PATH → `/bin/bash` → `sh`),不用系统默认 shell;Windows 原生走 `powershell` 工具 |
| 老版 WSL | `C:\Windows\System32\bash.exe` 走 `-s` + stdin(照搬 pi 的 `commandTransport: stdin`) |

## 7. 待定决策

- 审批交互形态(v2)
