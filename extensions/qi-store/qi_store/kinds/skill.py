"""Skill:上游 zip(顶层目录 = 技能名,内含 `SKILL.md`)。

两件必须做的事,都是"不做就等于装了个看不见的东西":

1. **必须有 `description`** —— qi 的规则是"没有 description 的技能等于不存在"
   (loader 读不到就不进 `top_skills`,提示词里也没有)。所以宁可在安装期失败。
2. **必须是"单顶层目录 + 根级 SKILL.md"的形状** —— qi 只认 `<技能目录>/SKILL.md`。
   如果包是 `skills/<名>/SKILL.md` 混着别的顶层文件,我们**装不出一个能用的技能**,
   那就明确报错,而不是解压出一个坏目录。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from ..actions import ExtractZip, Planned
from ..catalog import StoreItem, content_sig
from ..client import StoreClient
from ..errors import NotFound, UsageError
from .base import (
    MAX_ZIP_BYTES,
    MAX_ZIP_MEMBERS,
    MAX_LOCATE_DEPTH,
    SKILL_FILE,
    PlanContext,
    check_name,
    read_frontmatter,
)

KIND = "skill"
PAGE_SIZE = 100
MAX_PAGES = 50


def fetch(client: StoreClient) -> list[StoreItem]:
    items: list[StoreItem] = []
    page = 1
    while page <= MAX_PAGES:
        data = client.get("/skills", {
            "page": page, "page_size": PAGE_SIZE,
            "status": "parsed", "is_available": True,      # 商店口径:解析完成且未下架
            "sort_by": "upload_time", "sort_order": "desc",
        })
        if not isinstance(data, dict):
            break
        batch = data.get("items") or []
        items.extend(_item(raw) for raw in batch if isinstance(raw, dict))
        total = int(data.get("total") or 0)
        if len(batch) < PAGE_SIZE or len(items) >= total:
            break
        page += 1
    return items


def _item(raw: dict) -> StoreItem:
    name = str(raw.get("name") or "")
    return StoreItem(
        id=f"skill:{raw.get('id')}",
        kind=KIND,
        name=name,
        description=str(raw.get("description") or ""),
        # 上游没有版本号 → 内容指纹(大小 + 上传时间 + 名字)。改一个字节就该变。
        version=content_sig(name, raw.get("file_size"), raw.get("upload_time")),
        updated_at=str(raw.get("upload_time") or ""),
        requires_config=False,
        safety={"scan_status": raw.get("scan_status"), "reviewed": bool(raw.get("reviewed"))},
        badges={"downloads": raw.get("download_count"), "size": raw.get("file_size"),
                "owner": raw.get("owner_name")},
        meta={"upstream_id": raw.get("id"), "file_size": raw.get("file_size")},
    )


# ── 取包 ────────────────────────────────────────────────


def fetch_zip(item: StoreItem, ctx: PlanContext) -> bytes:
    """上游的下载端点。⚠️ 它会让上游 `download_count++` —— 这是我们对上游唯一的数据影响。"""
    client = ctx.require_client()
    upstream_id = item.meta.get("upstream_id")
    if upstream_id is None:
        raise NotFound(f"{item.id} 缺少上游 id,装不了")
    return client.get_bytes(f"/skills/{upstream_id}/download")


def _members(data: bytes) -> list[str]:
    """校验 zip 的规模,返回文件成员名(目录条目不算)。"""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UsageError(f"下载到的不是合法 zip:{exc}") from exc
    names = [info.filename for info in archive.infolist() if not info.is_dir()]
    if len(names) > MAX_ZIP_MEMBERS:
        raise UsageError(f"包内文件太多({len(names)} > {MAX_ZIP_MEMBERS})")
    total = sum(info.file_size for info in archive.infolist() if not info.is_dir())
    if total > MAX_ZIP_BYTES:
        raise UsageError(f"包解压后太大({total} bytes > {MAX_ZIP_BYTES})")
    return names


def _locate(names: list[str]) -> tuple[bool, str]:
    """找 `SKILL.md`,并判断要不要去掉顶层目录。返回 `(strip_top, 相对路径)`。"""
    candidates = [n for n in names
                  if n.rsplit("/", 1)[-1] == SKILL_FILE and n.count("/") <= MAX_LOCATE_DEPTH]
    if not candidates:
        raise UsageError(f"包里没有 {SKILL_FILE}(找过 {MAX_LOCATE_DEPTH} 层)")
    if len(candidates) > 1:
        raise UsageError("包里有不止一个 SKILL.md:" + "、".join(candidates))
    rel = candidates[0]
    if "/" not in rel:
        return False, rel
    top = rel.split("/", 1)[0]
    if not all(n.split("/", 1)[0] == top for n in names):
        # 解压出来会是 `target/skills/<名>/SKILL.md` —— qi 只认 `target/SKILL.md`,
        # 装下去等于装了个死目录。宁可失败。
        raise UsageError(
            f"包结构不支持:{SKILL_FILE} 在 `{rel}`,但还有文件不在 `{top}/` 下",
            hint="这个技能需要手工整理成「单顶层目录 + 根 SKILL.md」再装",
        )
    return True, rel.split("/", 1)[1]


def inspect(data: bytes, item: StoreItem) -> tuple[dict, list[str], bool]:
    """校验并返回 `(frontmatter, 去顶层后的文件列表, strip_top)`。plan 与 download 共用。"""
    names = _members(data)
    strip_top, rel = _locate(names)
    archive = zipfile.ZipFile(io.BytesIO(data))
    prefix = names[0].split("/", 1)[0] + "/" if strip_top else ""
    try:
        text = archive.read(prefix + rel).decode("utf-8", errors="replace")
    except KeyError as exc:                      # pragma: no cover - 上面刚定位过
        raise UsageError(f"读不到 {rel}:{exc}") from exc
    front = read_frontmatter(text)
    if not str(front.get("description") or "").strip():
        raise UsageError(
            f"技能 `{item.name}` 没有 description —— qi 会看不见它",
            hint="这是上游技能包的问题(没有 description 的技能不进提示词),装了也白装",
        )
    return front, [n[len(prefix):] if prefix and n.startswith(prefix) else n for n in names], \
        strip_top


def plan(item: StoreItem, ctx: PlanContext) -> Planned:
    data = fetch_zip(item, ctx)
    front, names, strip_top = inspect(data, item)
    name = check_name(ctx.name_override or item.name, "技能")
    target = ctx.skills_root() / name
    declared = str(front.get("name") or "").strip()
    note = f"根级 SKILL.md · {len(names)} 个文件"
    if declared and declared != name:
        # 不拦:qi 的显示名取 frontmatter,目录名只是落点。但要说出来,否则"装了两个同名技能"
        # 这种同层冲突会在装载期炸(LoaderError)。
        note += f" · 注意:SKILL.md 里的名字是 `{declared}`"
    return Planned(
        item=item,
        target=target,
        actions=[ExtractZip(target, data, strip_top=strip_top)],
        needs_config=[],
        note=note,
    )


def download(item: StoreItem, ctx: PlanContext, out_dir: Path, *, raw: bool = False) -> list[Path]:
    """`download` 子命令:只取件。`raw=True` 保留上游 zip,**不碰配置目录**。"""
    data = fetch_zip(item, ctx)
    _front, names, strip_top = inspect(data, item)   # 即使只取件也校验:先知道它是不是能用
    name = check_name(ctx.name_override or item.name, "技能")
    out_dir.mkdir(parents=True, exist_ok=True)
    if raw:
        target = out_dir / f"{name}.zip"
        target.write_bytes(data)
        return [target]
    target = out_dir / name
    target.mkdir(parents=True, exist_ok=True)
    archive = zipfile.ZipFile(io.BytesIO(data))
    written: list[Path] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        arc = info.filename
        if strip_top and "/" in arc:
            arc = arc.split("/", 1)[1]
        dest = target / arc
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(archive.read(info))
        written.append(dest)
    return written
