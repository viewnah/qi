"""会话自动命名:模型的原始输出要洗干净,失败不能影响本轮回答。

两件事值得钉死:
  1. `clean_title` —— 模型**一定**会带回引号/井号/`标题:` 前缀/多行解释,这些直接落盘
     就是左栏里一行 markdown 残渣;而"等于没起"的答案(未命名 / untitled)也不该占名分;
  2. 命名失败(报错、超时、返回垃圾)必须**无声回落**,不能把那一轮回答带崩。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from qi_agent.llm import ChatResponse, LLMDelta  # noqa: E402
from qi_agent.runtime import QiRuntime, RuntimeConfig  # noqa: E402
from qi_agent.session import SessionStore  # noqa: E402
from qi_agent.titling import TITLE_MAX_CHARS, clean_title, suggest_title  # noqa: E402


# ── 1. 清洗(纯函数)──────────────────────────────────────

def test_clean_title_strips_model_decoration():
    assert clean_title('标题:"梳理仓库结构"') == "梳理仓库结构"
    assert clean_title("### 仓库结构梳理") == "仓库结构梳理"
    assert clean_title("**排查登录 500**") == "排查登录 500"
    assert clean_title("Title: Fix the login bug.") == "Fix the login bug"
    assert clean_title("`git 历史重写`") == "git 历史重写"


def test_clean_title_takes_the_first_meaningful_line():
    """模型常先写一句解释再给标题 —— 取第一行有效内容。"""
    assert clean_title("这个对话是关于数据库迁移的\n\n梳理迁移步骤") == "这个对话是关于数据库迁移的"
    assert clean_title("\n\n   \n只改配置   ") == "只改配置"


def test_clean_title_refuses_useless_answers():
    """宁可不命名,也不写一个占位的假标题。"""
    for raw in (None, "", "   ", "未命名", "Untitled", "无"):
        assert clean_title(raw) is None


def test_clean_title_caps_length():
    long = "很长" * 40
    got = clean_title(long)
    assert got is not None and len(got) == TITLE_MAX_CHARS


# ── 2. 命名调用 ─────────────────────────────────────────

class _ChatStub:
    """只实现 `chat`(LLMClient 的最小契约)—— 命名只用到它。"""

    def __init__(self, reply: str | None = None, boom: bool = False,
                 tool_calls: bool = False) -> None:
        self.reply, self.boom, self.tool_calls = reply, boom, tool_calls
        self.seen: list[str] = []

    async def chat(self, messages, tools=None, temperature=None):
        self.seen.append(messages[-1].content)
        if self.boom:
            raise RuntimeError("provider 挂了")
        if self.tool_calls:
            from qi_agent.llm import ToolCallOut
            return ChatResponse(text="", tool_calls=[ToolCallOut(id="c", name="ls", args={})])
        return ChatResponse(text=self.reply or "")


@pytest.mark.asyncio
async def test_suggest_title_happy_path():
    stub = _ChatStub('"梳理仓库结构"')
    assert await suggest_title(stub, "帮我看看这个仓库") == "梳理仓库结构"
    assert "帮我看看这个仓库" in stub.seen[0]


@pytest.mark.asyncio
async def test_suggest_title_never_raises():
    """命名失败必须无声 —— 它跑在回合收尾路径上,抛出去就是"回答完了却报错"。"""
    assert await suggest_title(_ChatStub(boom=True), "任意") is None
    assert await suggest_title(_ChatStub(tool_calls=True), "任意") is None
    assert await suggest_title(_ChatStub("未命名"), "任意") is None
    assert await suggest_title(_ChatStub("ok"), "   ") is None


# ── 3. 端到端:一轮跑完,标题落盘 ──────────────────────

class _TurnStub:
    """`astream` 出回答、`chat` 出标题 —— 正好对应 runtime 的两条路径。"""

    def __init__(self) -> None:
        self.streams = 0
        self.title_calls = 0
        self.title_inputs: list[str] = []

    async def chat(self, messages, tools=None, temperature=None):
        self.title_calls += 1
        self.title_inputs.append(messages[-1].content)
        return ChatResponse(text="梳理仓库结构")

    async def astream(self, messages, tools=None, temperature=None):
        self.streams += 1
        yield LLMDelta(text="看完了。")
        yield LLMDelta(finished=True, usage={"total_tokens": 1})


@pytest.mark.asyncio
async def test_first_turn_names_the_session_once(tmp_path):
    store = SessionStore(root=tmp_path / "sessions")
    stub = _TurnStub()
    runtime = QiRuntime(cwd=tmp_path, runtime_cfg=RuntimeConfig(workdir=tmp_path),
                        session_store=store, llm=stub, disable_router=True)
    session = store.create("", cwd=tmp_path)
    assert session.title == ""                      # 新会话:未命名

    async for _ in runtime.stream("帮我看看这个仓库的结构", session):
        pass

    assert session.title == "梳理仓库结构"
    # 内存 + header entry + 磁盘三处一致(取名只写内存是常见的坑)
    on_disk = [json.loads(l) for l in session.path.read_text(encoding="utf-8").splitlines()]
    assert on_disk[0]["title"] == "梳理仓库结构"
    reloaded = store.get(session.id)
    assert reloaded is not None                      # 重新读盘:标题真的写上去了
    assert reloaded.title == "梳理仓库结构"

    # 第二轮不重复命名(标题非空)
    async for _ in runtime.stream("再看看测试", reloaded):
        pass
    assert stub.title_calls == 1


@pytest.mark.asyncio
async def test_naming_failure_leaves_the_turn_intact(tmp_path):
    """命名炸了:回答照常落盘,标题保持「未命名」——不编、不报错。"""
    class _Boom(_TurnStub):
        async def chat(self, messages, tools=None, temperature=None):
            raise RuntimeError("provider 挂了")

    store = SessionStore(root=tmp_path / "sessions")
    runtime = QiRuntime(cwd=tmp_path, runtime_cfg=RuntimeConfig(workdir=tmp_path),
                        session_store=store, llm=_Boom(), disable_router=True)
    session = store.create("", cwd=tmp_path)
    async for _ in runtime.stream("你好", session):
        pass

    assert session.title == ""
    roles = [e.get("role") for e in session.branch() if e.get("type") == "message"]
    assert roles == ["user", "assistant"]           # 这一轮的往来都在


@pytest.mark.asyncio
async def test_naming_an_old_session_uses_its_first_message(tmp_path):
    """老会话(上线之前建的)第一次跑时补标题:输入是它原本的第一句话。

    否则续聊一句"接着再补个测试",一个讲仓库结构的会话会被命名成「补充测试」。
    """
    store = SessionStore(root=tmp_path / "sessions")
    stub = _TurnStub()
    runtime = QiRuntime(cwd=tmp_path, runtime_cfg=RuntimeConfig(workdir=tmp_path),
                        session_store=store, llm=stub, disable_router=True)
    session = store.create("", cwd=tmp_path)                    # 有历史、没有标题
    store.append(session, {"type": "message", "role": "user", "content": "最早那句:看下仓库结构"})
    store.append(session, {"type": "message", "role": "assistant", "content": "好"})

    async for _ in runtime.stream("接着再补个测试", session):
        pass

    assert stub.title_inputs == ["最早那句:看下仓库结构"]
    assert session.title == "梳理仓库结构"
