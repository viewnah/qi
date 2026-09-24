"""应用设置 `settings.json`(对齐 pi)。

位置(高 → 低):

    <cwd>/.qi/settings.json      项目覆盖(可提交共享;与 `~/.qi/agent` 配对)
    ~/.qi/agent/settings.json      全局(对齐 pi 的 `~/.pi/agent/settings.json`)

合并规则同 pi:键级深合并,项目覆盖全局;数组整体替换(不逐项合并)。
路径类字段(extensions / skills)相对**各自 settings.json 所在目录**
解析,绝对路径与 `~` 都支持;`!pat` / `-pat` 为排除项,`+path` 为强制纳入。

本模块只负责"JSON 文件的装载/合并/写入"这一层,models.json 的语义在 config.py。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .paths import SETTINGS_FILE_NAME, settings_file_candidates

SCOPES = ("user", "project")


class SettingsError(Exception):
    """settings.json 读取/校验/写入失败。"""


# ── 通用 JSON 装载与合并 ────────────────────────────────

def read_json(path: Path) -> dict[str, Any]:
    """读取 JSON 对象文件;空文件视为 {};失败抛 SettingsError(带路径)。"""
    try:
        with path.open("rb") as fh:
            raw = fh.read()
        if not raw.strip():
            return {}
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SettingsError(f"读取 {path} 失败: {exc}") from exc
    return data if isinstance(data, dict) else {}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """键级深合并:override 覆盖 base;嵌套 dict 递归合并;数组整体替换。"""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# ── 设置模型 ────────────────────────────────────────────

class QiSettings(BaseModel):
    """settings.json 的已知字段;未知字段原样保留(extra=allow)。

    字段名与 pi 一致,便于两边共享同一个文件;`skillsEnabled` 是 qi 扩展
    (pi 用 `--no-skills` 关闭技能)。
    """

    model_config = {"extra": "allow"}  # type: ignore[assignment]

    # 模型
    defaultProvider: str | None = None
    defaultModel: str | None = None
    defaultThinkingLevel: str | None = None
    #: 按模型的思考级别(pi 同名段):`{"provider/模型": "high"}`。
    #: 优先级见 `QiRuntime.__init__`:`--thinking` > `--model :级别` > 这里 > 全局默认。
    modelThinkingLevels: dict[str, str] = Field(default_factory=dict)
    #: 每个思考级别的 token 预算(pi 同名段)。**只对 Anthropic 形态生效**(litellm 只在那里
    #: 有对应参数);不配就不带(pi 另有内置默认表,qi 没有 —— 差异见 design/pi-alignment.md)。
    thinkingBudgets: dict[str, int] = Field(default_factory=dict)
    #: 分支摘要(`/tree` 跳转时):`{"skipPrompt": true}` —— 不问也不摘要(pi 同名段)。
    #: pi 的 `reserveTokens` **未接**:qi 的摘要预算固定,加一个不生效的旋钮不如不加。
    branchSummary: dict[str, Any] = Field(default_factory=dict)
    enabledModels: list[str] | None = None

    # 界面
    theme: str | None = None
    quietStartup: bool = False
    #: TUI 模式(pi 同名键 `tuiMode`):`regular` = inline(不占全屏、终端自己管回滚缓冲);
    #: `fullscreen` = 备用屏(qi 拥有视口,滚轮只滚 transcript)。
    #: **qi 默认 fullscreen,与 pi 的默认(regular)相反** —— 取舍与理由见 `design/internals.md` 的终端界面一节。
    tuiMode: str | None = None
    defaultProjectTrust: str = "ask"
    doubleEscapeAction: str = "tree"
    hideThinkingBlock: bool = False
    # 编辑器左侧内边距:pi 默认 0,qi 视觉基线用 1
    editorPaddingX: int = 1
    outputPad: int = 1                      # 助手输出的左侧缩进(pi 默认 1)
    autocompleteMaxVisible: int = 5         # 补全面板最多几行(pi 默认 5)

    # 会话 / 工具
    sessionDir: str | None = None
    defaultTools: list[str] | None = None
    # bash 用哪个 shell(对齐 pi 的 shellPath);空 = 按平台解析(见 tools/shell.py)
    shellPath: str | None = None

    # 运行行为
    compaction: dict[str, Any] | None = None
    retry: dict[str, Any] | None = None

    # 资源
    packages: list[Any] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    # `prompts` / `themes` **删掉了**:qi 没有 prompt 模板、也不支持自定义主题文件 ——
    # 留着字段却没人读,用户写进去毫无反应,而 `extra: allow` 又让人分不出
    # "我拼错了"与"qi 没实现"。要做那两个功能就把字段一起加回来。
    extensions: list[str] = Field(default_factory=list)
    enableSkillCommands: bool = True
    skillsEnabled: bool = True


# ── 装载 ────────────────────────────────────────────────

def settings_scope_path(scope: str, cwd: Path | None = None) -> Path:
    """某作用域的 settings.json 路径(写入用)。"""
    for path, name in settings_file_candidates(cwd):
        if name == scope:
            return path
    raise SettingsError(f"未知作用域 {scope!r}(可选:{', '.join(SCOPES)})")


def load_settings_raw(cwd: Path | None = None) -> tuple[dict[str, Any], list[Path]]:
    """按优先级装载并深合并,返回 (merged, 实际读取的文件列表(高→低))。"""
    merged: dict[str, Any] = {}
    loaded: list[Path] = []
    for path, _scope in reversed(settings_file_candidates(cwd)):  # 低 → 高
        if not path.is_file():
            continue
        merged = deep_merge(merged, read_json(path))   # read_json 失败已带路径报错
        loaded.append(path)
    return merged, list(reversed(loaded))


def load_settings(cwd: Path | None = None) -> tuple[QiSettings, list[Path]]:
    """装载校验后的设置;无文件时返回默认值。"""
    raw, loaded = load_settings_raw(cwd)
    try:
        return QiSettings.model_validate(raw), loaded
    except Exception as exc:  # pydantic.ValidationError
        raise SettingsError(f"{SETTINGS_FILE_NAME} 校验失败: {exc}") from exc


def load_settings_by_scope(cwd: Path | None = None) -> dict[str, QiSettings]:
    """每个作用域**各自**解析后的设置(不合并),缺失的作用域不出现在结果里。

    资源路径必须按这个结果解析:pi 的语义是 `~/.pi/agent/settings.json` 里的路径
    相对于 `~/.pi/agent`,`<cwd>/.pi/settings.json` 里的相对于 `.pi`。用合并后的
    数组会让一侧的路径落到另一侧的基准目录上。
    """
    out: dict[str, QiSettings] = {}
    for path, scope in settings_file_candidates(cwd):
        if not path.is_file():
            continue
        try:
            out[scope] = QiSettings.model_validate(read_json(path))
        except Exception as exc:  # pydantic.ValidationError
            raise SettingsError(f"{path} 校验失败: {exc}") from exc
    return out


def save_settings(scope: str, data: dict[str, Any], cwd: Path | None = None) -> Path:
    """整份写入某作用域的 settings.json(保留未知字段)。"""
    path = settings_scope_path(scope, cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# ── 点号键读写(qi config --set / --unset) ──────────────

def parse_value(text: str) -> Any:
    """`--set` 的值:先按 JSON 解析,失败则当字符串(便于 theme=light)。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def set_value(scope: str, dotted: str, value: Any, cwd: Path | None = None) -> Path:
    """写某个点号路径(如 skills / compaction.enabled);中间层不存在则建。"""
    path = settings_scope_path(scope, cwd)
    data = read_json(path) if path.is_file() else {}
    keys = [k for k in dotted.split(".") if k]
    if not keys:
        raise SettingsError("键不能为空")
    cursor = data
    for key in keys[:-1]:
        nxt = cursor.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[key] = nxt
        cursor = nxt
    cursor[keys[-1]] = value
    return save_settings(scope, data, cwd)


