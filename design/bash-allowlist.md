# bash 策略:变更记录与 pi 对照

> 状态:**已实施**。本文记录 qi bash 策略的一次变更——删掉"命令首词只读白名单",与 pi 对齐为
> **无命令级过滤**——并保留 pi 的设计取向作为对照。
> 文件名 `bash-allowlist.md` 是历史名(变更前本文叫《bash 白名单设计与 pi 对照》)。
> 相关:[security.md](../docs/security.md)(bash 的边界)、[overview.md §5 安全总原则](overview.md)。
> pi 侧结论取自本机安装的 `@earendil-works/pi-coding-agent` **v0.85.1** 的 `docs/`、`examples/`
> 与打包产物(未实跑,见 §6)。

## 1. 结论先行

- **qi 现在与 pi 一致:内置 bash 不做命令级过滤。** 不筛子命令、不拦重定向、不做只读 allowlist。
- 限制手段只剩**工具级收窄**:角色 `tools` 三态 + `disallowed_tools`(denylist),以及 CLI 的
  `-t`/`-xt`/`-nt`/`-nbt`。要连 bash 一起摘掉就 `disallowed_tools: [bash]`,或 `qi -nt` 全禁。
- 需要真边界时把 qi 整个进程放进容器/VM,与 pi 的 `docs/containerization.md` 路线相同。
- 旧实现("首词白名单")是本仓库最典型的**"看起来像边界、实际不是"**:
  拦掉 `mkdir`/`mv`/`cp`/包安装等大量正常命令,却能被 `&&` / `;` / `|` / `>` / 裸 `python` 绕过。
  这正是 pi 在 `docs/security.md` 里明确拒做的"半吊子进程内沙箱"。
- 三层边界里**只有 bash 那层变了**。文件路径边界(`ToolContext.guard`)与工具集三态
  (`tools` / `disallowed_tools`)原样保留,两者与 pi 的关系见 §5。

## 2. 旧实现(已删除)及其绕过面

变更前的实现只在 `src/qi_agent/tools/__init__.py` 里做了三件事:

1. `READ_ONLY_FIRST`(28 个首词)+ `GIT_READ_ONLY`(10 个 git 子命令);
2. `_bash_allowed(command)`:`shlex.split()` 后只看**首词**是否命中;
3. `_bash` 里 `if not allowed: raise ToolError(...)`。

`git` 分支是**无条件**返回错误文案的写法:

```python
return sub in GIT_READ_ONLY, f"git {sub} 不在只读列表;{WRITE_HINT}"
```

`git status` 明明放行,reason 却写着"不在只读列表";当时只在 `not allowed` 时读 reason,
所以不是线上 bug,但属于"文案与实现不符"的一类。

### 2.1 五个可绕过点(变更的直接原因)

判定只取 `shlex.split(cmd)[0]`,而 `shlex` 不理解 shell 语法,于是:

| # | 绕过 | 结果(变更前实测) |
| --- | --- | --- |
| 1 | `echo probe > /tmp/f.txt` | ✅ 落盘。重定向无人检查 |
| 2 | `echo a && rm -rf x` / `cat f \| rm -rf x` | ✅ 放行。首个 token 合法即整条放行 |
| 3 | `ls ; rm -rf x`(**运算符前必须带空格**) | ✅ 放行 |
| 4 | `python3 -c "..."` / `python -c "..."` | ✅ 放行。可任意读、写、删 |
| 5 | `cat /etc/passwd`、`env` | ✅ 放行。`guard()` 只管文件工具,bash 不受限 |

其中第 4 条尤其讽刺:`_bash_allowed` 里那个"禁止裸 python3"的分支是**死代码**——
`python3` 已经在 `READ_ONLY_FIRST` 里,永远在它之前 `return True`。

> ⚠️ 一个容易写错的细节:`ls; rm -rf x`(分号**紧贴**前一命令、不带空格)会被 `shlex`
> 切成 `['ls;', 'rm', ...]`,首词是 `'ls;'`,**不在白名单,反而被拒**。
> 能绕过的是 `ls ; rm -rf x`。所以"`;` 一定能绕"这个说法是错的,
> 真正稳定的绕过面是 `&&`、`|`、`>` 和裸解释器。

### 2.2 另外两处

- **错误提示指向不存在的配置项**:`WRITE_HINT` 说"配置 `[runtime] bash` 放开",
  但**全仓库没有任何代码读取 `[runtime]`**。`dispatcher.py` 的注释(`# B6:可配 [runtime]`)
  与 `PLAN.md` 是同一处悬空设计。
- **TUI 的 `!` 命令完全绕过白名单**:`tui.py` 的 `_exec_bash` 直接
  `create_subprocess_shell`,不调 `_bash_allowed`。这可能是有意设计(用户亲手敲的操作
  不算模型越权),但它意味着白名单只约束**模型发起**的 bash。变更后这个区别不再重要:
  `!` 与模型发起的 bash 现在行为一致。

### 2.3 变更做了什么

