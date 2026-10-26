"""`assistants.json` 的读写与校验 —— **唯一事实源**。

为什么是文件而不是数据库：

* 条目量级是几十条，一次全读；
* 目录是**内容**（像技能 / 角色），要跟仓库走、能 review、能回滚；
* 只有一个写入者（qi-lab 设置页），不需要行级锁。

于是这里只做三件事：**读 → 校验 → 原子写**。所有消费者（qi-lab 的 HTTP 端点、
`assistant_search` / `assistant_recommend`）都走这一份实现，不另抄一遍规则。

文件位置固定为 `<cwd>/.qi/assistants.json`（项目级）。没有用户级、没有合并。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from qi_agent.paths import project_home

#: 文件名（项目级 `.qi/` 下）。
CATALOG_FILE_NAME = "assistants.json"
#: 文件结构版本。以后形状不兼容时升它，读端不认识就拒绝，而不是猜。
SCHEMA_VERSION = 1
#: 单文件条目上限：防一次误导入把文件写成一兆。
MAX_ENTRIES = 200

_NAME_MAX = 40
_DESCRIPTION_MAX = 400
_ITEM_MAX = 60
_TASKS_MAX = 8
_OUTPUTS_MAX = 8
_KEYWORDS_MAX = 12
_KEYWORD_MAX = 20
_URL_MAX = 2000

_ID_RE = re.compile(r"^as_[0-9a-f]{8}$")
#: 写操作串行化。范围是**本进程**：qi-lab 是单进程，够用；跨进程并发不在本期。
_LOCK = threading.RLock()

#: 允许出现在条目里的键。多一个就报错 —— 拼错字段名不该静默丢掉。
_FIELDS = {"id", "name", "description", "tasks", "outputs", "url", "keywords", "enabled"}
_PATCHABLE = _FIELDS - {"id"}


class CatalogError(Exception):
    """目录层的错误。`status` 是要回给 HTTP 的状态码（工具侧只取 detail）。"""

    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


@dataclass(frozen=True)
class Assistant:
    """一个应用助手。字段顺序与文件里的顺序一致。"""

    id: str
    name: str
    description: str
    tasks: tuple[str, ...]
    outputs: tuple[str, ...]
    url: str
    keywords: tuple[str, ...] = ()
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tasks": list(self.tasks),
            "outputs": list(self.outputs),
            "url": self.url,
            "keywords": list(self.keywords),
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class Catalog:
    """一次读的结果。`exists=False` 也回 —— 界面据此区分“没文件”与“文件空”。"""

    path: Path
    exists: bool
    assistants: tuple[Assistant, ...]

    def find(self, assistant_id: str) -> Assistant | None:
        for assistant in self.assistants:
            if assistant.id == assistant_id:
                return assistant
        return None

    def active(self) -> tuple[Assistant, ...]:
        """参与推荐的条目。停用只是隐藏，记录还在（删才是真没了）。"""
        return tuple(a for a in self.assistants if a.enabled)


def path_for(cwd: Path | str | None = None) -> Path:
    """项目级目录文件。与 qi 的 `.qi/` 约定同一个来源（`paths.project_home`）。"""
    return project_home(Path(cwd) if cwd is not None else None) / CATALOG_FILE_NAME


def _text(value: object, field: str, *, limit: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise CatalogError(f"{field} 必须是字符串")
    text = " ".join(value.split())
    if not text:
        if allow_empty:
            return ""
        raise CatalogError(f"{field} 不能为空")
    if len(text) > limit:
        raise CatalogError(f"{field} 最多 {limit} 字（现在 {len(text)} 字）")
    return text


def _items(value: object, field: str, *, limit: int, item_max: int,
           minimum: int) -> tuple[str, ...]:
    # 字符串是最常见的误写（"a,b"），单独给一句能看懂的话，而不是“类型不对”。
    if isinstance(value, str):
        raise CatalogError(f"{field} 必须是数组，不是字符串")
    if not isinstance(value, (list, tuple)):
        raise CatalogError(f"{field} 必须是数组")
    out: list[str] = []
    for item in value:
        text = _text(item, field, limit=item_max)
        if text not in out:          # 去重，保留第一次出现的顺序
            out.append(text)
    if len(out) < minimum:
        raise CatalogError(f"{field} 至少 {minimum} 条")
    if len(out) > limit:
        raise CatalogError(f"{field} 最多 {limit} 条")
    return tuple(out)


def _url(value: object) -> str:
    if not isinstance(value, str):
        raise CatalogError("url 必须是字符串")
    url = value.strip()
    if not url:
        raise CatalogError("url 不能为空")
    if len(url) > _URL_MAX:
        raise CatalogError(f"url 最多 {_URL_MAX} 字")
    # 空白 / 控制字符 / 反斜杠：浏览器与 urlsplit 对它们的解释不一致，直接拒绝。
    if any(ch.isspace() or ord(ch) < 0x20 for ch in url) or "\\" in url:
        raise CatalogError("url 不能包含空白、控制字符或反斜杠")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise CatalogError(f"url 不合法：{exc}") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise CatalogError("url 只接受 http(s):// 绝对地址")
    if not parts.netloc:
        raise CatalogError("url 缺少主机名")
    try:
        hostname = parts.hostname
        parts.port  # 端口写成 abc 时 urlsplit 不报，取一次才会抛
    except ValueError as exc:
        raise CatalogError(f"url 不合法：{exc}") from exc
    if not hostname:
        raise CatalogError("url 缺少主机名")
    if parts.username or parts.password:
        raise CatalogError("url 不能带用户名/密码")
    return url


def _enabled(value: object) -> bool:
    """`enabled` 只收真正的 bool：`1` / `"true"` 这类误写当场报错。"""
    if value is None:
        return True
    if not isinstance(value, bool):
        raise CatalogError("enabled 必须是 true / false")
    return value


def _clean(payload: object, *, entry_id: str, others: set[str]) -> Assistant:
    """校验并归一化一条记录。`others` 是**除自己以外**已占用的小写名字。"""
    if not isinstance(payload, dict):
        raise CatalogError("条目必须是 JSON 对象")
    unknown = sorted(set(payload) - _FIELDS)
    if unknown:
        raise CatalogError(f"不认识的字段：{', '.join(unknown)}")
    if not isinstance(entry_id, str) or not _ID_RE.fullmatch(entry_id):
        raise CatalogError("id 必须形如 as_ + 8 位十六进制")

    name = _text(payload.get("name"), "name", limit=_NAME_MAX)
    if name.casefold() in others:
        raise CatalogError(f"已有同名助手：{name}", status=409)

    return Assistant(
        id=entry_id,
        name=name,
        description=_text(payload.get("description"), "description", limit=_DESCRIPTION_MAX),
        tasks=_items(payload.get("tasks"), "tasks", limit=_TASKS_MAX,
                     item_max=_ITEM_MAX, minimum=1),
        outputs=_items(payload.get("outputs"), "outputs", limit=_OUTPUTS_MAX,
                       item_max=_ITEM_MAX, minimum=1),
        url=_url(payload.get("url")),
        keywords=_items(payload.get("keywords") or [], "keywords",
                        limit=_KEYWORDS_MAX, item_max=_KEYWORD_MAX, minimum=0),
        enabled=_enabled(payload.get("enabled")),
    )


def _read_raw(path: Path) -> dict | None:
    """读文件。不存在 → None；坏 JSON / 坏形状 → 503（带着哪一条坏）。"""
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CatalogError(f"读不动 {path.name}：{exc}", status=503) from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"{path.name} 不是合法 JSON：{exc}", status=503) from exc
    if not isinstance(raw, dict):
        raise CatalogError(f"{path.name} 顶层必须是对象", status=503)
    version = raw.get("version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise CatalogError(
            f"{path.name} 的 version={version!r} 不认识（本版只认 {SCHEMA_VERSION}）",
            status=503,
        )
    return raw


def _migrate_legacy(entry: object) -> object:
    """旧 `status` → `enabled`（读兼容，不要求用户手工改文件）。

    只有 `published` 算启用；`draft` / `archived` 都归到停用。
    同时出现两者时以 `enabled` 为准，`status` 丢弃。
    """
    if not isinstance(entry, dict) or "status" not in entry:
        return entry
    migrated = dict(entry)
    status = migrated.pop("status")
    if "enabled" not in migrated:
        migrated["enabled"] = str(status).strip().lower() == "published"
    return migrated


def load(cwd: Path | str | None = None) -> Catalog:
    """读整个目录。**每次现读，不缓存** —— qi-lab 改完下一轮工具调用立刻生效。"""
    path = path_for(cwd)
    raw = _read_raw(path)
    if raw is None:
        return Catalog(path=path, exists=False, assistants=())
    entries = raw.get("assistants")
    if not isinstance(entries, list):
        raise CatalogError(f"{path.name} 的 assistants 必须是数组", status=503)
    if len(entries) > MAX_ENTRIES:
        raise CatalogError(f"{path.name} 最多 {MAX_ENTRIES} 条", status=503)
    out: list[Assistant] = []
    names: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        try:
            entry = _migrate_legacy(entry)
            entry_id = entry.get("id") if isinstance(entry, dict) else None
            assistant = _clean(entry, entry_id=entry_id, others=names)
        except CatalogError as exc:
            raise CatalogError(
                f"{path.name} 第 {index} 条无效：{exc.detail}",
                status=503,
            ) from exc
        names.add(assistant.name.casefold())
        out.append(assistant)
    return Catalog(path=path, exists=True, assistants=tuple(out))


def _write(path: Path, assistants: tuple[Assistant, ...]) -> None:
    """原子写：同目录临时文件 → fsync → `os.replace`。半个 JSON 比没写更糟。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SCHEMA_VERSION,
        "assistants": [assistant.to_dict() for assistant in assistants],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                    prefix=".assistants.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _new_id(taken: set[str]) -> str:
    while True:
        candidate = f"as_{secrets.token_hex(4)}"
        if candidate not in taken:
            return candidate


