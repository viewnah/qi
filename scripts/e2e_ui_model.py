"""真 Chrome/CDP 端到端:输入卡右下的**模型 chip** —— 能不能点、切了之后界面与磁盘对不对。

为什么不用 vitest/jsdom:这一层要验的正是"浏览器里真的能点" —— chip 的定位
(portal + `position:fixed`)、菜单的层叠、以及"点完之后 chip 上的字有没有变",
都不是纯函数能回答的。仓库此前几节 UI 改动也是这么验的(design/web.md 的实测表)。

## 这份脚本自己踩过的两个坑(留着,免得下次再踩)

1. **`Page.navigate` 到"只差 hash"的 URL 是 same-document 导航**,CDP 下并不可靠 ——
   第一版用它切会话,读到的是上一态,于是"冤枉"了前端(前端其实是对的)。
   正确做法:改 `location.hash`(点左栏走的就是这条路,见 App.tsx 的 hashchange effect)。
2. **不能拿"chip 上的字"当"切换完成"的判据** —— 新会话**继承当前设置**,于是
   "切到 B"之后 B 可能与 A 显示同一个模型,循环会**立刻**满足(切换还没发生)。
   正确做法:等**网络**证据(`/api/models?session=<那个 id>` 的响应到了)。
   第一版就是这样把"还没切完"报成了"切错了"。

用法:`python scripts/e2e_ui_model.py`(需要本机装了 Google Chrome;headless 起一个临时实例)。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "extensions" / "qi-web"))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Cdp:
    """最小 CDP 客户端。

    **一个后台读协程**负责收消息并分发 —— 不让 `call()` 与"等某个事件"抢同一个
    websocket(那正是上一版的毛病:等响应时把 `call` 的回复也吞了)。
    """

    def __init__(self, ws_url: str) -> None:
        self.ws_url = ws_url
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._responses: list[str] = []      # 收到的 Network.responseReceived 的 URL
        self._errors: list[str] = []
        self._reader: asyncio.Task | None = None

    async def __aenter__(self) -> "Cdp":
        self.ws = await websockets.connect(self.ws_url, max_size=64 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read_loop())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
        await self.ws.close()

    async def _read_loop(self) -> None:
        async for raw in self.ws:
            msg = json.loads(raw)
            mid = msg.get("id")
            if mid is not None:
                fut = self._pending.pop(mid, None)
                if fut is not None and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(f"{msg['error']}"))
                    else:
                        fut.set_result(msg.get("result"))
                continue
            method = msg.get("method")
            if method == "Network.responseReceived":
                self._responses.append(msg["params"]["response"]["url"])
            elif method == "Runtime.exceptionThrown":
                self._errors.append(
                    str(msg["params"]["exceptionDetails"].get("text", "?")))
            elif (method == "Runtime.consoleAPICalled"
                  and msg["params"]["type"] == "error"):
                self._errors.append(str(msg["params"]["args"]))

    async def call(self, method: str, **params):
        self._id += 1
        mid = self._id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        return await asyncio.wait_for(fut, timeout=15)

    async def js(self, expr: str):
        r = await self.call("Runtime.evaluate", expression=expr,
                            returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    async def wait_response(self, needle: str, timeout: float = 10.0) -> bool:
        """等到某个 URL 的响应出现(判定"这一次切换真的完成了"的**唯一**可靠信号)。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(needle in url for url in self._responses):
                return True
            await asyncio.sleep(0.05)
        return False

    def clear_responses(self) -> None:
        self._responses.clear()

    @property
    def errors(self) -> list[str]:
        return list(self._errors)


CHIP_TEXT = ("[...document.querySelectorAll('.agentchip__btn')]"
             ".map(x=>x.textContent.trim()).join(' | ')")
CHIP_PROBE = """
[...document.querySelectorAll('.agentchip__btn')].map(x => ({
  text: x.textContent.trim(),
  x: Math.round(x.getBoundingClientRect().x),
  chevron: !!x.querySelector('.agentchip__chevron'),
  disabled: x.disabled}))
"""
MENU_ITEMS = """
[...document.querySelectorAll('.agentmenu--models .agentmenu__item')].map(x => ({
  name: x.querySelector('.agentmenu__name').textContent.trim(),
  note: x.querySelector('.agentmenu__note')?.textContent.trim() ?? '',
  checked: x.getAttribute('aria-checked')}))
"""


def open_chip(index: int) -> str:
    return f"""
      (() => {{
        const b = [...document.querySelectorAll('.agentchip__btn')][{index}];
        b.dispatchEvent(new MouseEvent('mousedown', {{bubbles:true, cancelable:true}}));
        b.click();
        return b.textContent.trim();
      }})()
    """


