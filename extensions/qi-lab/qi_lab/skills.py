"""技能的写盘操作:添加 / 删除 / 导入 —— **只管 qi 自己的那两层**。

技能是 *一个目录 + 里面的 `SKILL.md`*(少数情况下直接是根级一份 `*.md`),散在六个
技能根里 —— 见 `qi_agent.loader.top_level_skill_dirs`:

    1. `~/.agents/skills`         跨工具共享(别的工具也读它)      ← **不碰**
    2. `~/.qi/agent/skills`       qi 全局                          ← `scope=global`
    3. user settings 的 `skills[]` 用户自己指定的路径               ← 不碰(可能是仓库外任意目录)
    4. `.agents/skills`           项目里的跨工具共享                ← 不碰
    5. `<cwd>/.qi/skills`         qi 项目                          ← `scope=project`
    6. project settings 的 `skills[]`                              ← 不碰

**我建的我才删**:跨工具层是别的工具的家,settings 追加层是用户手指定的路径,动它们
都越界(而且 skills[] 可能 glob 到仓库外)。于是这里只有两个 scope,与 `/api/mcp`
写端点同一套词汇。

三条不变量,每条都有测试钉住:

1. **没有 description 的技能等于不存在** —— 顶层扫描(`_skill_from_file`)读不到
   description 就 `return None`:不进提示词、`/api/skills` 也看不见它。所以"建一个
   没 description 的技能"与"没建"等价 —— 直接 400,而不是写完再让人查它为什么没出现。
2. **写出去的显示名 == 目录名** —— 显示名取 frontmatter 的 `name`,没有才回落目录名。
   删的时候按**显示名**扫(手改过 frontmatter 的技能才删得掉),落刀处是 `SKILL.md`
   的父目录(根级 `*.md` 则是文件本身),并且**必须落在本根之内**。
3. **同层不重名** —— 跨层同名是合法的覆盖关系(高优先级盖低优先级),同层重复却会让
   `load_top_level_skills` 抛 `LoadError` → 宿主 503,等于"建一个技能把整页弄挂"。
   所以同层撞名回 409,不落盘。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Literal

import yaml

from qi_agent import paths
from qi_agent.loader import SKILL_FILE, scan_skill_root, split_frontmatter
from qi_agent.models import Skill
from qi_agent.paths import SKILLS_DIR_NAME

#: 技能名上限(目录名也是它,文件系统对单段长度有 255 字节的限制)
MAX_NAME = 100
#: 导入 zip 的防御性上限:成员数与解压后总字节数(zip bomb 的两条腿)
MAX_ZIP_MEMBERS = 200
MAX_ZIP_BYTES = 8 * 1024 * 1024          # 与 files.RAW_MAX_BYTES 同一档
#: 从 source 里找 SKILL.md 的下钻层数(技能仓库常见的 `skills/<名>/SKILL.md` 布局)
MAX_LOCATE_DEPTH = 3


class SkillError(Exception):
    """越界 / 不存在 / 结构不对。`status` 是调用方该回的 4xx。"""

    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.status = status


# ── 两个可写根 ──────────────────────────────────────────────


def skill_root(scope: str, cwd: Path | str) -> Path:
    """`scope` 那一层的技能根(`~/.qi/agent/skills` 或 `<cwd>/.qi/skills`)。

    **不创建目录**:根存不存在是读端的事,写的时候顺手 mkdir 就够了。
    """
    if scope == "global":
        return paths.global_home() / SKILLS_DIR_NAME
    if scope == "project":
        return paths.project_home(Path(cwd)) / SKILLS_DIR_NAME
    raise SkillError(f"不认识的 scope: {scope}(可选 global / project)")


def check_name(name: str) -> str:
    r"""技能名的规矩。它同时是**目录名**,所以路径安全也在这里一起管住:

    · 不许带 `/`、`\`、`.`、`..` —— 否则 `scope=global` + `name=../../x` 就写到家外面去了;
    · 不许首尾空白(一个手滑的空格会造出两个看起来一模一样的技能);
    · 不许控制字符(会得到一个没人打得出来的名字)。
    """
    if not name or not name.strip():
        raise SkillError("技能名不能为空")
    if name != name.strip():
        raise SkillError("技能名不能带首尾空白(手滑的空格会造出两个“同名”技能)")
    if "/" in name or "\\" in name:
        raise SkillError("技能名不能带路径分隔符(/ 或 \\)")
    if name in (".", "..") or name.startswith("."):
        raise SkillError("技能名不能以点开头")
    if any(ord(ch) < 32 for ch in name):
        raise SkillError("技能名不能带控制字符")
    if len(name) > MAX_NAME:
        raise SkillError(f"技能名太长(最多 {MAX_NAME} 字)")
    return name


def find(root: Path, name: str) -> Path | None:
    """按**显示名**在这个根里找技能文件(`SKILL.md` 或根级 `*.md`);没有 → None。

    按显示名扫而不是拼路径:手改过 frontmatter `name` 的技能,它的目录名与显示名
    可能不是一个东西 —— 界面上写的是显示名,删就得按它删。
    """
    found: list[Skill] = []
    try:
        scan_skill_root(root, label="write", out=found, allow_root_md=True)
    except Exception as exc:              # noqa: BLE001 坏技能不该让写端点 500
        raise SkillError(f"{root}: 扫不动({exc})") from exc
    for skill in found:
        if skill.name == name:
            return skill.path
    return None


def ensure_free(root: Path, name: str) -> None:
    """同层撞名 → 409(见本模块不变量 3)。"""
    if find(root, name) is not None:
        raise SkillError(f"该层已有同名技能: {name}", 409)
    target = root / name
    if target.exists():
        raise SkillError(f"该层已经有这个路径了(但不是技能): {target}", 409)


# ── 添加 ────────────────────────────────────────────────────


def render(name: str, description: str, body: str) -> str:
    """SKILL.md 的内容。frontmatter 走 yaml(`description` 里有冒号/引号很常见)。"""
    front = yaml.safe_dump({"name": name, "description": description.strip()},
                           allow_unicode=True, sort_keys=False).rstrip("\n")
    return f"---\n{front}\n---\n\n{body.strip()}\n"


def create(root: Path, name: str, description: str, body: str) -> Path:
    """新建 `<root>/<name>/SKILL.md`,返回落点。**不创建已存在的目录内容**。"""
    if not description.strip():
        raise SkillError("没有 description 的技能扫描器看不见(顶层技能按 description 索引),"
                         "description 不能空")
    ensure_free(root, name)
    target = root / name
    tmp = root / f".{name}.tmp"
    try:
        root.mkdir(parents=True, exist_ok=True)
        if tmp.exists():                  # 上一次失败留下的残骸,清掉
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        (tmp / SKILL_FILE).write_text(render(name, description, body), encoding="utf-8")
        # 先写临时目录再改名:落一半的技能目录会被扫出来,变成一个"有名字没正文"的技能
        os.replace(tmp, target)
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SkillError(f"{target}: 写不了({exc})") from exc
    return target


# ── 删除 ────────────────────────────────────────────────────


def _within(path: Path, root: Path) -> bool:
    """`path` 是否落在 `root` 之内(含等于)。**先 resolve 再比**:符号链接指出去的
    那种"技能"在这里会露馅 —— 按目录名删会删到根外面去。"""
    real, base = path.resolve(), root.resolve()
    return real == base or real.is_relative_to(base)


def remove(root: Path, name: str) -> Path:
    """按显示名删掉这一层的技能,返回被删的路径。**只删这一层**(见不变量 2)。"""
    path = find(root, name)
    if path is None:
        raise SkillError(f"该层没有这个技能: {name}", 404)
    target = path.parent if path.name == SKILL_FILE else path
    if not _within(target, root) or target.resolve() == root.resolve():
        raise SkillError(f"{target}: 不在 {root} 之内,拒绝删除")
    try:
        if target.is_dir():
            shutil.rmtree(target)          # 整个技能目录(含 references/ 这类附属文件)
        else:
            target.unlink()                # 根级单文件技能
    except OSError as exc:
        raise SkillError(f"{target}: 删不掉({exc})") from exc
    return target


# ── 导入 ────────────────────────────────────────────────────


def import_skill(root: Path, source: str, name: str = "") -> Path:
    """把 `source`(目录 / 单份 .md / .zip)导进 `root`,返回落点。

    三步:**定位**(`source` 里哪一个是技能)→ **验**(有没有 description、重不重名)
    → **落**(整目录复制,字节不动;只有名字对不上时才改 frontmatter 的 name)。
    """
    src = Path(source).expanduser()
    if not src.exists():
        raise SkillError(f"source 不存在: {source}", 404)
    if src.is_file() and src.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory(prefix="qi-lab-skill-") as tmp:
            _unzip(src, Path(tmp))
            kind, base = _locate(Path(tmp))
            # zip 根就是技能目录时,临时目录名没有信息量 → 用 zip 自己的名字当兜底
            hint = _hint(kind, base, Path(tmp), src.stem)
            return _install(root, kind, base, name, hint)
    kind, base = _locate(src)
    hint = _hint(kind, base, None, src.stem)
    return _install(root, kind, base, name, hint)


def _unzip(src: Path, dest: Path) -> None:
    """解压 `src` 到 `dest`。

    两条防御不是可选的:**zip slip**(成员名带 `..` 时 `extractall` 会写到 dest 外面)
    与 **zip bomb**(200 字节的 zip 解出 10 GB)。软链接条目直接丢弃 —— 它指向的
    目标在这里无法核实,复制时会跟着走。
    """
    try:
        with zipfile.ZipFile(src) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_MEMBERS:
                raise SkillError(f"{src}: 成员太多({len(infos)} > {MAX_ZIP_MEMBERS})")
            total = sum(i.file_size for i in infos)
            if total > MAX_ZIP_BYTES:
                raise SkillError(f"{src}: 解压后太大({total} 字节 > {MAX_ZIP_BYTES})")
            for info in infos:
                parts = info.filename.replace("\\", "/").lstrip("/").split("/")
                if info.filename.startswith("/") or ".." in parts or ":" in parts[0]:
                    raise SkillError(f"{src}: 成员名越界: {info.filename}")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    continue              # 软链接
            zf.extractall(dest)
    except zipfile.BadZipFile as exc:
        raise SkillError(f"{src}: 不是一个能解开的 zip({exc})") from exc


def _locate(base: Path) -> tuple[Literal["dir", "file"], Path]:
    """`source` 里**唯一**那个技能在哪。

    认三种形状:技能目录(含 `SKILL.md`)、单份 `.md`、以及包住它们一层的容器
    (技能仓库 / zip 解出来的临时目录)。定位不到唯一一个就报错 —— **不猜**:
    一次导错包比拒绝导入贵得多。
    """
    if base.is_file():
        if base.suffix.lower() != ".md":
            raise SkillError(f"只认目录 / 单份 .md / .zip: {base}")
        return "file", base
    hits = _skill_dirs(base, MAX_LOCATE_DEPTH)
    if len(hits) == 1:
        return "dir", hits[0]
    if len(hits) > 1:
        listed = "、".join(str(p) for p in hits[:5])
        raise SkillError(f"这里面有 {len(hits)} 个技能({listed}),一次只能导一个")
    mds = [p for p in sorted(base.iterdir())
           if p.is_file() and p.suffix.lower() == ".md" and not p.name.startswith(".")]
    if len(mds) == 1:
        return "file", mds[0]
    if len(mds) > 1:
        raise SkillError(f"这里面有 {len(mds)} 份 .md,挑一份再导")
    raise SkillError(f"这里面找不到 {SKILL_FILE}: {base}")


def _skill_dirs(base: Path, depth: int) -> list[Path]:
    """`base` 下(含自身)含 `SKILL.md` 的目录;有 `SKILL.md` 就不再往下钻。"""
    out: list[Path] = []
    if not base.is_dir():
        return out

    def walk(directory: Path, left: int) -> None:
        if (directory / SKILL_FILE).is_file():
            out.append(directory)
            return
        if left <= 0:
            return
        try:
            children = sorted(directory.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_dir() and not child.name.startswith("."):
                walk(child, left - 1)

    walk(base, depth)
    return out


def _hint(kind: Literal["dir", "file"], base: Path, tmp: Path | None,
          source_stem: str) -> str:
    """源里没写 name 时的兜底名。"""
    if kind == "dir":
        if tmp is not None and base == tmp:
            return source_stem             # zip 根就是技能目录 → 用 zip 名
        return base.name
    if base.name == SKILL_FILE:
        parent = base.parent
        return source_stem if tmp is not None and parent == tmp else parent.name
    return base.stem


def _install(root: Path, kind: Literal["dir", "file"], base: Path,
             name: str, hint: str) -> Path:
    """验完拷进 `root`。**字节原样复制**(源里的 `references/` 之类都跟着走),
    只有当 frontmatter 的 `name` 与落点名不一致时才改那一个字段。"""
    skill_file = base if kind == "file" else base / SKILL_FILE
    try:
        meta, _body = split_frontmatter(skill_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillError(f"{skill_file}: 读不了({exc})") from exc
    description = str(meta.get("description") or "").strip()
    if not description:
        raise SkillError(f"{skill_file}: 没有 description —— 顶层技能按 description 索引,"
                         "没有它扫描器会直接跳过(等于没导入)")
    front_name = str(meta.get("name") or "").strip()
    # 不替调用方 strip:首尾空白是**要报的错**(与 create 同一口径),静默修好只会让
    # 「看起来叫 x、实际叫 x 」的技能出现
    target_name = check_name(name if name else (front_name or hint))
    ensure_free(root, target_name)
    target = root / target_name
    try:
        if kind == "dir":
            shutil.copytree(base, target)
        else:
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(base, target / SKILL_FILE)
    except OSError as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise SkillError(f"{target}: 导不进去({exc})") from exc
    # 只在「源里写了 name、且与落点名不一致」时改写 —— 否则目录名 A、显示名 B 的技能
    # 会让"按显示名删"这件事变成靠运气。源里没写 name 时,扫描器回落目录名,天然一致。
    if front_name and front_name != target_name:
        _rename(target / SKILL_FILE, target_name)
    return target


def _rename(path: Path, name: str) -> None:
    """改写 frontmatter 的 `name`。**只动 name**:其它字段(含 description、
    extra 字段)按原顺序重发,注释会丢 —— frontmatter 本来就是机器读的那一块。"""
    try:
        text = path.read_text(encoding="utf-8")
        meta, body = split_frontmatter(text)
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillError(f"{path}: 改名字失败({exc})") from exc
    out: dict = {"name": name}
    out.update({k: v for k, v in meta.items() if k != "name"})
    front = yaml.safe_dump(out, allow_unicode=True, sort_keys=False).rstrip("\n")
    try:
        path.write_text(f"---\n{front}\n---\n\n{body.strip()}\n", encoding="utf-8")
    except OSError as exc:
        raise SkillError(f"{path}: 改名字失败({exc})") from exc