def create(cwd: Path | str | None, payload: dict) -> Catalog:
    """新建一条。`id` 由服务端生成；同名（忽略大小写）→ 409。"""
    with _LOCK:
        current = load(cwd)
        if len(current.assistants) >= MAX_ENTRIES:
            raise CatalogError(f"最多 {MAX_ENTRIES} 条", status=409)
        if "id" in payload:
            raise CatalogError("id 由服务端生成，不要自己传")
        entry_id = _new_id({a.id for a in current.assistants})
        assistant = _clean(payload, entry_id=entry_id,
                           others={a.name.casefold() for a in current.assistants})
        _write(current.path, current.assistants + (assistant,))
        return load(cwd)


def update(cwd: Path | str | None, assistant_id: str, patch: dict) -> Catalog:
    """局部更新：**只改送来的键**；没送的保持原值。未知键 → 400。"""
    with _LOCK:
        current = load(cwd)
        target = current.find(assistant_id)
        if target is None:
            raise CatalogError(f"没有这个助手：{assistant_id}", status=404)
        unknown = sorted(set(patch) - _PATCHABLE)
        if unknown:
            raise CatalogError(f"不认识的字段：{', '.join(unknown)}")
        if not patch:
            raise CatalogError("没有要改的字段")
        if any(value is None for value in patch.values()):
            # 清空数组请传 []；null 会让“没送”和“送了个空”混在一起。
            raise CatalogError("字段不能为 null；清空数组请传 []")
        merged = target.to_dict()
        merged.update(patch)
        others = {a.name.casefold() for a in current.assistants if a.id != target.id}
        updated = _clean(merged, entry_id=target.id, others=others)
        assistants = tuple(updated if a.id == target.id else a
                           for a in current.assistants)
        _write(current.path, assistants)
        return load(cwd)


