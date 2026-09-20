/**
 * markdown → **mdast**(语法树),渲染在 `render.tsx`。
 *
 * 为什么用解析器而不是自己写正则:LLM 的回答里有代码围栏、嵌套列表、表格、
 * 行内代码 —— 手写正则在这四样上都会出错(围栏里出现的反引号、列表里的段落、
 * 表格里的转义),而这些是**回答的主要内容**,出错就是"看不懂"。dsh 用的也是
 * 同一族(micromark + mdast),这里只砍掉它的数学(katex)与语法高亮两个子系统。
 *
 * 管线:**不做 HTML 字符串**。`render.tsx` 把 mdast 走成 React 元素,所以
 * 用户/模型给的文本由 React 转义,天然没有注入面(见那边的文件头)。
 */
import type { Root } from "mdast";
import { fromMarkdown } from "mdast-util-from-markdown";
import { gfmFromMarkdown } from "mdast-util-gfm";
import { gfm } from "micromark-extension-gfm";
import { cjkFriendlyStrong } from "./cjk";

/**
 * 解析一份 markdown。
 *
 * 语法 = CommonMark + **GFM**(表格 / 删除线 / 任务列表 / 自动链接)+ CJK 强调补丁。
 * 不装数学扩展:qi 不渲染 TeX,装了只会把 `$x$` 吃成看不懂的节点。
 */
export function parseMarkdown(text: string): Root {
  return fromMarkdown(text, {
    extensions: [gfm(), cjkFriendlyStrong()],
    mdastExtensions: [gfmFromMarkdown()],
  });
}
