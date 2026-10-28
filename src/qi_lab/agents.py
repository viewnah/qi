"""角色(agent)的删除 —— **角色由 qi-agents 定义与发现,这个宿主只负责删**。

一个角色 = 一个目录里的一份 `agent.md`:frontmatter 的 `name` / `description` 是它的
可见性判据(`_read_role` 缺一个就 return None),正文即 system prompt。两层,与
`qi_agents.discovery` 的发现口径逐字一致:

    ~/.qi/agent/agents/<目录>/agent.md      scope = "user"
    <cwd>/.qi/agents/<目录>/agent.md        scope = "project"(同名覆盖 user)

为什么 scope 叫 `user` / `project` 而不是技能那套 `global` / `project`:这里的词汇
是 qi-agents 的 —— `GET /api/agents` 回的 `source` 字段、界面上的角标写的都是
`user` / `project`。**删除请求用的就是界面上显示的那个词**,不另设翻译表。

四条边界(都配了测试,见 `tests/test_lab_agent_write_api.py`):

1. **只删这一层** —— 跨层同名是覆盖关系:删掉 `user` 那份,`project` 同名的会
   *露出来*而不是被一起带走(与 `DELETE /api/mcp`、`DELETE /api/skills` 同一条规矩)。
2. **我管的我才删** —— 只有上面两个目录是 qi 体系写出来的;qi-agents 还认包内置
   角色与 `agentScope`,那些不归这里管。
3. **不自己拼路径** —— `name` 是**查找键**,目标由 `qi_agents.discovery.discover()`
   给出,与 `GET /api/agents` 同一条代码路径:界面上显示什么名字,就删什么名字。
   于是名字里带 `../` 之类只会"查不到"(404),压根拼不出路径;查到之后再做一次
   resolve 包含性检查,拿带两个路径的理由拦下"角色目录是指向根外的符号链接"
   这种情况(详见 `_within` 的说明)。
4. **整目录删** —— 目录里的 `mcp.json` 等私有属于这个角色,一起消失;确认层里
   把这点写明。

删除**不需要**像技能那样 `refresh_skills()`:runtime 缓存的是 `top_skills`,角色是
每次调用工具时现 `discover()` 的(`qi_agents.subagent` / `qi_agents.__init__`),所以
"读模型立刻变"就是"下一次派单立刻变"。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal, cast

from .core import AGENTS_DIR_NAME, paths

#: 只有这两层可删(与读模型 `source` 的取值域一致)
AgentDeleteScope = Literal["user", "project"]


def available() -> bool:
    """qi-agents 是否可用(只 import,不读文件)。"""
    try:
        import qi_agents.discovery  # noqa: F401
    except ImportError:
        return False
    return True


AgentScope = Literal["user", "project", "both"]


def discover(cwd: Path | str | None, scope: AgentScope = "both"):
    """qi-agents 的角色发现。没装时抛 ImportError,由调用方映射成 unavailable / 503。"""
    from qi_agents.discovery import discover as _discover

    root = Path(cwd) if isinstance(cwd, str) else cwd
    return _discover(root, scope)


class AgentError(Exception):
    """越界(400)/ 该层没有(404)/ qi-agents 没装(503)。`status` 是要回的状态码。"""

    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.status = status


def agent_root(scope: str, cwd: Path | str) -> Path:
    """`scope` 那一层的角色根(`~/.qi/agent/agents` 或 `<cwd>/.qi/agents`)。

    与 `qi_agents.discovery._global_agents_dir` / `_project_agents_dir` 同口径 ——
    那两个是私有函数,这里用它们背后的 `qi_agent.paths`(core,总是可用)重算一遍。
    **不创建目录**:根存不存在是读端的事。
    """
    if scope == "user":
        return paths.global_home() / AGENTS_DIR_NAME
    if scope == "project":
        return paths.project_home(Path(cwd)) / AGENTS_DIR_NAME
    raise AgentError(f"不认识的 scope: {scope}(可选 user / project)")


def find(cwd: Path | str, scope: str, name: str) -> Path:
    """该层里 frontmatter `name` 等于 `name` 的角色的 `agent.md` 路径;没有 → 404。

    走 `discover()` 而不是自己拼 `<root>/<name>/agent.md`:目录名与显示名可以不是
    一个东西(手改过 frontmatter 的角色),界面上显示的是 `name`,删就得按 `name` 找。
    同层两个目录写了同一个 `name` 时 `discover()` 里后者胜 —— 那也正是读端显示的
    那一个,删它与所见一致。
    """
    agent_root(scope, cwd)                      # 顺手把未知 scope 拦在 400
    try:
        from qi_agents.discovery import discover
    except ImportError as exc:
        raise AgentError("角色由 qi-agents 提供,没装就没有可删的对象", 503) from exc
    # 内置层与远端不是这个宿主管的:同名内置角色既不能被用户目录“删掉”,也不该
    # 因为界面显示了它就被这个端点当成可删目标。发现之后按 source 再筛一次,
    # 不依赖 qi-agents 是否支持额外的 include_* 开关(宿主与扩展可以独立升级)。
    role = discover(Path(cwd), cast(AgentScope, scope)).get(str(name))      # 键 = frontmatter 的 name
    if role is None or role.path is None or role.source != scope:
        raise AgentError(f"该层没有这个角色: {name}", 404)
    return Path(role.path)


def _within(path: Path, root: Path) -> bool:
    """`path` 是否落在 `root` 之内(不含等于)。**先 resolve 再比**:指向根外的
    符号链接在这里会露馅。

    拦在 `rmtree` 之前有两个理由:①给出带两个路径的明确原因(中文,能直接进
    弹层),`rmtree` 自己的 `Cannot call rmtree on a symbolic link` 既没有根也没有
    目标;②`rmtree` 只检查**最后一级**是不是链接 —— “目标不是链接、但父链里有链接”
    那种形态它会顺着删下去,只有 resolve 能看出来。
    """
    real, base = path.resolve(), root.resolve()
    return real.is_relative_to(base) and real != base


def remove(cwd: Path | str, scope: str, name: str) -> Path:
    """按显示名删掉该层的一个角色(**整目录**),返回被删的目录。

    整目录删是因为角色目录里还有属于它的东西(`mcp.json` 那层私有声明等):只删
    `agent.md` 会留下一个"看不见但还在生效"的孤儿声明。确认层里把这点写明。
    """
    path = find(cwd, scope, name)
    root = agent_root(scope, cwd)
    target = path.parent                        # 角色目录
    try:
        if not _within(target, root):
            raise AgentError(f"{target}: 不在 {root} 之内(符号链接?),拒绝删除")
    except OSError as exc:                      # resolve 失败(链条断了)也是边界问题
        raise AgentError(f"{target}: 解析不了({exc})") from exc
    if not target.is_dir():
        raise AgentError(f"角色目录不存在: {target}", 404)
    try:
        shutil.rmtree(target)                   # rmtree 不跟随目录里的符号链接
    except OSError as exc:
        raise AgentError(f"{target}: 删不动({exc})") from exc
    return target