def delete(cwd: Path | str | None, assistant_id: str) -> Catalog:
    """硬删一条。想“下线但保留”请把 `enabled` 改成 `false`。"""
    with _LOCK:
        current = load(cwd)
        if current.find(assistant_id) is None:
            raise CatalogError(f"没有这个助手：{assistant_id}", status=404)
        assistants = tuple(a for a in current.assistants if a.id != assistant_id)
        _write(current.path, assistants)
        return load(cwd)


def seed_entries() -> list[dict]:
    """包里带的示例条目（**不带 id**，导入时再分配）。"""
    path = Path(__file__).parent / "seed" / "assistants.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"示例文件不可用：{exc}", status=503) from exc
    entries = raw.get("assistants") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise CatalogError("示例文件的 assistants 必须是数组", status=503)
    return entries


def import_seed(cwd: Path | str | None, *, mode: str = "merge") -> tuple[Catalog, int, int]:
    """导入示例。返回 `(目录, 新建数, 跳过数)`。

    `merge` 跳过同名条目（重复点“导入示例”不该报错）；`replace` 先清空再写。
    """
    if mode not in ("merge", "replace"):
        raise CatalogError("mode 只能是 merge / replace")
    with _LOCK:
        current = load(cwd)
        assistants = [] if mode == "replace" else list(current.assistants)
        names = {a.name.casefold() for a in assistants}
        ids = {a.id for a in assistants}
        created = skipped = 0
        for raw in seed_entries():
            name = _text(raw.get("name"), "name", limit=_NAME_MAX)
            if name.casefold() in names:
                skipped += 1
                continue
            entry_id = _new_id(ids)
            assistant = _clean(raw, entry_id=entry_id, others=names)
            assistants.append(assistant)
            names.add(assistant.name.casefold())
            ids.add(entry_id)
            created += 1
        if len(assistants) > MAX_ENTRIES:
            raise CatalogError(f"导入后超过 {MAX_ENTRIES} 条", status=409)
        _write(current.path, tuple(assistants))
        return load(cwd), created, skipped
