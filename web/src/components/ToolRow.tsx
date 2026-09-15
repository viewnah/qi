/**
 * 工具行:折叠态**恰好一行**(设计清单第 6 条),点击才展开正文。
 *
 * 颜色只用于"类别点"与成功/失败形符,并且**不单靠颜色**表达状态:
 * 成功带 ✓、失败带 ✗、运行中有呼吸点(docs/web.md §11 第 8/9 条)。
 */
import { useState } from "react";
import type { ToolView } from "../state/turn";

/** 工具 → 类别。点色用 `--dsw-*` 令牌(见 app.css `.tool__dot--*`)。未列出的插件工具用中性点。 */
const TOOL_KIND: Record<string, "read" | "write" | "exec" | "web" | "task"> = {
  read: "read",
  ls: "read",
  find: "read",
  grep: "read",
  write: "write",
  edit: "write",
  bash: "exec",
};

function summarizeArgs(args: Record<string, unknown>): string {
  const parts: string[] = [];
  let length = 0;
  for (const [key, value] of Object.entries(args)) {
    const text = typeof value === "string" ? value : JSON.stringify(value);
    const piece = `${key}=${text}`;
    parts.push(piece);
    length += piece.length + 3; // " · " 三个字符
    if (length > 96) break; // 提前停:不再把已拼好的串反复 join 一遍
  }
  const joined = parts.join(" · ");
  return joined.length > 110 ? `${joined.slice(0, 110)}…` : joined;
}

export function ToolRow({ tool }: { tool: ToolView }) {
  const [open, setOpen] = useState(false);
  const kind = TOOL_KIND[tool.tool];
  const running = tool.status === "running";
  const dotClass = [
    "tool__dot",
    kind ? `tool__dot--${kind}` : "",
    running ? "tool__dot--running" : "",
  ]
    .filter(Boolean)
    .join(" ");
  const mark = running ? "" : tool.status === "ok" ? "✓" : "✗";
  const cost = [
    typeof tool.durationMs === "number" ? `${tool.durationMs}ms` : "",
    typeof tool.exitCode === "number" && tool.exitCode !== 0
      ? `exit=${tool.exitCode}`
      : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className="tool">
      <button
        type="button"
        className="tool__head"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        title={open ? "收起输出" : "展开输出"}
      >
        <span className={dotClass} aria-hidden="true" />
        <span className="tool__name">{tool.tool}</span>
        <span className="tool__args" title={JSON.stringify(tool.args)}>
          ({summarizeArgs(tool.args)})
        </span>
        {cost ? <span className="tool__cost">{cost}</span> : null}
        {mark ? (
          <span
            className={`tool__cost tool__mark--${tool.status === "ok" ? "ok" : "error"}`}
            aria-label={tool.status === "ok" ? "成功" : "失败"}
          >
            {mark}
          </span>
        ) : null}
      </button>
      {open ? (
        <pre
          className={`tool__body ${tool.status === "error" ? "tool__body--error" : ""}`}
        >
          {tool.result || (running ? "运行中…" : "(无输出)")}
        </pre>
      ) : null}
    </div>
  );
}
