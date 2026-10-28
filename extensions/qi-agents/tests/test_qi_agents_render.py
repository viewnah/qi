"""qi-agents 渲染面:平铺架子 / agent 卡 —— 点击折叠、流式跟底、不被 `Vertical`
默认 `height:1fr` + `overflow:hidden` 裁剪。

这三个用例原在 `tests/test_tui_extension_render.py`,但那个文件的其余测试是 **core 的
扩展渲染机制**(`src/qi_agent/tui.py` 如何消费 `renderCall`/`renderResult`),归属 core;
这里只搬 qi-agents 自己的 `_run_stack`/`_agent_card` 组件链,随扩展包一起维护。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "extensions" / "qi-agents"))

import pytest  # noqa: E402
@pytest.mark.asyncio
async def test_agent_cards_flatten_click_fold_and_ctrl_o_toggles_all():
    """平铺卡链路在**真 app**里验证(Static/滚动能否收到点击只有 pilot 说了算):
    每 agent 一张卡、标题 = `▸ 名字 ← 父 · 深度`;完成 `set_done` 自动收起为
    摘要 → 点击展开全文;`set_output`(ctrl+o 的转发通道)对整个架子**全局开合**,
    绝不把内置 tool_body 覆盖进平铺内容。
    用极简宿主而不是 QiTui:这里测的是组件与事件链,不涉工具卡片的装配(另有测试)。"""
    from textual.app import App

    from qi_agents.render import _run_stack, call_summary

    stack = _run_stack(call_summary({"agent": "librarian", "task": "查"}))
    assert stack is not None
    stack.styles.width = "100%"
    stack.styles.height = "auto"
    outer = stack.add_agent("scout", "qi", 1)
    inner = stack.add_agent("writer", "scout", 2)
    assert outer is not None and inner is not None
    outer.add_activity('read {"path": "a.py"}')
    outer.set_stream("第一段思考…", "think")

    host = App()
    async with host.run_test(size=(90, 40)) as pilot:
        await host.mount(stack)
        await pilot.pause()
        # 标题信息:名字 ← 由谁指派 · 深度(全树拍平在同一架子上)
        assert "▸ scout ← qi · 深度1" in outer._head.content.plain
        assert "▸ writer ← scout · 深度2" in inner._head.content.plain
        assert "运行中" in outer._head.content.plain
        body = outer._body.content.plain
        assert "· read" in body and "第一段思考" in body
        # 完成:自动收起为摘要 → 点击展开全文
        outer.set_done(True, "最终结论行", ["轮思考全文"])
        await pilot.pause()
        assert "✓" in outer._head.content.plain
        assert "点击展开" in outer._body.content.plain
        assert "轮思考全文" not in outer._body.content.plain
        assert await pilot.click(outer), "点击应命中卡片"
        await pilot.pause()
        assert "轮思考全文" in outer._body.content.plain
        # ctrl+o 的重设(set_output):架子级全局开合 —— 内置 body 不覆盖,
        # 所有卡同步折叠
        stack.set_output("内置tool_body")
        assert "内置tool_body" not in outer._body.content.plain
        assert "点击展开" in outer._body.content.plain, "全局开合:收起所有卡"
        # 再转发一次 → 全局展开
        stack.set_output("内置tool_body")
        assert "轮思考全文" in outer._body.content.plain


@pytest.mark.asyncio
async def test_agent_card_stream_visible_and_auto_follows_tail():
    """单卡:运行中默认展开(流式就在正文里滚),**每次刷新 scroll_end 自动跟底**
    —— 内容超高时 `scroll_offset` 应离开顶部,用户不用手动滚找进度;点击收起
    只剩一行;收起态的 set_stream 只存不刷,再展开数据仍在。"""
    from textual.app import App

    from qi_agents.render import _agent_card

    card = _agent_card("librarian", "qi", 1)
    assert card is not None
    card.styles.width = "100%"
    card.styles.height = "auto"
    card.set_stream("第一段思考…", "think")
    card.add_activity('read {"path": "a.py"}')

    host = App()
    async with host.run_test(size=(80, 30)) as pilot:
        await host.mount(card)
        await pilot.pause()
        head, body = card._head.content.plain, card._body.content.plain
        assert "librarian" in head and "← qi" in head and "深度1" in head
        assert "第一段思考" in body and "· read" in body
        # 超高流式 → 正文滚到底(自动跟底),不必手动滚动
        card.set_stream("\n".join(f"第{i}行思考" for i in range(30)), "think")
        await pilot.pause()
        assert card._scroll.scroll_offset.y > 0, "正文超高时应自动滚到最后一行"
        # 点击收起 → 再点展开
        assert await pilot.click(card), "点击应命中运行卡"
        await pilot.pause()
        assert "点击展开" in card._body.content.plain
        assert "第29行思考" not in card._body.content.plain
        # 收起态的流式只存不刷(不重渲染),展开后数据不丢
        card.set_stream("收起时到达的思考", "think")
        assert "收起时到达" not in card._body.content.plain
        assert await pilot.click(card)
        await pilot.pause()
        assert "收起时到达的思考" in card._body.content.plain


@pytest.mark.asyncio
async def test_agent_card_not_clipped_by_vertical_default_height():
    """回归:`Textual` 的 `Vertical` 默认 `height:1fr` + `overflow:hidden hidden`,
    `_AgentCard`/`_RunStack` 若不显式 `height:auto`,就会被裁到“父分配的 1fr 高度”
    —— 实测 12 行窗只露 ~6 行、展开的 auto 全文全被裁掉、跟底错乱。core 的
    `.msg{height:auto}` 只盖到最外层 `ExtensionToolBlock`,内部两层靠自己 auto。

    这里走**真实默认高度**(不手动 `card.styles.height="auto"` —— 那会绕过 bug,
    正是它溜过整轮测试的原因),断言不被裁、实时态滚到最新、点开后完全撑开。"""
    from textual.app import App
    from textual.containers import Vertical

    from qi_agents.render import _run_stack, call_summary

    stack = _run_stack(call_summary({"agent": "scout", "task": "查代码"}))
    card = stack.add_agent("scout", "qi", 1)
    assert card is not None
    card.set_stream("\n".join(f"流式{i}" for i in range(40)), "think")

    host = App()
    host.CSS = ".msg { width: 1fr; height: auto; }"   # type: ignore[attr-defined]  # core 对消息块的约定

    async with host.run_test(size=(80, 30)) as pilot:
        await host.mount(Vertical(stack, classes="msg"))
        await pilot.pause()
        # 实时态:卡高 = 头(1) + 滚动区(12),一个不裁(styles.height 会被渲染
        # 规范化成 Scalar,一律看**渲染尺寸** size.height)
        assert card.size.height >= 1 + 12, f"卡片被 Vertical 默认 1fr 裁剪:{card.size}"
        assert card._scroll.size.height == 12, f"滚动窗没拿到 12 行:{card._scroll.size}"
        assert card._scroll.scroll_offset.y > 0, "实时态应滚到最新(不是停在开头)"
        # 点两下 → 浏览态完全展开:auto、不裁、无内部滚动条
        await pilot.click(card)
        await pilot.pause()
        await pilot.click(card)
        await pilot.pause()
        lines = len(card._body.content.plain.splitlines())
        # 转浏览态 auto 生效 = 滚动区撑到内容高(无内部滚动条、不被裁)
        assert card.size.height >= lines, f"展开被裁:{card.size.height} < 正文 {lines} 行"
        assert not (card._scroll.size.height < card._scroll.virtual_size.height), \
            f"浏览态不该有内部滚动条:{card._scroll.size} vs {card._scroll.virtual_size}"