def unset_value(scope: str, dotted: str, cwd: Path | None = None) -> tuple[Path, bool]:
    """删除某个点号路径;返回 (文件, 是否真的删掉了键)。"""
    path = settings_scope_path(scope, cwd)
    data = read_json(path) if path.is_file() else {}
    keys = [k for k in dotted.split(".") if k]
    cursor: Any = data
    for key in keys[:-1]:
        cursor = cursor.get(key) if isinstance(cursor, dict) else None
        if not isinstance(cursor, dict):
            return path, False
    removed = False
    if isinstance(cursor, dict) and keys and keys[-1] in cursor:
        del cursor[keys[-1]]
        removed = True
    if removed:
        save_settings(scope, data, cwd)
    return path, removed


# ── 资源路径解析 ────────────────────────────────────────

def split_resource_values(values: list[str]) -> tuple[list[str], list[str]]:
    """把资源数组拆成 (纳入, 排除);`!pat`/`-pat` 为排除,`+path` 为强制纳入。"""
    includes: list[str] = []
    excludes: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        if value.startswith(("!", "-")):
            excludes.append(value[1:])
        elif value.startswith("+"):
            includes.append(value[1:])
        else:
            includes.append(value)
    return includes, excludes


def resolve_resource_paths(value: str, base_dir: Path) -> list[Path]:
    """把一条资源路径解析成实际目录/文件;支持 `~`、相对路径与 glob。"""
    expanded = value.strip().replace("\\", "/")
    if expanded.startswith("~"):
        root = Path(expanded).expanduser()
    else:
        root = Path(expanded)
        if not root.is_absolute():
            root = base_dir / root
    if any(ch in str(root) for ch in "*?["):
        try:
            return sorted(p for p in base_dir.glob(expanded) if p.exists())
        except (OSError, ValueError):
            return []
    return [root] if root.exists() else []


