/**
 * 中日韩文本的 `**加粗**` 兼容:让星号强强调能在**标点之后**收尾。
 *
 * 逐字节取自 dsh `ui-primitives/src/markdown/cjkFriendlyStrong.ts`(同一个问题、同一份解法)。
 *
 * 为什么需要它(不是可选的美化):CommonMark 规定"闭合星号"必须**右侧成翼**
 * (right-flanking):前面是标点、后面又是**非标点非空白**时,它不能闭合。中文写作里
 * 最常见的形态恰好全部落在这个死角上 ——
 *
 *     中文**重点:**后面还有中文          ← 前面是 `:`、后面是 `中` → 闭不上,星号原样显示
 *     **注意!**这一条很关键
 *
 * 于是"中文 + 冒号 + 加粗 + 紧跟中文"会渲染成一堆字面星号。dsh 的做法是加一条
 * micromark 语法扩展:当标记个数 ≥2、前一个字符是 unicode 标点、后一个字符是 CJK 时,
 * 也允许闭合。这里照抄。
 */
import { attention } from "micromark-core-commonmark";
import { unicodePunctuation } from "micromark-util-character";
import { classifyCharacter } from "micromark-util-classify-character";
import { codes, constants } from "micromark-util-symbol";
import type { Construct, Extension, State, Tokenizer } from "micromark-util-types";

const cjkCharacter = new RegExp(
  [
    "\\p{Script_Extensions=Han}",
    "\\p{Script_Extensions=Hiragana}",
    "\\p{Script_Extensions=Katakana}",
    "\\p{Script_Extensions=Hangul}",
    "\\p{Script_Extensions=Bopomofo}",
  ].join("|"),
  "u",
);

function isCjkCharacter(code: number | null): boolean {
  return (
    code !== null && code >= 0 && cjkCharacter.test(String.fromCodePoint(code))
  );
}

const tokenizeCjkFriendlyAttention: Tokenizer = function (effects, ok, nok) {
  const configuredAttentionMarkers = this.parser.constructs.attentionMarkers.null;
  if (configuredAttentionMarkers === undefined) {
    throw new Error("micromark CommonMark attention markers are unavailable");
  }
  const attentionMarkers = configuredAttentionMarkers;
  const previous = this.previous;
  const before = classifyCharacter(previous);
  let marker: number | null = codes.eof;

  return start;

  function start(code: number | null): State | undefined {
    if (code !== codes.asterisk) return nok(code);
    marker = code;
    effects.enter("attentionSequence");
    return inside(code);
  }

  function inside(code: number | null): State | undefined {
    if (code === marker) {
      effects.consume(code);
      return inside;
    }

    const token = effects.exit("attentionSequence");
    const after = classifyCharacter(code);
    const open =
      !after ||
      (after === constants.characterGroupPunctuation && Boolean(before)) ||
      attentionMarkers.includes(code);
    const commonMarkClose =
      !before ||
      (before === constants.characterGroupPunctuation && Boolean(after)) ||
      attentionMarkers.includes(previous);
    const markerCount = token.end.offset - token.start.offset;
    // 这就是 dsh 加的那一条:CJK 前的标点也允许闭合。
    const cjkStrongClose =
      markerCount >= 2 && unicodePunctuation(previous) && isCjkCharacter(code);
    const close = commonMarkClose || cjkStrongClose;

    token._open = open;
    token._close = close;
    return ok(code);
  }
};

const cjkFriendlyAttention: Construct = {
  name: "cjkFriendlyAttention",
  resolveAll: attention.resolveAll,
  tokenize: tokenizeCjkFriendlyAttention,
};

const cjkFriendlyStrongExtension: Extension = {
  text: { [codes.asterisk]: cjkFriendlyAttention },
};

/** 交给 `fromMarkdown` 的 micromark 扩展。 */
export function cjkFriendlyStrong(): Extension {
  return cjkFriendlyStrongExtension;
}
