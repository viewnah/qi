/**
 * mdast → **React 元素**。整条管线里**不出现 HTML 字符串**。
 *
 * 这是安全设计的全部:模型（以及用户粘进来的内容）是不可信输入,而这里的
 * 每一种节点都走 React 元素 —— 文本由 React 转义、属性由 React 序列化,
 * 所以**没有注入面**,也就不需要额外的 sanitizer(那种"先拼 HTML 再消毒"的做法
 * 一旦消毒白名单漏一条就是 XSS)。dsh 的 `render.tsx` 是同一条路。
 *
 * 三条**明确**的安全/降级决定:
 *
 *   1. **markdown 里的裸 HTML 当作文字显示**。`<script>`、`<img onerror=…>` 这类
 *      在 CommonMark 里是 `html` 节点,这里渲染成**可见的字面文本**,不执行、不解析
 *      (dsh 也这样处理)。
 *   2. **链接只放行 `http:` / `https:` / `mailto:`**。`javascript:`、`data:` 与相对
 *      路径一律退化成纯文本 —— 退化的方向是"少一个链接",不是"多一个可点的东西"。
 *   3. **图片不加载**。qi 没有图片代理,渲染 `<img src="https://…">` 等于把用户的 IP
 *      与 referrer 送给回答里出现的任意域名(还可能是一张 1×1 追踪像素)。所以
 *      `![alt](url)` 渲染成**链接**(文字用 alt,没有 alt 就用 URL),要不要点由用户决定。
 *
 * 未知节点类型同样**不丢**:有子节点就照渲子节点(markdown 家族一直在长新节点,
 * 丢弃会让"模型发了东西但没人看见")。
 */
import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { RootContent } from "mdast";
import { IconCheckOutline16, IconCopyOutline16 } from "../components/icons";
import { parseMarkdown } from "./parse";

/** 渲染上下文:代码块的复制按钮要用到 App 的剪贴板回调。 */
type Ctx = {
  /** 返回是否真的复制成功(成功才把图标换成勾)。 */
  onCopy?: (text: string) => Promise<boolean>;
  /** 流式光标:只加在**最后一个顶层块**的行内末尾(见 `Markdown`)。 */
  caret?: boolean;
  lastIndex?: number;
};

/** 允许出现在 `href` 里的协议。别的一律退化成纯文本。 */
const SAFE_SCHEME = /^(https?:|mailto:)/i;

function safeHref(url: string | null | undefined): string | null {
  if (!url) return null;
  return SAFE_SCHEME.test(url.trim()) ? url.trim() : null;
}

/** 一个代码块:语言标签 + 复制 + 原文。不做语法高亮(见 design/web.md §18.23)。 */
function CodeBlock({
  code,
  lang,
  onCopy,
}: {
  code: string;
  lang: string | null;
  onCopy?: (text: string) => Promise<boolean>;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="md-code">
      <div className="md-code__head">
        <span className="md-code__lang">{lang || "text"}</span>
        {onCopy ? (
          <button
            type="button"
            className="md-code__copy"
            title={copied ? "已复制" : "复制代码"}
            aria-label={copied ? "已复制" : "复制代码"}
            onClick={() => {
              void onCopy(code).then((ok) => {
                if (!ok) return;
                setCopied(true);
                window.setTimeout(() => setCopied(false), 1200);
              });
            }}
          >
            {copied ? (
              <IconCheckOutline16 size={14} />
            ) : (
              <IconCopyOutline16 size={14} />
            )}
          </button>
        ) : null}
      </div>
      <pre className="md-code__pre">
        <code>{code}</code>
      </pre>
    </div>
  );
}

/** 行内文本(可以嵌强调/链接/行内代码)。 */
function inline(nodes: readonly RootContent[], ctx: Ctx): ReactNode[] {
  return nodes.map((node, index) => node_(node, index, ctx));
}

/** 块级容器:`<p>` / `<li>` 里可能是"段落 + 列表"混着,所以两者共用同一套 children 渲染。 */
function block(nodes: readonly RootContent[], ctx: Ctx): ReactNode {
  return <>{inline(nodes, ctx)}</>;
}

/** 只有在渲染**最后一个顶层块**时才补流式光标(嵌套节点的 index 会撞上 lastIndex)。 */
function tail(ctx: Ctx, key: number, top: boolean): ReactNode {
  return top && ctx.caret === true && key === ctx.lastIndex ? (
    <span className="caret" />
  ) : null;
}

