"""packages 声明层:`settings.packages` ↔ 实际已装扩展的**比对**(只读)。

为什么要单独一个模块:声明层解决的是 uv tool / pipx 那个坑 —— 它们会把 qi 自己的
环境**整个重建**,把 pip 装进去的扩展一并抹掉。所以「这个环境该装哪些扩展」必须有一份
落在 settings.json 里的声明,重建之后才有补装依据。

**本模块只回答「差在哪」,不调 pip、不写任何文件、不起子进程**(决策 C):
安装动作交给用户,由 `install_hints()` 生成可复制的命令。这样没有只读解释器、
uv tool、pipx 那些环境分支要处理,也没有半途失败的中间态。

洞见来自与 `_extension_report()` 同一条原则:信息取自**注册面的反查**(entry point /
目录扫描),不是让扩展自报 —— 否则「声明里说装了」与「宿主真能加载」会各说各话。

三条通道都要看,只看 entry point 会把目录通道的扩展误报成「声明未安装」:

* pip 通道 —— entry point 组 `qi.extensions`(dist 名 + 版本)
* 目录通道 —— `<全局|项目>/extensions/<名>/extension.py`
* 附加路径 —— `settings.extensions[]` 解析出的目录(由 `settings.extension_dirs()` 负责作用域标注)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import paths
from .registry import EXTENSION_ENTRY_FILE, EXTENSION_ENTRY_POINT_GROUP, HOST_DISTRIBUTION

#: 声明里区分通道的前缀(`pip:qi-mcp` / `local:/path/to/ext`)。
_PIP_PREFIX = "pip:"
_LOCAL_PREFIX = "local:"

#: pip 能直接装的**本地归档**。它们是** pip 通道**,不是目录通道 ——
#: 目录通道会去找 `<归档>/extension.py`(归档是个文件,必然失败)。
_ARCHIVE_SUFFIXES = (".tar.gz", ".tar.bz2", ".tgz", ".whl", ".zip")


def _path_of(spec: str) -> str:
    """`file:` 前缀去掉的本地路径(仅用于取名字/判后缀)。"""
    return spec[len("file:"):] if spec.startswith("file:") else spec


def _looks_like_archive(spec: str) -> bool:
    """路径是不是一个 pip 能装的归档(wheel / sdist)。"""
    return _path_of(spec).lower().endswith(_ARCHIVE_SUFFIXES)


def _archive_name(spec: str) -> str:
    """本地归档 → 归一前的包名(从文件名取)。

    `qi_mcp-0.1.1.tar.gz` 与 `qi_mcp-0.1.1-py3-none-any.whl` 都取第一个 `-` 前那段
    (PEP 625 的归一名字不含 `-`,只用 `.`/`_`)。取不到就退回文件名本身。
    """
    base = Path(_path_of(spec)).name
    for suffix in _ARCHIVE_SUFFIXES:
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base.split("-")[0] or base


def normalize_name(name: str) -> str:
    """PEP 503 归一:大小写与 `-_.` 视为等价。

    与 `registry._normalize_dist_name` 同一规则(那边判「有没有钉宿主版本」,这边判
    「声明与已装是不是同一个东西」)。两处必须一致,否则 `Qi.MCP` 会在一边算命中、
    在另一边算不命中。
    """
    return re.sub(r"[-_.]+", "-", name or "").strip().lower()


@dataclass(frozen=True)
class DeclaredPackage:
    """`settings.packages` 里的一条声明。"""

    spec: str                     # 原样保留,出错时能原样回显给用户
    name: str                     # 归一后的名字,用于比对
    channel: str                  # pip | local
    source: str                   # settings:user / settings:project …
    requirement: str | None = None  # pip 通道的原始 requirement(含版本约束),local 为 None
    #: 对象形态里的 `extensions` 键(pi 的 "object form filters which resources to load",
    #: 照 `docs/settings.md` 的 packages 一节)。`[]` = 这个包**不贡献扩展**;
    #: 键缺失(`None`)= 不过滤(全部加载)。`qi config` 的"关掉"就是写 `[]`。
    extensions: list[str] | None = None

    @property
    def contributes_extensions(self) -> bool:
        """这个包该不该贡献扩展。对象形态里显式写了空数组 = 不贡献。"""
        return self.extensions is None or bool(self.extensions)


@dataclass(frozen=True)
class InstalledExtension:
    """实际**能被宿主加载**的扩展。"""

    name: str
    channel: str                  # pip | local
    origin: str                   # dist 名(带版本)或 extension.py 路径
    scope: str                    # user / project / temporary
    version: str | None = None


@dataclass
class PackageReport:
    """比对结果。`missing` / `undeclared` 是两个方向,不能合成一个数。"""

    installed: list[InstalledExtension] = field(default_factory=list)
    declared: list[DeclaredPackage] = field(default_factory=list)
    missing: list[DeclaredPackage] = field(default_factory=list)       # 声明了但装不上
    undeclared: list[InstalledExtension] = field(default_factory=list)  # 装了但没声明
    unparsed: list[str] = field(default_factory=list)                   # 认不出的声明(原样回显)

    @property
    def consistent(self) -> bool:
        return not self.missing and not self.undeclared and not self.unparsed


def _extension_filter(value: Any) -> list[str] | None:
    """对象形态里的 `extensions` 键 → `list[str] | None`。

    `None`(键缺失)= **不过滤**,全部加载 —— pi 的语义("object form filters which
    resources to load",没提的种类不过滤);`[]` = 这个包不贡献扩展。
    值写歪了(不是列表/字符串)也按“不过滤”处理 —— 不因为一个键写错就把包判死。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return None


def parse_declaration(entry: Any, source: str) -> DeclaredPackage | None:
    """一条声明 → `DeclaredPackage`。**认不出就返回 None**(由调用方原样上报,不静默丢)。

    容忍两种形状:字符串 `"pip:qi-mcp"`,以及 dict `{"name": …, "source": …}`
    (`settings.packages` 声明为 `list[Any]`,历史上允许写结构体)。
    """
    if isinstance(entry, dict):
        raw = entry.get("source") or entry.get("spec") or entry.get("name")
    else:
        raw = entry
    # 对象形态的其它键(pi:只挑要加载的资源种类)。目前只认 `extensions` —— qi 的
    # pip 包也只贡献这一类资源;将来包带技能/主题时在这里扩键即可。
    options = entry if isinstance(entry, dict) else None
    declared_extensions = options.get("extensions") if options else None
    spec = str(raw or "").strip()
    if not spec:
        return None
    original = spec      # `spec` 的契约是**原样保留**(报错时回显用户到底写了什么)

    if spec.startswith(_LOCAL_PREFIX):
        target = spec[len(_LOCAL_PREFIX):].strip()
        name = Path(target).name or target
        return DeclaredPackage(spec=spec, name=normalize_name(name),
                               channel="local", source=source,
                               extensions=_extension_filter(declared_extensions))
    if spec.startswith(_PIP_PREFIX):
        spec = spec[len(_PIP_PREFIX):].strip()
    # 裸路径也算目录通道:`/abs/path`、`./rel`、`~/x` —— 但**归档文件走 pip 通道**:
    # `qi install ./qi_mcp-0.1.1.tar.gz` 是“装这个包”,不是“找 ./...tar.gz/extension.py”。
    if spec.startswith(("/", "./", "../", "~")) or spec.startswith("file:"):
        if _looks_like_archive(spec):
            return DeclaredPackage(spec=original, name=normalize_name(_archive_name(spec)),
                                   channel="pip", source=source, requirement=spec)
        name = Path(spec.removeprefix("file:")).name or spec
        return DeclaredPackage(spec=spec, name=normalize_name(name),
                               channel="local", source=source)

    # PEP 508 的 `名字 @ URL` 形式:名字是显式的,取它
    explicit = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*@\s*\S+", spec)
    if explicit:
        return DeclaredPackage(spec=original, name=normalize_name(explicit.group(1)),
                               channel="pip", source=source, requirement=spec)
    # 裸 URL / VCS 地址没有可提取的名字 —— 不猜(否则 “git+https://host/qi-mcp” 会被
    # 当成一个叫 "git" 的包,比认不出更坏)。交给调用方回显,让用户写成 `名字 @ URL`。
    if "://" in spec:
        return None

    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    if not match:
        return None
    return DeclaredPackage(spec=original, name=normalize_name(match.group(1)),
                           channel="pip", source=source, requirement=spec,
                           extensions=_extension_filter(declared_extensions))


def discover_declared(cwd: Path | None = None) -> tuple[list[DeclaredPackage], list[str]]:
    """两个作用域的 `settings.packages` 都收;同名以**项目级**为准(近者胜)。

    → `(可解析的声明, 认不出的原样 spec)`。第二个返回值不能少:静默丢掉一条声明
    等于“声明了但看起来没声明”,比报错难诊断得多。

    按作用域各自解析(不是合并后的数组),理由同 `settings.extension_dirs()`:
    合并会让一侧的声明落到另一侧的基准上。
    """
    from .settings import load_settings_by_scope

    scopes = load_settings_by_scope(cwd)      # 坏设置就直接抛:调用方必须报出来

    out: dict[str, DeclaredPackage] = {}
    unparsed: list[str] = []
    for scope in ("user", "project"):        # 先远后近,project 覆盖 user
        settings = scopes.get(scope)
        if settings is None:
            continue
        for entry in getattr(settings, "packages", None) or []:
            decl = parse_declaration(entry, f"settings:{scope}")
            if decl is None:
                unparsed.append(str(entry))
            else:
                out[decl.name] = decl
    return list(out.values()), unparsed


def _dir_extensions(root: Path, scope: str) -> list[InstalledExtension]:
    """目录通道:`<root>/<名>/extension.py`。"""
    if not root.is_dir():
        return []
    found = []
    for child in sorted(root.iterdir()):
        entry = child / EXTENSION_ENTRY_FILE
        if child.is_dir() and entry.is_file():
            found.append(InstalledExtension(name=normalize_name(child.name),
                                            channel="local", origin=str(entry),
                                            scope=scope))
    return found


def discover_installed(cwd: Path | None = None, *, project_trusted: bool = False,
                       extra_dirs: Iterable[Path | tuple[Path, str]] | None = None
                       ) -> list[InstalledExtension]:
    """实际可加载的扩展:entry point + 目录通道 + 附加路径。

    `project_trusted=False`(doctor 的默认)时**不扫项目目录** —— 与装载路径
    (`registry._iter_extension_loaders`)保持一致:声明层报告不该越过信任门控,
    否则未信任的项目会因为一份 `.qi/extensions/` 而显得"已装"。
    """
    import importlib.metadata as metadata

    out: list[InstalledExtension] = []
    for ep in metadata.entry_points(group=EXTENSION_ENTRY_POINT_GROUP):
        dist = getattr(ep, "dist", None)
        dist_name = getattr(dist, "name", None) or ep.value.split(":")[0].split(".")[0]
        version = getattr(dist, "version", None)
        out.append(InstalledExtension(
            name=normalize_name(dist_name), channel="pip",
            origin=f"{dist_name} {version}" if version else str(dist_name),
            scope="user", version=version))

    if project_trusted:
        out += _dir_extensions(paths.project_home(cwd) / paths.EXTENSIONS_DIR_NAME, "project")
    out += _dir_extensions(paths.global_home() / paths.EXTENSIONS_DIR_NAME, "user")

    # 附加路径:可能是父目录,也可能本身就是一个扩展目录
    for path, scope in _normalize_extra(extra_dirs):
        if (path / EXTENSION_ENTRY_FILE).is_file():
            out.append(InstalledExtension(name=normalize_name(path.name), channel="local",
                                          origin=str(path / EXTENSION_ENTRY_FILE),
                                          scope=scope))
        else:
            out += _dir_extensions(path, scope)

    seen: set[str] = set()
    unique: list[InstalledExtension] = []
    for item in out:                      # 同名去重(entry point 先到先得)
        if item.name not in seen:
            seen.add(item.name)
            unique.append(item)
    return unique


def _normalize_extra(extra_dirs: Iterable[Path | tuple[Path, str]] | None
                     ) -> list[tuple[Path, str]]:
    """与 `registry._normalize_extra` 同义:裸 Path 记 temporary。"""
    out: list[tuple[Path, str]] = []
    for item in extra_dirs or ():
        if isinstance(item, tuple):
            out.append((Path(item[0]), str(item[1])))
        else:
            out.append((Path(item), "temporary"))
    return out


def build_report(installed: Iterable[InstalledExtension],
                 declared: Iterable[DeclaredPackage],
                 unparsed: Iterable[str] = ()) -> PackageReport:
    """纯比对(不碰文件系统 / 元数据),所以测试不必造环境。"""
    installed_list = list(installed)
    declared_list = list(declared)
    have = {item.name for item in installed_list}
    want = {item.name for item in declared_list}
    return PackageReport(
        installed=installed_list,
        declared=declared_list,
        missing=[item for item in declared_list if item.name not in have],
        undeclared=[item for item in installed_list if item.name not in want],
        unparsed=list(unparsed),
    )


def package_report(cwd: Path | None = None, *, project_trusted: bool = False
                   ) -> PackageReport:
    """发现 + 比对。doctor / `qi list` 的唯一入口。

    **可能抛 `settings.SettingsError`**(settings.json 校验失败)—— 不在这里吞:
    吞掉的话“设置写坏了”会表现成“一条声明也没有”,而这两种情况的处置完全不同。
    """
    from .settings import extension_dirs, load_settings_by_scope

    extra: list[Path] = []
    if load_settings_by_scope(cwd):
        extra = [p for p, _ in extension_dirs(cwd, trusted=project_trusted)]
    installed = discover_installed(cwd, project_trusted=project_trusted, extra_dirs=extra)
    declared, unparsed = discover_declared(cwd)
    return build_report(installed, declared, unparsed)


@dataclass(frozen=True)
class Resource:
    """一个**可启停的资源项**:目录扩展 或 声明的 pip 包。

    "关"的表示法两种(都照 pi,**都不是新字段**),所以状态是**记住的** ——
    面板能区分"没配过"与"主动关了",这也是 `pi config -l` 能把继承项置灰的前提:

    * 目录扩展 → `settings.extensions[]` 里加一条否定项 `-<路径>`;
    * pip 包   → `settings.packages` 里那一条换成对象形态 `{"source": X, "extensions": []}`。
    """

    kind: str        # "extension" | "package"
    name: str
    detail: str      # 路径 / 原样声明
    scope: str       # user | project
    enabled: bool
    spec: str = ""   # package 用:"开"时写回的原样声明


def list_resources(cwd: Path | None = None, *, trusted: bool = True) -> list[Resource]:
    """可启停的资源清单(目录扩展 + 声明的 pip 包),带当前启用状态。

    面得比 pi 窄一点:`local:` 声明目前只是**声明**(loader 不从 `packages` 读目录通道),
    所以它列不出来 —— 目录通道的资源请写进 `settings.extensions[]`(那才是它生效的地方)。
    不知道"关"状态的(比如 `settings.packages` 里的结构体写法)按**开**算。
    """
    from . import paths
    from .settings import (load_settings_by_scope, settings_exclude_paths,
                           settings_include_paths)

    scopes = load_settings_by_scope(cwd)
    out: list[Resource] = []
    for scope in ("user", "project"):
        if scope == "project" and not trusted:
            continue
        settings = scopes.get(scope)
        excluded = {_abs(p) for p in settings_exclude_paths(settings, scope, "extensions", cwd)}
        seen: set[Path] = set()
        home = paths.project_home(cwd) if scope == "project" else paths.global_home()
        candidates: list[Path] = []
        builtin = home / paths.EXTENSIONS_DIR_NAME
        if builtin.is_dir():
            candidates += sorted(builtin.iterdir())
        for entry in settings_include_paths(settings, scope, "extensions", cwd):
            candidates += [entry] if (entry / EXTENSION_ENTRY_FILE).is_file() else (
                sorted(entry.iterdir()) if entry.is_dir() else [])
        for child in candidates:
            if not (child.is_dir() and (child / EXTENSION_ENTRY_FILE).is_file()):
                continue
            key = _abs(child)
            if key in seen:
                continue
            seen.add(key)
            out.append(Resource(kind="extension", name=child.name, detail=str(child),
                                scope=scope, enabled=key not in excluded))

    declared, _unparsed = discover_declared(cwd)
    for decl in declared:
        if decl.channel != "pip":
            continue          # 目录通道的见上(它们该写在 settings.extensions)
        out.append(Resource(kind="package", name=decl.name, detail=decl.spec,
                            scope=decl.source.split(":")[-1],
                            enabled=decl.contributes_extensions, spec=decl.spec))
    return out


def set_resource_enabled(resource: Resource, enabled: bool, cwd: Path | None = None) -> str:
    """把一个资源开关写成声明;返回一句人话(给 CLI 打出来)。

    只动该资源**所在作用域**的 settings —— 这正是"项目关掉、全局还开着"能表达的原因。
    """
    from .settings import load_settings_by_scope, set_value

    scope = resource.scope
    settings = load_settings_by_scope(cwd).get(scope)
    if resource.kind == "extension":
        values = [str(item) for item in (getattr(settings, "extensions", None) or [])]
        negated = f"-{resource.detail}"
        if enabled:
            values = [item for item in values if item != negated]
        elif negated not in values:
            values.append(negated)
        set_value(scope, "extensions", values, cwd)
        return (f"{'启用' if enabled else '关闭'}扩展 {resource.name}"
                f"({scope} settings.extensions {'去掉' if enabled else '加了'}否定项)")

    raw = list(getattr(settings, "packages", None) or [])
    rewritten: list[Any] = []
    for item in raw:
        decl = parse_declaration(item, f"settings:{scope}")
        if decl is not None and decl.name == resource.name:
            rewritten.append(resource.spec if enabled
                             else {"source": resource.spec, "extensions": []})
        else:
            rewritten.append(item)
    set_value(scope, "packages", rewritten, cwd)
    return (f"{'启用' if enabled else '关闭'}包 {resource.name}"
            f"({scope} settings.packages 写成{'一行' if enabled else '对象形态 extensions: []'})")


def apply_resource_selection(resources: list[Resource], enabled: set[int],
                            cwd: Path | None = None) -> list[str]:
    """面板 / 脚本交回“哪些保持启用”(下标)后,把**改动**写成声明。返回人话列表。

    纯函数式的分工:UI 只负责显示与收回勾选,写声明在这里 —— 于是
    “面板怎么用”与“写了什么”可以分别测,而且取消时不会留下副作用(调用方根本不调它)。
    没变的项不动(不重写 settings,也不在输出里噬噬)。
    """
    notes: list[str] = []
    for index, resource in enumerate(resources):
        wanted = index in enabled
        if wanted == resource.enabled:
            continue
        notes.append(set_resource_enabled(resource, wanted, cwd))
    return notes


def _abs(path: Path) -> Path:
    """比较用的绝对路径(解析不了就退原样 —— 不因此把排除项当成没写)。"""
    try:
        return path.resolve()
    except OSError:
        return path


def install_hints(spec: str) -> list[str]:
    """一条声明的**可复制**装法。

    首选 `qi install`:它把包装进 qi 自己的解释器环境(uv tool 环境里自动改用 uv 的
    安装器),并写回 `settings.packages`。第二条是 uv 的托管依赖 —— 环境重建/升级也不丢。
    不选边:用户才知道自己是怎么装的。
    """
    if spec.startswith(_LOCAL_PREFIX):
        spec = spec[len(_LOCAL_PREFIX):].strip()
    if spec.startswith(_PIP_PREFIX):
        spec = spec[len(_PIP_PREFIX):].strip()
    return [
        f'qi install "{spec}"',
        f'uv tool install {HOST_DISTRIBUTION} --with "{spec}"',
    ]