def settings_include_paths(settings: QiSettings | None, scope: str, key: str,
                           cwd: Path | None = None) -> list[Path]:
    """某作用域某资源字段(skills/extensions/...)的**纳入**路径(已展开 glob)。

    `settings` 必须是 `load_settings_by_scope()[scope]` —— 即该作用域**自己**的
    那份,不能传合并后的结果(否则相对路径基准会错)。
    """
    if settings is None:
        return []
    includes, _excludes = split_resource_values(list(getattr(settings, key, None) or []))
    base = settings_scope_path(scope, cwd).parent
    paths: list[Path] = []
    for item in includes:
        for path in resolve_resource_paths(item, base):
            if path not in paths:
                paths.append(path)
    return paths


def settings_exclude_paths(settings: QiSettings | None, scope: str, key: str,
                           cwd: Path | None = None) -> list[Path]:
    """某作用域某资源字段的**排除**路径(`!pat` / `-pat`),同样各自基准解析。

    对齐 pi:排除项作用于**整个发现集**,而不只是数组里纳入的根 —— 所以默认目录
    (`~/.qi/agent/skills` 等)里扫出来的技能也能被单独关掉。
    """
    if settings is None:
        return []
    _includes, excludes = split_resource_values(list(getattr(settings, key, None) or []))
    base = settings_scope_path(scope, cwd).parent
    removed: list[Path] = []
    for item in excludes:
        for path in resolve_resource_paths(item, base):
            if path not in removed:
                removed.append(path)
    return removed


#: `/settings` 面板能改的键 —— **只收值域有限且已接线**的偏好键。
#: 数字/路径类(`editorPaddingX` / `outputPad` / `autocompleteMaxVisible` /
#: `shellPath` / `sessionDir`)不放面板:在 TUI 里敲数字和路径的体验比 `qi config --set` 差。
#: 字段未设时面板显示什么 —— 显示**生效值**而不是 "None"(那是内部表示,不是用户看到的语义)
#: `defaultThinkingLevel` 的生效值同 `llm.DEFAULT_THINKING_LEVEL`(pi 默认 medium);
#: 这里不 import llm —— 两模块有静态环(settings 里就写了延迟导入的原因),有测试锁住两边一致。
UNSET_DISPLAY: dict[str, str] = {"theme": "auto", "defaultThinkingLevel": "medium"}

SETTING_CHOICES: dict[str, tuple[str, ...]] = {
    "theme": ("dark", "light", "auto"),
    "defaultThinkingLevel": ("off", "minimal", "low", "medium", "high", "xhigh", "max"),
    "doubleEscapeAction": ("tree", "fork", "none"),
    "hideThinkingBlock": ("false", "true"),
    "quietStartup": ("false", "true"),
    "skillsEnabled": ("false", "true"),
}


def setting_choices(settings: QiSettings) -> list[tuple[str, str]]:
    """`/settings` 面板的行:`(键, 当前值)` —— 值域来自 `SETTING_CHOICES`。

    当前值不在候选里(比如手写了一个不认识的主题)时**原样显示** —— 面板不该假装它不存在,
    也不该在用户没动它的时候把值改掉。
    """
    rows: list[tuple[str, str]] = []
    for key in SETTING_CHOICES:
        current = getattr(settings, key, None)
        if current is None and key in UNSET_DISPLAY:
            current = UNSET_DISPLAY[key]        # 未设 → 显示**生效值**,不显示 "None"
        rows.append((key, str(current).lower() if isinstance(current, bool) else str(current)))
    return rows