function node_(node: RootContent, key: number, ctx: Ctx, top = false): ReactNode {
  switch (node.type) {
    case "text":
      // React 会转义 —— 这是"没有 XSS 面"的落点。
      return node.value;

    case "paragraph":
      // 段落里只有行内内容,但 td/li 里可能出现块级:统一走 inline 即可(嵌套块由各自分支处理)
      return (
        <p key={key}>
          {inline(node.children, ctx)}
          {tail(ctx, key, top)}
        </p>
      );

    case "heading": {
      const Tag = `h${Math.min(6, Math.max(1, node.depth))}` as "h1";
      return (
        <Tag key={key}>
          {inline(node.children, ctx)}
          {tail(ctx, key, top)}
        </Tag>
      );
    }

    case "strong":
      return <strong key={key}>{inline(node.children, ctx)}</strong>;

    case "emphasis":
      return <em key={key}>{inline(node.children, ctx)}</em>;

    case "delete":
      return <del key={key}>{inline(node.children, ctx)}</del>;

    case "inlineCode":
      return (
        <code key={key} className="md-inline-code">
          {node.value}
        </code>
      );

    case "code":
      return (
        <CodeBlock
          key={key}
          code={node.value}
          lang={node.lang ?? null}
          {...(ctx.onCopy ? { onCopy: ctx.onCopy } : {})}
        />
      );

    case "link": {
      const href = safeHref(node.url);
      if (href === null) {
        // 不放行的协议:**退化成文字**,而不是留一个可点的东西。
        return <span key={key}>{inline(node.children, ctx)}</span>;
      }
      return (
        <a key={key} href={href} target="_blank" rel="noreferrer noopener">
          {inline(node.children, ctx)}
        </a>
      );
    }

    case "image":
    case "imageReference": {
      // 不加载远程图片(见文件头第 3 条):渲染成链接,要不要点由用户定。
      const url = "url" in node ? safeHref(node.url) : null;
      const label =
        node.alt?.trim() ||
        ("url" in node ? node.url : "") ||
        ("identifier" in node ? node.identifier : "");
      if (url === null) return <span key={key}>{label}</span>;
      return (
        <a key={key} href={url} target="_blank" rel="noreferrer noopener">
          {label || url}
        </a>
      );
    }

    case "break":
      return <br key={key} />;

    case "thematicBreak":
      return <hr key={key} />;

    case "blockquote":
      return (
        <blockquote key={key}>{block(node.children, ctx)}</blockquote>
      );

    case "list": {
      const Tag = node.ordered === true ? "ol" : "ul";
      return (
        <Tag
          key={key}
          {...(node.ordered === true && node.start !== null && node.start !== 1
            ? { start: node.start }
            : {})}
        >
          {block(node.children, ctx)}
        </Tag>
      );
    }

    case "listItem": {
      const checked = (node as { checked?: boolean | null }).checked;
      if (checked === undefined || checked === null) {
        return <li key={key}>{block(node.children, ctx)}</li>;
      }
      // 任务列表:形符 + 划线,**不做真勾选框**(它不可交互,做成交互控件是骗人)
      return (
        <li key={key} className="md-task" data-done={checked}>
          <span className="md-task__mark" aria-hidden>
            {checked ? "☑" : "☐"}
          </span>
          {block(node.children, ctx)}
        </li>
      );
    }

    case "table":
      return (
        <div className="md-table" key={key}>
          <table>
            <tbody>
              {node.children.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.children.map((cell, cellIndex) =>
                    rowIndex === 0 ? (
                      <th key={cellIndex} scope="col">
                        {inline(cell.children, ctx)}
                      </th>
                    ) : (
                      <td key={cellIndex}>{inline(cell.children, ctx)}</td>
                    ),
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );

    case "html":
      // 见文件头第 1 条:裸 HTML 只当文字显示(不解析、不执行)。
      return (
        <span className="md-html" key={key}>
          {node.value}
        </span>
      );

    case "linkReference":
      // 引用式链接的**定义**在别处;这里至少把可见文字留下(定义那行本身不显示)。
      return <span key={key}>{inline(node.children, ctx)}</span>;

    case "definition":
      return null;

    case "footnoteReference":
      return (
        <sup className="md-footnote-ref" key={key}>
          [{node.identifier}]
        </sup>
      );

    case "footnoteDefinition":
      return (
        <div className="md-footnote" key={key}>
          [{node.identifier}] {block(node.children, ctx)}
        </div>
      );

    default: {
      // 未知节点(比如将来 mdast 新增的类型):有子节点就照渲,不丢内容。
      const children = (node as { children?: RootContent[] }).children;
      if (Array.isArray(children)) return <div key={key}>{block(children, ctx)}</div>;
      const value = (node as { value?: string }).value;
      return value === undefined ? null : <span key={key}>{value}</span>;
    }
  }
}

/**
 * 渲染一段 markdown。
 *
 * 流式期间每来一个增量都会重解析一次(答案通常几 KB,本地开销可忽略);
 * dsh 那边做了"只有尾巴重解析"的增量解析器,qi 不做 —— 它的代价是长回答
 * 末期每个 token 多花一次解析,量级远小于 LLM 自己的延迟。
 */
export function Markdown({
  text,
  onCopy,
  streaming = false,
}: {
  text: string;
  onCopy?: (text: string) => Promise<boolean>;
  /** 还在流式输出:在末尾补一个光标(见 `tail`)。 */
  streaming?: boolean;
}) {
  const root = useMemo(() => parseMarkdown(text), [text]);
  const lastIndex = root.children.length - 1;
  const ctx: Ctx = {
    ...(onCopy ? { onCopy } : {}),
    ...(streaming ? { caret: true, lastIndex } : {}),
  };
  const blocks = root.children.map((node, index) => node_(node, index, ctx, true));
  // 末尾是代码块/表格这类"块"时,光标放不进行内 —— 就单独落在下面(还在写)。
  const last = root.children[lastIndex];
  const inlineTail =
    last !== undefined && (last.type === "paragraph" || last.type === "heading");
  return (
    <div className="md">
      {blocks}
      {streaming && !inlineTail ? <span className="caret" /> : null}
    </div>
  );
}
