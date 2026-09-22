#!/usr/bin/env python3
"""删掉会话目录里的**空会话**(没有任何 message 的文件)。

为什么需要它:懒建上线之前,`qi` 一进 TUI 就 `store.create()` 落一个空文件,`--no-session`
也照样落 —— 开发机上因此攒了 3000+ 个只有 header 的会话(事故记录见
`docs/session-format.md` §9.1)。懒建堵住了源头,但**存量**还在:它们会挤满 `/resume`
选择器,还让每次 `list()` 都白读几千个文件。

判定口径(保守,**只删确定没有内容的**):
  * 一个 `message` entry 都没有 → 删除(含 0 行、只有 header、只有设置类 entry 的);
  * 有任何 `message` → **保留**(哪怕只有用户那句、没有回答)。

为什么把 `title` 也看一眼:用户可能给一个空会话手动改过名(`/name`)—— 那是**用户的动作**,
删掉等于替他做了决定。这类**默认保留**,只是列出来。

`--include-named` 会把这层也放开(无消息 + 有名字也删)。它存在的理由:测试泄漏的垃圾
会带着测试用的假名字(例如 `create("t")` 留下的 `"t"`),默认口径清不掉。删之前**务必先看
dry-run 清单**——放宽之后脚本就不再替你区分"用户的空会话"和"测试垃圾"了。

默认 dry-run(只列不删)。要真删加 `--apply`。

用法:
    python scripts/prune_empty_sessions.py                # 列清单
    python scripts/prune_empty_sessions.py --apply        # 真删
    python scripts/prune_empty_sessions.py --dir <路径>   # 换会话目录
    python scripts/prune_empty_sessions.py --include-named --apply   # 连"有名字但无消息"一起删
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qi_agent import paths  # noqa: E402


def default_sessions_dir() -> Path:
    """与生产同一套路径推导(尊重 `QI_AGENT_HOME` / `settings.sessionDir` 之外的那层)。

    `settings.sessionDir` 这里不读:它是**项目级**配置,而这个脚本是全局清理工具 ——
    要清别的目录就显式 `--dir`。
    """
    return paths.global_home() / "sessions"


def looks_empty(path: Path) -> tuple[bool, str]:
    """→ `(能不能删, 为什么不能删)`。

    读不出来的文件(权限、坏 JSON)**不算空** —— 宁可留着一个可疑文件,也不删掉一个
    可能含内容的会话。会话是不可再生的用户数据。
    """
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return False, "读不了"
    if not lines:
        return True, ""                       # 0 行:确定是空的
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            return False, "有坏行"
    if any(e.get("type") == "message" for e in entries):
        return False, "有消息"
    title = str(entries[0].get("title") or "").strip() if entries else ""
    if title and title != "tui":                # `tui` 是旧版写死的默认名,不算用户起的
        return False, f"无消息但有名字({title})"
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=None,
                    help="会话目录(默认 ~/.qi/agent/sessions,尊重 QI_AGENT_HOME)")
    ap.add_argument("--apply", action="store_true", help="真删(默认只列清单)")
    ap.add_argument("--include-named", action="store_true",
                    help="连「无消息但有名字」的也删(默认保留;先看 dry-run 清单再用)")
    args = ap.parse_args()

    root = (args.dir or default_sessions_dir()).expanduser()
    if not root.is_dir():
        print(f"没有这个目录: {root}")
        return 1

    files = sorted(root.glob("*.jsonl"))
    plain: list[Path] = []           # 无消息、也没名字 —— 谁都不会要
    named: list[Path] = []           # 无消息但**有名字** —— 有可能是用户起的,默认保留
    kept: list[tuple[Path, str]] = []          # 有内容,保留
    for path in files:
        empty, why = looks_empty(path)
        if empty:
            plain.append(path)
        elif why.startswith("无消息但有名字"):
            named.append(path)
        else:
            kept.append((path, why))
    doomed = plain + (named if args.include_named else [])

    print(f"会话目录: {root}")
    print(f"共 {len(files)} 个文件:")
    print(f"  {len(plain):5} 个空会话(无消息、无名字)")
    if named:
        if args.include_named:
            print(f"  {len(named):5} 个无消息**但有名字** —— 已因 --include-named 一并删除")
        else:
            print(f"  {len(named):5} 个无消息**但有名字** —— 可能是用户起的,保留(见下)")
    print(f"  {len(kept):5} 个保留")
    for why, count in sorted(
            collections.Counter(w for _, w in kept).items(), key=lambda kv: -kv[1]):
        print(f"        保留 {count:5} 个 —— {why}")
    print(f"\n→ 本次{'删除' if args.apply else '将删除'} {len(doomed)} 个")

    if named and args.apply and not args.include_named:
        print("\n(这些**不删**,列出来让你确认:)")
        for path in named[:10]:
            print(f"  {path.name}")

    if not doomed:
        print("没有要删的。")
        return 0

    if not args.apply:
        print("\n空会话清单(dry-run;加 --apply 才真删):")
        for path in doomed[:40]:
            print(f"  {path.name}")
        if len(doomed) > 40:
            print(f"  … 还有 {len(doomed) - 40} 个")
        return 0

    removed = 0
    for path in doomed:
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            print(f"  删不掉 {path.name}: {exc}", file=sys.stderr)
    kept_named = 0 if args.include_named else len(named)
    print(f"\n已删除 {removed} 个空会话(保留 {kept_named} 个无消息但有名字的)。")
    if removed != len(doomed):
        print("有文件没删掉(见上面 stderr);重跑一次或手动处理。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