def pick_item(model_id: str) -> str:
    return f"""
      (() => {{
        const items = [...document.querySelectorAll('.agentmenu--models .agentmenu__item')];
        const t = items.find(x => x.querySelector('.agentmenu__name').textContent.includes('{model_id}'));
        if (!t) return 'NOTFOUND';
        t.dispatchEvent(new MouseEvent('mousedown', {{bubbles:true, cancelable:true}}));
        t.click();
        return 'ok';
      }})()
    """


async def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "models.json").write_text(json.dumps({"providers": {
        "alpha": {"api": "openai-completions", "apiKey": "k-alpha",
                  "models": [{"id": "m1", "contextWindow": 32000},
                             {"id": "m2", "contextWindow": 64000, "reasoning": True}]},
        "beta": {"api": "openai-completions", "apiKey": "$QI_E2E_MISSING",
                 "models": [{"id": "m3"}]},
    }}), encoding="utf-8")
    home = tmp / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"defaultProvider": "alpha", "defaultModel": "m1"}), encoding="utf-8")
    os.environ["QI_AGENT_CONFIG"] = str(tmp / "models.json")
    os.environ["QI_AGENT_HOME"] = str(home)
    os.environ.pop("QI_E2E_MISSING", None)
    proj = tmp / "proj"
    (proj / ".git").mkdir(parents=True)

    from qi_web.app import create_app

    app = create_app(cwd=proj, password=None, bind_host="127.0.0.1")
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    server_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)

    chrome_port = free_port()
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={chrome_port}",
         f"--user-data-dir={tmp / 'chrome'}", "--no-first-run", "--no-default-browser-check",
         "--disable-gpu", "--window-size=1440,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failed: list[str] = []

    def check(label: str, ok: bool, detail: str) -> None:
        print(f"{'✓' if ok else '✗'} {label}  {detail}")
        if not ok:
            failed.append(label)

    try:
        debug_base = f"http://127.0.0.1:{chrome_port}"
        for _ in range(150):
            try:
                async with httpx.AsyncClient() as c:
                    (await c.get(f"{debug_base}/json/version", timeout=1)).json()
                break
            except Exception:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Chrome 没起来")
        async with httpx.AsyncClient() as c:
            await c.put(f"{debug_base}/json/new?about:blank", timeout=5)
            page = next(t for t in (await c.get(f"{debug_base}/json/list", timeout=5)).json()
                        if t["type"] == "page")

        site = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=site) as c:
            a = (await c.post("/api/sessions",
                              json={"title": "A", "cwd": str(proj)})).json()["id"]
            b = (await c.post("/api/sessions",
                              json={"title": "B", "cwd": str(proj)})).json()["id"]

        async with Cdp(page["webSocketDebuggerUrl"]) as cdp:
            await cdp.call("Runtime.enable")
            await cdp.call("Page.enable")
            await cdp.call("Network.enable")

            async def chip_text() -> str:
                return await cdp.js(CHIP_TEXT) or ""

            async def switch_to(sid: str) -> None:
                """切会话:走真实路径(改 hash),等**网络证据**确认切完了。"""
                cdp.clear_responses()
                await cdp.js(f"window.location.hash = 's={sid}'; true")
                ok = await cdp.wait_response(f"/api/models?session={sid}")
                check("切会话发出 /api/models?session=…", ok, f"session={sid[:8]}")

            async def menu_is_open() -> bool:
                return bool(await cdp.js(
                    "!!document.querySelector('.agentmenu--models')"))

            async def open_menu() -> None:
                """把模型菜单**确保**打开。

                不能无脑 `open_chip(1)` —— 那个按钮是 **toggle**:菜单已经开着时再点
                等于关掉。第一版就是这样:第 ③ 步读完菜单没关,第 ④ 步"打开"实际是
                关闭,于是 `pick_item` 报"菜单里找不到 m2"。
                """
                if not await menu_is_open():
                    await cdp.js(open_chip(1))
                    await asyncio.sleep(0.35)

            async def close_menu() -> None:
                if await menu_is_open():
                    await cdp.js(
                        "document.dispatchEvent(new KeyboardEvent('keydown',"
                        "{key:'Escape', bubbles:true})); true")
                    await asyncio.sleep(0.2)

            async def pick(model_id: str) -> None:
                cdp.clear_responses()
                await open_menu()
                got = await cdp.js(pick_item(model_id))
                if got != "ok":
                    failed.append(f"菜单里找不到 {model_id}")
                    return
                ok = await cdp.wait_response("/api/model")
                check(f"切换 {model_id} 的请求往返", ok, "")

            # ── ① 首屏:两个 chip 都在,且模型 chip 可点 ──
            await cdp.call("Page.navigate", url=f"{site}/")
            await asyncio.sleep(2.0)
            chips = await cdp.js(CHIP_PROBE)
            check("输入卡右下有两个 chip(智能体 · 模型)", len(chips or []) == 2,
                  json.dumps(chips, ensure_ascii=False))
            model_chip = (chips or [{}, {}])[1]
            check("模型 chip 带 chevron(可点,不是只读回声)",
                  bool(model_chip.get("chevron")), f"disabled={model_chip.get('disabled')}")
            check("智能体 chip 在模型 chip 左边",
                  bool(chips) and chips[0]["x"] < chips[1]["x"],
                  f"x = {[c['x'] for c in chips] if chips else []}")

            # ── ② 打开 A:chip 显示当前模型 ──
            await cdp.call("Page.navigate", url=f"{site}/#s={a}")
            await asyncio.sleep(2.5)
            check("打开 A 后 chip", "m1" in await chip_text(), await chip_text())

            # ── ③ 菜单:清单 + 缺凭证标注 + 级别分节 + 层叠 ──
            cdp.clear_responses()
            await cdp.js(open_chip(1))
            await asyncio.sleep(0.4)
            items = await cdp.js(MENU_ITEMS)
            names = [i["name"] for i in (items or [])]
            check("菜单列出全部可选模型(含缺凭证的那个)",
                  [n.strip("✓ ") for n in names[:3]] == ["m1", "m2", "m3"],
                  json.dumps(names, ensure_ascii=False))
            notes = [i["note"] for i in (items or [])]
            check("缺凭证的 provider 标出来了", any("缺凭证" in n for n in notes),
                  json.dumps(notes[:3], ensure_ascii=False))
            check("上下文窗口标在 provider 后", any("32k" in n for n in notes), notes[0] if notes else "")
            check("当前值带 ✓",
                  any(i["checked"] == "true" and "m1" in i["name"] for i in (items or [])), "")
            check("思考级别在同一面板的第二节",
                  any(n.strip("✓ ") in ("medium", "high") for n in names),
                  json.dumps(names[3:], ensure_ascii=False))
            layered = await cdp.js("""
              (() => {
                const p = document.querySelector('.agentmenu--models');
                if (!p) return 'no-menu';
                const r = p.getBoundingClientRect();
                const hit = document.elementFromPoint(r.left + 20, r.top + 20);
                return p.contains(hit) ? 'yes' : 'no:' + (hit?.className ?? '?');
              })()
            """)
            check("菜单压得住输入卡上方的行(portal 生效)", layered == "yes", str(layered))
            await close_menu()

            # ── ④ 给 A 切到 m2 ──
            await pick("m2")
            await asyncio.sleep(0.8)
            check("A 切到 m2 后 chip 跟着变", "m2" in await chip_text(), await chip_text())

            # ── ⑤ 给 B 切到 m3 之外的一个**不同**值:验两条会话互不影响 ──
            await switch_to(b)
            await pick("m1")
            await asyncio.sleep(0.8)
            check("B 切到 m1", "m1" in await chip_text(), await chip_text())

            await switch_to(a)
            check("切回 A 仍是 m2(不被 B 覆盖)", "m2" in await chip_text(), await chip_text())
            await switch_to(b)
            check("再切到 B 仍是 m1", "m1" in await chip_text(), await chip_text())

            # ── ⑥ 级别切换:同一面板第二节 ──
            await switch_to(a)
            cdp.clear_responses()
            await open_menu()
            got = await cdp.js(pick_item("high"))
            if got == "ok":
                await cdp.wait_response("/api/model")
            else:
                failed.append("菜单里找不到 high")
            await asyncio.sleep(0.6)
            check("切级别后 chip 上的模型名不变(级别不占 chip)",
                  "m2" in await chip_text(), await chip_text())

            # ── ⑦ 页面错误 ──
            check("页面错误 = 0", len(cdp.errors) == 0, str(cdp.errors[:2]))

        # ── ⑧ 磁盘/内存对账(A、B 都还没说过话,所以 entry 只在内存里)──
        store = app.state.web.sessions
        recorded: dict[str, list] = {}
        for sid, who in ((a, "A"), (b, "B")):
            sess = store.get(sid)
            recorded[who] = [(e.get("type"), e.get("model_id") or e.get("thinking_level"))
                             for e in sess.branch()
                             if e.get("type") in ("model_change", "thinking_level_change")]
        check("A 记下 m1 → m2", ("model_change", "m2") in recorded["A"], str(recorded["A"]))
        check("B 记下 m1(它自己切的)", ("model_change", "m1") in recorded["B"],
              str(recorded["B"]))
        check("A 记下级别 high", ("thinking_level_change", "high") in recorded["A"],
              str(recorded["A"]))
    finally:
        chrome.terminate()
        server.should_exit = True
        await server_task
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + ("✓ 全部通过" if not failed else f"✗ 失败:{failed}"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
