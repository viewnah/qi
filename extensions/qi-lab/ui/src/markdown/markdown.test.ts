/**
 * markdown 渲染:结构 + **安全** + 中文强调。
 *
 * 为什么值得测(markdown 是唯一把"模型给的文本"变成结构的入口):
 *   1. **注入面**:`html` 节点、`javascript:` 链接、远程图片 —— 这三条是"渲染 markdown"
 *      这件事真正的风险,断言的是"它们没有变成可执行/可加载的东西";
 *   2. **中文加粗**:`中文**重点:**后面` 这种形态在 CommonMark 里闭不上(CJK 补丁就是
 *      为它写的,见 `cjk.ts`),它错了在中文界面里到处都是;
 *   3. 结构:代码块/列表/表格是回答的主要内容,渲染不出来等于白装。
 *
 * 用 `renderToStaticMarkup` 断言真渲染出来的 HTML(不需要 jsdom,node 环境即可)。
 */
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Markdown } from "./render";

const html = (text: string): string =>
  renderToStaticMarkup(createElement(Markdown, { text }));

describe("结构", () => {
  it("段落 / 加粗 / 斜体 / 行内代码", () => {
    const out = html("一段话 **粗** 和 *斜* 还有 `code`。");
    expect(out).toContain("<p>");
    expect(out).toContain("<strong>粗</strong>");
    expect(out).toContain("<em>斜</em>");
    expect(out).toContain('class="md-inline-code"');
  });

  it("代码围栏:语言标签 + 原文(并转义 HTML)", () => {
    const out = html('```python\nprint("<b>")\n```');
    expect(out).toContain('class="md-code"');
    expect(out).toContain("python");          // 语言标签
    expect(out).toContain("print(");
    expect(out).toContain("&lt;b&gt;");        // 代码里的标签被转义
    expect(out).not.toContain("<b>");
  });

  it("有序 / 无序 / 任务列表", () => {
    expect(html("1. 一\n2. 二")).toContain("<ol>");
    expect(html("- 一\n- 二")).toContain("<ul>");
    const task = html("- [x] 好了\n- [ ] 没做");
    expect(task).toContain("☑");
    expect(task).toContain("☐");
    expect(task).toContain('data-done="true"');
  });

  it("表格(首行是表头)、引用、分隔线", () => {
    const table = html("| a | b |\n| --- | --- |\n| 1 | 2 |");
    expect(table).toContain("<table>");
    expect(table).toContain('scope="col"');
    expect(table).toContain("class=\"md-table\"");
    expect(html("> 引用一句")).toContain("<blockquote>");
    expect(html("---")).toContain("<hr/>");
  });

  it("标题走 dsh 的阶梯类名(h1–h4)", () => {
    expect(html("# 一级")).toContain("<h1>");
    expect(html("### 三级")).toContain("<h3>");
  });
});

describe("安全:模型给的东西不能变成可执行/可加载的", () => {
  it("裸 HTML 只当文字(转义),不解析", () => {
    const out = html('前 <script>alert("x")</script> 后');
    expect(out).not.toContain("<script");
    expect(out).toContain("&lt;script&gt;");
    // 带事件属性的标签同样只是文字
    const img = html('<img src=x onerror="alert(1)">');
    expect(img).not.toContain("<img");
    expect(img).toContain("&lt;img");
  });

  it("危险协议的链接退化成纯文本(不放行 javascript: / data: / 相对路径)", () => {
    for (const url of [
      "javascript:alert(1)",
      "data:text/html,<script>alert(1)</script>",
      "/etc/passwd",
      "file:///etc/passwd",
    ]) {
      const out = html(`[点我](${url})`);
      expect(out).not.toContain("<a ");
      expect(out).toContain("点我");
    }
  });

  it("http / https / mailto 放行,并带上 rel", () => {
    const out = html("[文档](https://example.com/a?b=1&c=2)");
    expect(out).toContain('href="https://example.com/a?b=1&amp;c=2"');
    expect(out).toContain('target="_blank"');
    expect(out).toContain('rel="noreferrer noopener"');
    expect(html("[写邮件](mailto:a@b.com)")).toContain('href="mailto:a@b.com"');
  });

  it("远程图片不加载:渲染成链接,而且没有 <img>", () => {
    const out = html("![示意图](https://tracker.example.com/pixel.png)");
    expect(out).not.toContain("<img");
    expect(out).toContain('href="https://tracker.example.com/pixel.png"');
    expect(out).toContain("示意图");
  });
});

describe("中文强调(CJK 补丁)", () => {
  it("标点之后紧接中文也能闭上 —— 不打补丁时这里会露出字面星号", () => {
    const out = html("中文**重点:**后面还有中文");
    expect(out).toContain("<strong>重点:</strong>");
    expect(out).not.toContain("**重点:**");
  });

  it("全角标点 + 英文混排同样成立", () => {
    const out = html("注意(**必须**:先备份)再动手");
    expect(out).toContain("<strong>必须</strong>");
  });

  it("普通中文加粗照常(补丁不能把不该闭合的也闭合了)", () => {
    const out = html("这是**重点**内容");
    expect(out).toContain("<strong>重点</strong>");
    // 单个星号是斜体,不是加粗
    expect(html("这是*斜*体")).toContain("<em>斜</em>");
  });
});
