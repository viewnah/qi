"""会话自动命名:用**模型**给会话起一个短标题。

为什么需要它:qi 的会话默认叫「未命名」,左栏一列「未命名」等于没有左栏 —— 分不清哪条是哪条。
标题是**检索线索**,而生成它只需要一次很短的 LLM 调用(输入是用户的第一句话)。

三条设计取舍:

1. **只命名一次**:`title` 非空就不再调(用户手改过的标题绝不被覆盖)。
2. **纯函数收尾**:模型永远可能带上引号、井号、`标题:` 前缀、多行解释 —— `clean_title` 负责
   把它压成一行干净的标题,并且**只在这些清洗之后**才落盘(见那几条单测)。
3. **失败无声**:命名失败(超时/报错/返回垃圾)一律回落 `None`,**绝不**影响本轮回答 ——
   少一个标题远好过多一个错误。
"""

from __future__ import annotations

from .llm import ChatMessage, LLMClient

#: 给模型看的约束。刻意写"只输出标题本身":多数模型会顺手加上引号和句末标点。
TITLE_SYSTEM_PROMPT = """你负责给一段对话起标题。规则:

- 只输出标题本身:不要引号、不要井号、不要句末标点、不要任何解释
- 用与用户消息相同的语言,中文不超过 12 个字,英文不超过 8 个词
- 概括用户**想做什么**(动作 + 对象),不要复述原句、不要写"关于…的对话"

示例:
用户:帮我看看这个仓库的结构,顺便说下每个目录干什么
标题:梳理仓库结构"""

#: 标题长度上限(字符)。超出就截断 —— 左栏那一行本来也会省略号截断。
TITLE_MAX_CHARS = 30

#: 输入截断:标题只看开头,没必要把整条长消息喂进去。
TITLE_INPUT_CHARS = 600

#: 这些是"等于没起"的答案:模型偶尔会回它,写上反而占住名分。
_USELESS = {"未命名", "untitled", "none", "n/a", "无", "标题"}


def clean_title(raw: str | None) -> str | None:
    """把模型的原始输出压成一行标题;**拿不准就返回 `None`**(宁可不命名)。

    这一步是必需的,不是美化:实测模型会回 `标题:"梳理仓库结构"`、`### 仓库结构梳理`、
    甚至先写一句"这个对话是关于…的"。这些直接落盘,左栏里就是一行markdown 残渣。
    """
    if not raw:
        return None
    # 第一行有效内容(模型常先来一句解释再给标题)
    line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    if not line:
        return None
    # 去掉 `标题:` / `Title:` 这类前缀
    for prefix in ("标题:", "标题：", "title:", "Title:"):
        if line.startswith(prefix):
            line = line[len(prefix):].strip()
    # ATX 标题的井号是**前缀**(`### 标题 ##`),不是成对装饰 —— 单独剥。
    line = line.strip("#").strip()
    # 去掉成对的引号 / 反引号 / 星号等装饰(可能有嵌套,循环剥)
    for _ in range(4):
        before = line
        line = line.strip()
        for pair in (('"', '"'), ("'", "'"), ("“", "”"), ("「", "」"),
                     ("`", "`"), ("**", "**"), ("*", "*")):
            if len(line) > 2 * len(pair[0]) and line.startswith(pair[0]) and line.endswith(pair[1]):
                line = line[len(pair[0]):-len(pair[1])].strip()
        if line == before:
            break
    line = " ".join(line.split())            # 折叠内部空白
    line = line.rstrip("。.!,;:,、")          # 句末标点
    if not line or line.strip().lower() in _USELESS:
        return None
    return line[:TITLE_MAX_CHARS]


async def suggest_title(llm: LLMClient, first_user_text: str) -> str | None:
    """让模型给一个会话标题。

    **不抛异常**(调用方在流式回合里 await 它):拿不到就回 `None`,那一轮照常结束。
    """
    text = (first_user_text or "").strip()[:TITLE_INPUT_CHARS]
    if not text:
        return None
    try:
        resp = await llm.chat([ChatMessage(role="system", content=TITLE_SYSTEM_PROMPT),
                               ChatMessage(role="user", content=text)])
    except Exception:                    # noqa: BLE001 命名失败不该影响回答
        return None
    if getattr(resp, "tool_calls", None):
        return None                      # 它想调工具就说明没在起标题
    return clean_title(getattr(resp, "text", None))
