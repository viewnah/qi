"""落盘:原子写 + 备份 + 记账。失败不留半成品。

三条不变量:

1. **原子**:先写临时路径再 `os.replace` / 整目录 rename。中途失败不会留下半个技能目录。
2. **只动自己的**:`PatchJson` 只写那一个键,文件里别的 server 原样活着;
   `--force` 覆盖前把整个文件(或整个目录)备份成 `*.bak-<时间>`。
3. **记完才算完**:每成功一条就 `ledger.upsert()` 并立刻存盘 —— 装到第三个失败时,
   前两个已经被记着,`uninstall` 找得到它们。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import time
import zipfile
from pathlib import Path

from . import paths
from .actions import ExtractZip, PatchJson, WriteBytes, WriteText
from .ledger import Ledger, Patch, Record
from .planner import Plan, Planned


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _backup(path: Path) -> Path:
    target = path.with_name(f"{path.name}.bak-{_stamp()}")
    if path.is_dir():
        shutil.copytree(path, target)
    else:
        shutil.copy2(path, target)
    return target


# ── 单个动作 ────────────────────────────────────────────


def _apply_text(action: WriteText | WriteBytes, *, force: bool) -> list[Path]:
    target = action.target
    if target.exists() and force:
        _backup(target)
    data = action.text.encode("utf-8") if isinstance(action, WriteText) else action.data
    _atomic_write(target, data)
    return [target]


def _apply_zip(action: ExtractZip, *, force: bool) -> list[Path]:
    target = action.target
    if target.exists() and not force:
        raise FileExistsError(str(target))       # 计划阶段应该已经拦下:这是最后一道
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".{target.name}.staging-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    relatives: list[Path] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(action.data))
        staging_root = staging.resolve()
        for info in archive.infolist():
            if info.is_dir():
                continue
            arc = info.filename
            if action.strip_top and "/" in arc:
                arc = arc.split("/", 1)[1]
            dest = staging / arc
            # 防 zip-slip:解出来的路径必须还在 staging 里
            if staging_root not in dest.resolve().parents:
                raise ValueError(f"包内路径越界:{info.filename}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(archive.read(info))
            relatives.append(Path(arc))
        if target.exists():
            _backup(target)
            shutil.rmtree(target)
        os.replace(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return [target / rel for rel in relatives]


def _apply_patch(action: PatchJson, *, force: bool) -> list[Path]:
    target = action.target
    raw: dict = {}
    if target.is_file():
        try:
            raw = json.loads(target.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"{target} 不是合法 JSON,不敢动它:{exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"{target} 顶层不是对象,不敢动它")
        if force:
            _backup(target)
    section = raw.get(action.section)
    if not isinstance(section, dict):
        section = {}
        raw[action.section] = section
    section[action.key] = action.value
    _atomic_write(target, (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return [target]


def _apply(action, *, force: bool) -> list[Path]:
    if isinstance(action, ExtractZip):
        return _apply_zip(action, force=force)
    if isinstance(action, PatchJson):
        return _apply_patch(action, force=force)
    if isinstance(action, (WriteText, WriteBytes)):
        return _apply_text(action, force=force)
    raise TypeError(f"未知动作:{action!r}")     # pragma: no cover


# ── 一条条目 ────────────────────────────────────────────


def _key_of(planned: Planned) -> str:
    for action in planned.actions:
        if isinstance(action, PatchJson):
            return action.key
    return planned.target.name


def apply_one(planned: Planned, *, ledger: Ledger, store: str, scope: str,
              force: bool) -> Record:
    files: list[Path] = []
    patches: list[Patch] = []
    for action in planned.actions:
        written = _apply(action, force=force)
        if isinstance(action, PatchJson):
            # **不能把 mcp.json / a2a.json 记进 files[]** —— 否则卸载时会把整个文件删掉,
            # 把用户其它的 server 一起带走。键的存活归 patches[] 管。
            patches.append(Patch(file=str(action.target), section=action.section,
                                 key=action.key))
        else:
            files.extend(written)
    item = planned.item
    record = Record(
        itemId=str(item.id),
        store=store,
        kind=str(item.kind),
        name=_key_of(planned),
        version=str(item.version),
        scope=scope,
        target=str(planned.target),
        files=[str(path) for path in files],
        patches=patches,
        needs_config=list(planned.needs_config),
        installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        upstream_updated_at=str(item.updated_at),
    )
    ledger.upsert(record)
    ledger.save()
    return record


def apply(plan: Plan, *, ledger: Ledger, store: str, scope: str,
          force: bool = False) -> list[Record]:
    """执行整份计划。冲突条目应在调用方被拦下(这里不替用户决定跳过还是覆盖)。"""
    return [apply_one(planned, ledger=ledger, store=store, scope=scope, force=force)
            for planned in plan.items]


# ── 卸载 ────────────────────────────────────────────────


def uninstall(record: Record, *, ledger: Ledger) -> list[str]:
    """删**账本登记过**的东西。返回人读的"丢掉了什么"。

    两道保险:① 只删 `files[]` / `patches[]` 里的东西;② 路径必须还在作用域根之内
    (对齐 qi-lab `skills.py` 的"我建的我才删")。
    """
    roots = [paths.global_agent_dir(), paths.project_dir(None)]
    removed: list[str] = []
    deleted: list[Path] = []
    for entry in record.files:
        path = Path(entry)
        if not _inside(path, roots):
            removed.append(f"跳过(不在已知根内):{path}")
            continue
        if path.is_file():
            path.unlink()
            deleted.append(path)
            removed.append(str(path))
    for patch in record.patches:
        file = Path(patch.file)
        if not file.is_file() or not _inside(file, roots):
            continue
        try:
            raw = json.loads(file.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        section = raw.get(patch.section)
        if isinstance(section, dict) and patch.key in section:
            section.pop(patch.key)
            if not section:
                raw.pop(patch.section, None)      # 段空了就整段删,别留 `{"mcpServers": {}}`
            if not raw:
                # 整个文件空了(我们建的那种) → 直接删掉,别在配置目录里留一个 `{}`
                file.unlink()
                removed.append(str(file))
                continue
            _atomic_write(file, (json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
                          .encode("utf-8"))
            removed.append(f"{file} → {patch.section}.{patch.key}")
    _prune_dirs(deleted, record, roots)
    ledger.remove(record.itemId, record.scope)
    ledger.save()
    return removed


def _inside(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _prune_dirs(deleted: list[Path], record: Record, roots: list[Path]) -> None:
    """把删空的目录也删掉(由深到浅),只往上走到作用域根,不越界。

    只从 `record.target` 开始删不够:技能包里的 `scripts/` 删空后会拦住父目录,
    结果就是一个空壳技能目录留在那里 —— `qi` 下次启动仍然会扫到它。
    """
    resolved_roots = {root.resolve() for root in roots}
    starts = {path.parent for path in deleted}
    if record.target:
        starts.add(Path(record.target))
    for start in sorted(starts, key=lambda p: len(p.parts), reverse=True):
        if not _inside(start, roots):
            continue
        for candidate in (start, *start.parents):
            if candidate.resolve() in resolved_roots or not _inside(candidate, roots):
                break
            try:
                if candidate.is_dir() and not any(candidate.iterdir()):
                    candidate.rmdir()
                else:
                    break
            except OSError:
                break