def next_choice(key: str, current: str) -> str:
    """循环到下一个候选值(面板里按一下走一格)。未知键/未知值都**原样返回**。"""
    values = SETTING_CHOICES.get(key)
    if not values:
        return current
    try:
        index = list(values).index(current)
    except ValueError:
        return values[0]          # 手写的怪值:按一下回到第一个合法值
    return values[(index + 1) % len(values)]


#: TUI 模式取值(pi `tuiMode` 的两个值,语义同 pi)。
TUI_MODES: tuple[str, ...] = ("regular", "fullscreen")
#: **qi 的默认**是 fullscreen(pi 默认 regular)。为什么不同:pi 的 regular 把滚动权交给终端,
#: 滚轮/PageUp 会翻到启动 qi 之前的 shell 输出;qi 要的是“滚动只在本界面内”(见 design/internals.md 的终端界面一节)。
DEFAULT_TUI_MODE = "fullscreen"


def tui_mode(settings: QiSettings | None) -> str:
    """`settings.tuiMode` 归一:认 `regular` / `fullscreen`;没写或写歪 → `DEFAULT_TUI_MODE`。

    与 `normalize_thinking_level` 同理:写歪的值不报错也不生效,回落默认 —— 一个错字
    不该让界面起不来。
    """
    raw = str(getattr(settings, "tuiMode", None) or "").strip().lower()
    return raw if raw in TUI_MODES else DEFAULT_TUI_MODE


def branch_summary_skip_prompt(settings: QiSettings) -> bool:
    """`branchSummary.skipPrompt`(pi 同名段,默认 `false`)。

    pi 的定义是 "Skip \"Summarize branch?\" prompt on `/tree` navigation (**defaults to no
    summary**)" —— 所以它为真时**不问也不摘要**,不是"自动摘要"。写歪的值一律当 `false`
    (即"照常问"),不因为一个错字把摘要静默关掉。
    """
    section = getattr(settings, "branchSummary", None)
    if not isinstance(section, dict):
        return False
    # 严格布尔:写歪的值(`"yes"` / `1`)一律当 false —— 不因为一个错字把摘要静默关掉。
    # (同 `qi_web/app.py` 里 `disabled` 的写法:避开 identity comparison 的静态检查。)
    value = section.get("skipPrompt")
    return isinstance(value, bool) and value


def model_thinking_level(settings: QiSettings, model: Any) -> str | None:
    """`modelThinkingLevels` 里该模型的档(pi 同名段:`{"provider/模型": "high"}`)。

    键写 **`provider/模型` 最明确**,也认**裸模型 id**(只写 id 时任何 provider 下同名模型都命中);
    **更具体的那条赢**(先找带 provider 的)。值不认识(不在 `THINKING_LEVELS` 里)时返回 `None`
    并按"没配"处理 —— 一个写歪的值不该让启动失败。
    """
    from .llm import THINKING_LEVELS          # 延迟导入:避免 settings ↔ llm 的静态环

    table = dict(getattr(settings, "modelThinkingLevels", None) or {})
    mid = str(getattr(model, "model", "") or "")
    if not table or not mid:
        return None
    provider = str(getattr(model, "provider", "") or "")
    for key in (f"{provider}/{mid}", mid):
        raw = table.get(key)
        if raw is None:
            continue
        level = str(raw).strip().lower()
        if level in THINKING_LEVELS:
            return level
    return None


def default_tools(settings: QiSettings) -> list[str] | None:
    """`defaultTools` 规范化(None = 用框架默认)。"""
    if settings.defaultTools is None:
        return None
    return [str(t) for t in settings.defaultTools]


def project_trust_default(settings: QiSettings) -> str:
    """`defaultProjectTrust` 取值归一(ask/always/never);未知值按 ask。"""
    value = (settings.defaultProjectTrust or "ask").strip().lower()
    return value if value in ("ask", "always", "never") else "ask"


def project_declared_default_trust(cwd: Path | None = None) -> str | None:
    """项目级 `settings.json` **原始文件**里显式写的 `defaultProjectTrust`(没写 → None)。

    为什么不能直接读解析后的 `QiSettings`:那个字段默认值就是 `"ask"`,`getattr` 分不出
    “没写”与“写了 ask” —— 会把**任何**有 `.qi/settings.json` 的仓库都误报成“仓库自称可信”。
    """
    for path, scope in settings_file_candidates(cwd):
        if scope != "project":
            continue
        if not path.is_file():
            return None
        value = read_json(path).get("defaultProjectTrust")
        return str(value).strip() if value is not None else None
    return None


