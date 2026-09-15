/**
 * 行的渲染器。
 *
 * 每行统一是 `<行> = <左缘细线> + <行体>`;细线的颜色由行种类决定(用 `currentColor`
 * 传下去),行体里第一层是"行眉"(种类 + 元信息,等宽极小),第二层才是内容。
 * 这样扫读时眼睛只需要跟一列细线和一列极小标签,不会被大段文字打断。
 *
 * 折叠策略只有一条:**过程可折叠,结论不可折叠**。思考和工具正文默认收起
 * (它们是证据,不是答案),结论永远展开。
 */
import { useState } from "react";
import { IconChevronDownOutline14 } from "./icons";
import type {
  ErrorRow,
  NoteRow,
  RouteRow,
  Row,
  SayRow,
  ThinkRow,
  ToolRowData,
  YouRow,
} from "../state/turn";

/** 行外壳:细线 + 行体。 */
function RowShell({
  kind,
  attrs,
  eyebrow,
  children,
}: {
  kind: string;
  attrs?: Record<string, string>;
  eyebrow?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className={`row row--${kind}`} {...attrs}>
      <div className="row__rail" />
      <div className="row__body">
        {eyebrow ? <div className="row__eyebrow">{eyebrow}</div> : null}
        {children}
      </div>
    </div>
  );
}

function Caret({ open }: { open: boolean }) {
  return <IconChevronDownOutline14 className="disclose__caret" size={11} data-open={open} />;
}

// ── 你的输入 ──────────────────────────────────────────────

function You({ row }: { row: YouRow }) {
  return (
    <RowShell kind="you" eyebrow={<b>你</b>}>
      <div className="row__text">{row.text}</div>
    </RowShell>
  );
}

// ── 分派 ──────────────────────────────────────────────────

/**
 * 分派行。分派器是 qi 相对普通 chat 的第一个差异点,所以它必须出现在转录里 ——
 * 但**不能抢视线**,所以只用一行,细节(理由)收起。
 */
function Route({ row }: { row: RouteRow }) {
  const [open, setOpen] = useState(false);
  const pct = `${Math.round(row.confidence * 100)}%`;
  return (
    <RowShell
      kind="route"
      eyebrow={
        <>
          <button
            type="button"
            className="disclose"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            <Caret open={open} />
            <b>分派</b>
          </button>
          <span>
            {row.label} · {row.source} · {pct}
          </span>
        </>
      }
    >
      {open && row.reasoning ? <div className="row__text">{row.reasoning}</div> : null}
    </RowShell>
  );
}

// ── 思考 ──────────────────────────────────────────────────

/** 思考默认收起:它是过程,读者多数时候不需要。 */
function Think({ row }: { row: ThinkRow }) {
  const [open, setOpen] = useState(false);
  return (
    <RowShell
      kind="think"
      eyebrow={
        <button
          type="button"
          className="disclose"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          <Caret open={open} />
          <b>思考</b>
          <span>{row.text.length} 字</span>
        </button>
      }
    >
      {open ? <pre className="evidence">{row.text}</pre> : null}
    </RowShell>
  );
}

// ── 助手文本 ──────────────────────────────────────────────

const TONE_LABEL: Record<SayRow["tone"], string> = {
  final: "回答",
  narration: "过程",
  opening: "开场",
};

function Say({ row }: { row: SayRow }) {
  return (
    <RowShell
      kind="say"
      attrs={{ "data-tone": row.tone }}
      eyebrow={
        <>
          <b>{TONE_LABEL[row.tone]}</b>
          {row.agent ? <span>{row.agent}</span> : null}
        </>
      }
    >
      <div className="row__text">
        {row.text}
        {row.live ? <span className="caret" /> : null}
      </div>
    </RowShell>
  );
}

// ── 工具 ──────────────────────────────────────────────────

function cost(row: ToolRowData): string {
  const parts: string[] = [];
  if (row.durationMs !== null) parts.push(`${row.durationMs}ms`);
  if (row.exitCode !== null) parts.push(`exit ${row.exitCode}`);
  return parts.join(" · ");
}

/** 折叠态**恰好一行**:点 · 名字 · 参数 · 耗时 · 形符。扫读只看这一行。 */
function Tool({ row }: { row: ToolRowData }) {
  const [open, setOpen] = useState(false);
  const running = row.status === "running";
  const args = Object.entries(row.args)
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(" ");
  return (
    <RowShell
      kind="tool"
      attrs={{ "data-status": row.status }}
      eyebrow={
        <button
          type="button"
          className="disclose disclose--fill"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          <Caret open={open} />
          <span className="tool-line disclose--grow">
            <i className="tool-line__dot" data-live={running} />
            <span className="tool-line__name">{row.tool}</span>
            <span className="tool-line__args">{args}</span>
            <span className="tool-line__cost">{cost(row)}</span>
            {running ? null : (
              // 状态不单靠颜色:必须带形符(设计清单第 9 条)
              <span className="tool-line__mark" data-ok={row.status === "ok"}>
                {row.status === "ok" ? "✓" : "✗"}
              </span>
            )}
          </span>
        </button>
      }
    >
      {open ? (
        <>
          {args ? <pre className="evidence">{JSON.stringify(row.args, null, 2)}</pre> : null}
          {row.result ? (
            <pre className={`evidence${row.status === "error" ? " evidence--error" : ""}`}>
              {row.result}
            </pre>
          ) : null}
          {row.error ? <div className="row__text">错误原因:{row.error}</div> : null}
        </>
      ) : null}
    </RowShell>
  );
}

// ── 标记 / 错误 ───────────────────────────────────────────

function Note({ row }: { row: NoteRow }) {
  return (
    <RowShell kind="note">
      <b className="note__tag">{row.label}</b>
      <span className="row__rule" />
      {row.detail ? <span className="note__tail">{row.detail.slice(0, 60)}</span> : null}
    </RowShell>
  );
}

function Failure({ row }: { row: ErrorRow }) {
  return (
    <RowShell kind="error" eyebrow={<b>错误</b>}>
      <div className="row__text">{row.text}</div>
    </RowShell>
  );
}

// ── 分发 ──────────────────────────────────────────────────

export function RowView({ row }: { row: Row }) {
  switch (row.kind) {
    case "you":
      return <You row={row} />;
    case "route":
      return <Route row={row} />;
    case "think":
      return <Think row={row} />;
    case "say":
      return <Say row={row} />;
    case "tool":
      return <Tool row={row} />;
    case "note":
      return <Note row={row} />;
    case "error":
      return <Failure row={row} />;
  }
}