- 删除 `READ_ONLY_FIRST` / `GIT_READ_ONLY` / `WRITE_HINT` / `_bash_allowed` 与
  `import shlex`;`_bash` 只保留入参校验(`command` 非空,否则 `ToolError`)。
- bash 工具描述由"执行只读命令(安全白名单)"改为"执行 shell 命令(工作目录=会话目录)"。
- 测试:原 `test_tool_error_is_tagged_tool_error` 用 bash 被拒触发 `ToolError`,
  改为用 `read` 路径越界(仍然覆盖 `error="tool_error"` 契约);新增
  `test_bash_has_no_command_allowlist` 与 `test_bash_runs_arbitrary_interpreter`
  锁定"无过滤"这个契约,防止白名单被悄悄加回来。

## 3. pi 的设计(对照,与变更前不变)

### 3.1 定位:不做进程内沙箱

`docs/security.md`:

> **No Built-in Sandbox** — Pi does not include a built-in sandbox. Built-in tools can read files, write files, edit files, and run shell commands with the permissions of the pi process. Extensions are TypeScript modules that run with the same permissions.
>
> This is intentional. … **A partial in-process sandbox would be easy to misunderstand as a security boundary** while still depending on the host shell, filesystem, package managers, credentials, and extension code. Real isolation needs to come from the operating system or a virtualization/container boundary.

`docs/usage.md` 把这件事列进了设计取舍:

> It intentionally does not include built-in MCP, sub-agents, **permission popups**, plan mode, to-dos, or background bash.

在打包产物 `dist/` 里按 `allowlist` / `READ_ONLY` / `allowedCommands` / `blockedCommands` 搜过:
`allowedCommands` / `blockedCommands` 命中 0 次,`allowlist` 的 13 处全部指**工具名**,
`READ_ONLY` 唯一命中是 SQL 事务常量。`dist/core/tools/bash.js` 的参数 schema 只有
`{ command: string, timeout?: number }`,校验分支只有 timeout 合法性与 cwd 存在性。

### 3.2 实际用来"限制"的四层

| 机制 | 约束对象 | 强度 |
| --- | --- | --- |
| `defaultTools` / `--tools` / `--exclude-tools` / `--no-builtin-tools` / `--no-tools` | **工具名**,非命令 | 强(可把 `bash` 整个摘掉) |
| Project trust | **加载什么配置/扩展**,非执行 | 弱,官方声明"不是沙箱" |
| 扩展 hook `tool_call` | 命令(需自行实现) | 取决实现 |
| 容器 / VM(Gondolin、Docker) | 整个进程 | 真边界 |

### 3.3 工具级三态(pi 唯一的 allowlist)

内置工具 8 个:`read` `bash` `powershell` `edit` `write` `grep` `find` `ls`(`docs/settings.md`)。
`--tools` 是"strict allowlist for all tools",`--exclude-tools` 在其后过滤,
空数组 = 保留扩展工具、去掉全部内置。

```bash
# 连 bash 都不给,模型只能读
pi --tools read,grep,find,ls -p "Review the code"
```

这与 qi 的 `tools` / `disallowed_tools` 是同一个思路:**把 bash 整个拿走,而不是筛 bash 里的子命令。**

### 3.4 命令过滤要么靠扩展,要么靠容器

`examples/extensions/permission-gate.ts` 是官方给的范式——**黑名单 + 交互确认**,非交互时 fail-safe 默认拦:

```ts
const dangerousPatterns = [/\brm\s+(-rf?|--recursive)/i, /\bsudo\b/i, /\b(chmod|chown)\b.*777/i];

pi.on("tool_call", async (event, ctx) => {
  if (event.toolName !== "bash") return undefined;
  const isDangerous = dangerousPatterns.some((p) => p.test(event.input.command as string));
  if (isDangerous) {
    if (!ctx.hasUI) return { block: true, reason: "Dangerous command blocked (no UI for confirmation)" };
    const choice = await ctx.ui.select(`⚠️ Dangerous command:\n\n  ${event.input.command}\n\nAllow?`, ["Yes", "No"]);
    if (choice !== "Yes") return { block: true, reason: "Blocked by user" };
  }
  return undefined;
});
```

注意两点:它是**黑名单**(只列三种,不是白名单);它是**示例扩展**,默认不加载。
同目录还有 `protected-paths.ts`(拦对 `.env`、`.git/`、`node_modules/` 的 write/edit)。
qi 没有扩展系统,所以这条路线在 qi 里对应"二期审批闸门"(见 §5)。

### 3.5 plan-mode 的只读清单(唯一的"真白名单",仍是示例)

`examples/extensions/plan-mode/utils.ts` 有一份真正意义上的只读白名单:

```ts
export function isSafeCommand(command: string): boolean {
 const isDestructive = DESTRUCTIVE_PATTERNS.some((p) => p.test(command));
 const isSafe = SAFE_PATTERNS.some((p) => p.test(command));
 return !isDestructive && isSafe;   // 双向:必须同时"安全"且"不破坏"
}
```

- **双向判定**:`SAFE_PATTERNS`(50 条,`^\s*cat\b` 这种锚定式正则)与
  `DESTRUCTIVE_PATTERNS`(33 条)同时生效。