def resolve_project_trust(settings: QiSettings | None, *,
                          approve: bool | None = None,
                          has_ui: bool = False,
                          stored: tuple[bool, str] | None = None) -> tuple[bool, str]:
    """项目信任判定 → `(trusted, reason)`(docs/extensions.md §2 / design/extensions-design.md 的 E16)。

    优先级(对齐 pi 的 "saved decisions ... apply before the global default"):

    1. CLI 显式(`-a` / `-na`);
    2. `trust.json` 里**当前目录或父目录上最近的**决定(`stored`,由 `trust.TrustStore` 查);
    3. `settings.defaultProjectTrust`(**只从用户级读** —— 项目级那份不能自称可信)。

    `ask` 表示"问用户",而 qi 的交互询问尚未实现,所以那样时**保守判不信任** ——
    扩展是仓库控制的任意代码,fail-safe 只能是"不执行"。
    """
    if approve is not None:                 # 三态:None = 没在 CLI 上表态
        return approve, ("CLI -a" if approve else "CLI -na")
    if stored is not None:
        trusted, where = stored
        kind = "已信任" if trusted else "已拒绝"
        return trusted, f"trust.json 记住了这个{kind}({where})"
    if settings is None:
        return False, "无 settings"
    default = project_trust_default(settings)
    if default == "always":
        return True, "settings.defaultProjectTrust=always"
    if default == "never":
        return False, "settings.defaultProjectTrust=never"
    where = "有 UI 但询问未实现" if has_ui else "无 UI"
    return False, f"defaultProjectTrust=ask({where};用 -a 信任)"


def extension_dirs(cwd: Path | None = None, *, trusted: bool,
                   extra: list[Path] | None = None) -> list[tuple[Path, str]]:
    """`settings.extensions[]` 解析出的附加扩展目录 → `[(路径, 作用域)]`。

    作用域(`user` / `project` / `temporary`)会进工具的 `source_info.scope`,
    所以必须按**各自 settings.json 所在的作用域**标注 —— 一律记 temporary 会让
    `getAllTools()` 的诊断面撒谎(那是它存在的唯一理由)。

    项目级那份**只在信任时**返回 —— 它随仓库走,未信任时它不是用户的意图。
    `extra` 是调用方自己的追加路径(如 CLI 的 `-e`),记 temporary。

    **排除项**(`!pat` / `-path`)在这里就减掉 —— `-e` 显式给的 `extra` 不减
    (显式意图胜过配置),与 `registry._iter_extension_loaders` 同一口径。
    """
    scopes = load_settings_by_scope(cwd)
    out: list[tuple[Path, str]] = [(Path(p), "temporary") for p in (extra or ())]
    for scope in ("user", "project"):
        if scope == "project" and not trusted:
            continue
        settings = scopes.get(scope)
        excluded = {p for p in settings_exclude_paths(settings, scope, "extensions", cwd)}
        out += [(p, scope)
                for p in settings_include_paths(settings, scope, "extensions", cwd)
                if p not in excluded]
    return out


DOUBLE_ESCAPE_ACTIONS = ("tree", "fork", "none")
"""`doubleEscapeAction` 的合法取值(对齐 pi 的 settings-manager)。"""


def double_escape_action(settings: QiSettings | None) -> str:
    """空编辑器连按两次 escape 干什么:tree / fork / none。默认 tree(pi 同款)。"""
    value = (getattr(settings, "doubleEscapeAction", None) or "tree").strip().lower()
    return value if value in DOUBLE_ESCAPE_ACTIONS else "tree"


def session_dir(settings: QiSettings, cwd: Path | None = None,
                scopes: dict[str, QiSettings] | None = None) -> Path | None:
    """`sessionDir` 解析;项目优先于全局,各按自己 settings.json 所在目录解析。"""
    scopes = scopes if scopes is not None else load_settings_by_scope(cwd)
    for scope in ("project", "user"):
        raw = (getattr(scopes.get(scope), "sessionDir", None) or "").strip()
        if not raw:
            continue
        base = settings_scope_path(scope, cwd).parent
        resolved = resolve_resource_paths(raw, base)
        if resolved:
            return resolved[0]
        return (Path(raw).expanduser() if Path(raw).is_absolute()
                else base / raw).resolve()
    raw = (settings.sessionDir or "").strip()
    if not raw:
        return None
    base = settings_scope_path("user", cwd).parent
    resolved = resolve_resource_paths(raw, base)
    return resolved[0] if resolved else Path(raw).expanduser()