- **黑名单专门堵重定向和复合副作用**:`/(^|[^<])>(?!>)/`、`/>>/` 都在列,
  另有 `tee` / `truncate` / `dd` / `shred`。
- 覆盖的破坏面比旧 qi 白名单宽:`rm rmdir mv cp mkdir touch chmod chown chgrp ln tee truncate dd shred`、
  包管理器(`npm/yarn/pnpm/pip/apt/brew`)、`git add|commit|push|pull|merge|rebase|reset|checkout|branch -d|stash|cherry-pick|revert|tag|init|clone`、
  `sudo su kill pkill killall reboot shutdown systemctl service`、编辑器(`vim nano emacs code subl`)。
- **匹配的是整串正则,不是首词**;`isSafe` 用 `^\s*` 锚定开头。
- 它还配合 `pi.setActiveTools(...)` 直接把 `edit` / `write` 摘掉(plan-mode `index.ts`,
  `PLAN_MODE_DISABLED_TOOLS = {edit, write}`),形成**双保险**:工具摘除 + 命令过滤。

**这份清单也不是密不透风**:它只做"命中允许 + 未命中破坏性"的与运算,所以
`cat x && python -c '...'` 这种"首词合法、尾段不在黑名单"的复合命令仍会通过
(`python` 不在 `DESTRUCTIVE_PATTERNS` 里;实测 destructive=false、safe=true)。
要堵住它得再加"复合操作符"规则。这是示例扩展的边界,不是 pi 内置行为。

## 4. 对照表

| 维度 | pi (v0.85.1) | qi(变更后) | qi(变更前) |
| --- | --- | --- | --- |
| bash 命令白名单 | **无**(仅 plan-mode 示例内有) | **无** | 28 首词 + git 10 子命令 |
| 判定粒度 | 示例用整串正则(锚定开头) | — | 仅首词,`&&` / `;` / `\|` 后不校验 |
| 破坏性黑名单 | 示例有(33 条) | 无 | 无 |
| 重定向 | 示例用 `>` / `>>` 正则覆盖 | 不拦(与 pi 一致) | 不拦(实测可落盘) |
| `python -c` | 无 bash 过滤;靠 `--tools` 摘除 bash | 无过滤;靠 `disallowed_tools` 摘除 bash | 裸放行;"禁止"分支是死代码 |
| 路径边界 | **无**:绝对路径随便读 | `guard()` 限会话目录(**仅 6 个文件工具**) | 同左 |
| 工具收窄 | `defaultTools` / `--tools` / `--exclude-tools` / `--no-builtin-tools` | `tools` 三态 + `disallowed_tools` | 同左 |
| 配置信任 | Project trust(`trust.json`,`-a` / `-na`) | `-a` / `-na` + 按目录记住(TUI `/trust`,`~/.qi/agent/trust.json`) | 同左 |
| 兜底隔离 | 容器 / VM / Gondolin,文档成体系 | 未做(路线一致) | 同左 |
| 设计定位 | "不是沙箱",不做假边界 | 与 pi 同 | 轻量只读白名单,但**看起来像边界** |

## 5. 现在怎么收紧(替代旧白名单)

按成本从低到高:

1. **摘掉 bash**:agent frontmatter `disallowed_tools: [bash]`,模型只剩文件工具
   (对应 pi 的 `--tools read,grep,find,ls`)。
2. **限定工具集**:`tools: [read, ls, grep, find]`(allowlist 三态,含未知名启动报错)。
3. **容器/VM**:把整个 qi 进程放进去,只挂载需要的路径,最小化凭证 ——
   与 pi `docs/containerization.md` 同一路线。这是唯一"真边界"。
4. **二期:审批闸门**。SSE 已预留 `action.required` 位;需要时在 bash 工具执行前
   加钩子做交互确认(pi 的 `permission-gate.ts` 就是这个形态的示例)。

保留不变的两层:

- **文件路径边界**:`ToolContext.guard` 作用于 `read` / `ls` / `find` / `grep` / `write` / `edit`
  六个文件工具,解析后的路径必须落在会话工作目录内,否则
  `路径越界(仅允许会话目录内)`。bash 不受此限(与 pi 同为进程权限模型)。
- **工具集三态**:`AgentConfig.tools` / `disallowed_tools` 在装载期解析,
  名单含未知名 → 启动报错(`loader.py` → `resolves_tools`)。

## 6. 验证方式

- **qi**:读源码(`tools/__init__.py`、`tui.py`、`models.py`、`loader.py`、`dispatcher.py`、`PLAN.md`),
  并直接调用 `_bash_allowed` 复现 §2.1 的每一条绕过。变更后由
  `tests/test_contract_p0.py::test_bash_has_no_command_allowlist` /
  `::test_bash_runs_arbitrary_interpreter` 锁定"无过滤"契约。
- **pi**:**只读**打包产物与 `docs/` / `examples/`,未实跑。§3 中所有命令行行为
  (如"扩展默认不加载")据文档推断,未经实测。版本 `0.85.1`,
  路径 `node_modules/@earendil-works/pi-coding-agent/`。
