/**
 * 行的渲染器。
 *
 * 版式取自 dsh 的 `ui-chat/src/client/chat/`(它与 qi 的差别不在"好不好看",而在
 * **哪些东西该带壳**):
 *
 *   · **提问 = 右对齐气泡**(dsh `MessageItem.module.css` 的 `.userRow` / `.bubble`:
 *     r22、`--dsw-specific-bubble`、内衬 `10px 16px`、宽度上限约内容列的 70%);
 *   · **回答 = 无壳正文**(dsh 的 assistant markdown 就是干净的一段 markdown,
 *     没有左边框、没有"回答"标签);
 *   · **过程 = 带左缘细线的行**(思考 / 工具):qi 自己那套"行语言"只留给**证据**,
 *     于是它在视觉上比回答缩进一级 —— 与 dsh 把过程缩进在回答之下的关系一致;
 *   · **思考与回答之间一条 0.5px 分隔线**(dsh `TurnProcessNodeView` 的
 *     `border-bottom: 0.5px solid border-l2`:过程收尾、回答开始);
 *   · **每条消息下面一行图标动作**(dsh `MessageIconActions`):复制 + 分叉,悬停才出现。
 *
 * 折叠策略不变:**过程可折叠,结论不可折叠**。
 */
import { useState } from "react";
import type { UiNode } from "../api/types";
import {
  IconBranchOutline16,
  IconCheckOutline16,
  IconChevronDownOutline14,
  IconCopyOutline16,
} from "./icons";
import { processLabel } from "../state/stats";
import type {
  Block,
  ErrorRow,
  NoteRow,
  ProcessRow,
  RouteRow,
  Row,
  SayRow,
  ToolRowData,
  YouRow,
} from "../state/turn";

// ── 插件 UI:声明式词汇表 + 必不准少的 JSON 兜底 ─────────────
//
// 这是"插件不用改 API 就能渲染"的落点(见 docs/web.md §16)。插件把
// `details["ui"]` 写成词汇表里的节点,宿主负责画;宿主加新节点类型时,
// 所有已存在的插件立刻可用。
//
// **一条硬规则**:不认识的节点必须退回原始 JSON,不能丢弃。丢掉会让
// "插件发了东西但没人看见"变成不可诊断的问题。空 ui 亦然。

/** 单个词汇表节点。
 *
 * 入参声明为 `UiNode` 而不是 `unknown`:这样 `switch` 是**穷尽检查**的 ——
 * 词汇表加一个新节点类型,这里不补分支就编译不过。`default` 分支仍然保留,
 * 因为运行期随时可能收到**旧宿主不认识的**节点(插件可能比宿主新),
 * 那种情况必须退回 JSON 显示,而不是丢弃。
 */
function UiNodeView({ node }: { node: UiNode }) {
  switch (node.type) {
    case "list": {
      const items = node.items ?? [];
      return (
        <ul className="ui-list">
          {items.map((it, i) => {
            const state = it.state ?? "pending";
            return (
              <li key={i} className="ui-list__item">
                {/* 状态不单靠颜色:形状本身也不同(✓ / ▸ / ○) */}
                <span className="ui-list__mark" data-state={state}>
                  {state === "done" ? "✓" : state === "active" ? "▸" : "○"}
                </span>
                <span className="ui-list__label" data-state={state}>
                  {it.label}
                </span>
                {it.note ? (
                  <span className="ui-list__note">{it.note}</span>
                ) : null}
              </li>
            );
          })}
        </ul>
      );
    }
    case "kv":
      return (
        <dl className="ui-kv">
          {(node.rows ?? []).map(([k, v], i) => (
            <div key={i} style={{ display: "contents" }}>
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      );
    case "progress": {
      const pct =
        node.max > 0 ? Math.min(100, (node.value / node.max) * 100) : 0;
      return (
        <div className="ui-progress">
          <div className="ui-progress__bar">
            <div className="ui-progress__fill" style={{ width: `${pct}%` }} />
          </div>
          <div className="ui-progress__note">
            {node.label ? `${node.label} · ` : ""}
            {node.value} / {node.max}
          </div>
        </div>
      );
    }
    case "code":
      return (
        <div className="ui-code">
          {node.lang ? <div className="ui-code__lang">{node.lang}</div> : null}
          <pre className="evidence">{node.text}</pre>
        </div>
      );
    case "note":
      return <div className="ui-note">{node.text}</div>;
    default:
      // 运行期可能收到比宿主新的节点类型。原样显示 JSON。**不丢**。
      return <pre className="evidence">{JSON.stringify(node, null, 2)}</pre>;
  }
}

/** 工具结构化结果:认得的走词汇表,不认得的(或没有的)退回原始 JSON。 */
function Details({ details }: { details: Record<string, unknown> | null }) {
  if (details === null) return null;
  if (details._truncated === true) {
    return (
      <div className="ui-note">
        结构化结果太大({String(details._full_chars ?? "?")} 字符),落盘时已省略。
      </div>
    );
  }
  const ui = details.ui;
  if (Array.isArray(ui)) {
    return (
      <div className="ui">
        {ui.map((node, i) => (
          <UiNodeView key={i} node={node as UiNode} />
        ))}
      </div>
    );
  }
  return <pre className="evidence">{JSON.stringify(details, null, 2)}</pre>;
}

/** 无壳的行容器:提问 / 回答 / 分派 / 标记 / 错误。 */
function Plain({
  kind,
  attrs,
  children,
}: {
  kind: string;
  attrs?: Record<string, string>;
  children: React.ReactNode;
}) {
  return (
    <div className={`plain plain--${kind}`} {...attrs}>
      {children}
    </div>
  );
}

function Caret({ open }: { open: boolean }) {
  return (
    <IconChevronDownOutline14
      className="disclose__caret"
      size={11}
      data-open={open}
    />
  );
}

// ── 每条消息下面那行图标动作(dsh `MessageIconActions`)──────

/**
 * 复制 + 分叉。
 *
 * 两条口径直接照 dsh:
 *   · **只给已落盘的消息挂动作**:分叉要一个 entry id,而直播中刚发出去的那条还没有
 *     (`YouRow.entryId` 的注释);dsh 同样是"没有被持久化的消息不带 per-message 动作";
 *   · **悬停才显形**(触摸设备常显):老消息不该一直挂着一排按钮抢视线。
 *     `always` 给最后一条用 —— 最新一条的动作要能直接看见。
 */
function MessageActions({
  text,
  entryId,
  disabled,
  align,
  always,
  onCopy,
  onFork,
}: {
  text: string;
  entryId?: string;
  disabled?: boolean;
  align: "start" | "end";
  always: boolean;
  /** 返回**是否真的复制成功**:成功才把图标换成勾,失败由调用方甩 note。 */
  onCopy: (text: string) => Promise<boolean>;
  onFork: (entryId: string) => void;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="msg-actions" data-align={align} data-always={always}>
      <button
        type="button"
        className="msg-action"
        title={copied ? "已复制" : "复制"}
        aria-label={copied ? "已复制" : "复制这条消息"}
        onClick={() => {
          // 不乐观上报成功:剪贴板会被权限/焦点拒掉,那时变成勾就是撒谎。
          void onCopy(text).then((ok) => {
            if (!ok) return;
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1200);
          });
        }}
      >
        {copied ? <IconCheckOutline16 size={14} /> : <IconCopyOutline16 size={14} />}
      </button>
      {entryId === undefined ? null : (
        <button
          type="button"
          className="msg-action"
          disabled={disabled === true}
          title="从这里分叉:复制到这条为止的分支成新会话"
          aria-label="从这条消息分叉"
          onClick={() => onFork(entryId)}
        >
          <IconBranchOutline16 size={14} />
        </button>
      )}
    </div>
  );
}

// ── 你的输入 ──────────────────────────────────────────────

/**
 * 提问:**右对齐气泡**,没有"你"这个称谓。
 *
 * 为什么去掉称谓:气泡的位置(靠右)与形状(实底 r22)已经把"这是谁说的"说完了,
 * 再写一个"你"是把同一件事说两遍 —— dsh 那边也只有气泡,没有人称标签。
 */
function You({
  row,
  sessionId,
  always,
  onCopy,
  onFork,
}: {
  row: YouRow;
  sessionId: string | null;
  always: boolean;
  onCopy: (text: string) => Promise<boolean>;
  onFork: (entryId: string) => void;
}) {
  return (
    <Plain kind="you">
      <div className="bubble">{row.text}</div>
      <MessageActions
        text={row.text}
        entryId={row.entryId}
        disabled={sessionId === null}
        align="end"
        always={always}
        onCopy={onCopy}
        onFork={onFork}
      />
    </Plain>
  );
}

// ── 分派 ──────────────────────────────────────────────────

/**
 * 分派行。分派器是 qi 相对普通 chat 的第一个差异点,所以它必须出现在转录里 ——
 * 但**不能抢视线**,所以只用一行,细节(理由)收起。
 *
 * 位置:紧跟在它回答的那个提问之后(行顺序由 `fromEntries` 保证),因此它读起来是
 * "接下来这个回答是谁给的",而不是"先选了个人、然后你才提问"。
 */
function Route({ row }: { row: RouteRow }) {
  const [open, setOpen] = useState(false);
  const pct = `${Math.round(row.confidence * 100)}%`;
  return (
    <Plain kind="route">
      <button
        type="button"
        className="disclose route__head"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <Caret open={open} />
        <b>分派</b>
        <span>
          {row.label} · {row.source} · {pct}
        </span>
      </button>
      {open && row.reasoning ? (
        <div className="route__why">{row.reasoning}</div>
      ) : null}
    </Plain>
  );
}

// ── 过程块:一轮的思考 + 工具 + 叙述 ──────────────────────

/**
 * 块内的行。**不区分思考与过程**:两者都是"过程里的文字",同一种渲染
 * (使用反馈:「不用区分思考和过程」)。工具保留自己的一行折叠 —— 它要能单独展开看证据。
 */
function ProcessRowView({ row }: { row: ProcessRow }) {
  if (row.kind === "tool") return <Tool row={row} />;
  if (!row.text) return null;
  return <div className="process__text">{row.text}</div>;
}

/**
 * **一个回合的所有过程收成一个可折叠的块**。
 *
 * 头部照 dsh `TurnProcessNodeView`:33px 一行、下沿 0.5px 发丝线(过程收尾、回答开始)、
 * 收起时带 8px 下边距;正文整体缩进一级(与 dsh 把过程缩进在回答之下的关系一致)。
 * 默认**收起** —— 与 dsh 一样,过程平时不该占视线。
 */
function Process({ rows }: { rows: ProcessRow[] }) {
  const [open, setOpen] = useState(false);
  const live = rows.some((row) =>
    row.kind === "think"
      ? row.live
      : row.kind === "tool"
        ? row.status === "running"
        : false,
  );
  return (
    <div className="process" data-open={open || undefined} data-live={live || undefined}>
      <button
        type="button"
        className="disclose process__head"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <Caret open={open} />
        <span className="process__label">{processLabel(rows)}</span>
      </button>
      {open ? (
        <div className="process__body">
          {rows.map((row) => (
            <ProcessRowView key={row.key} row={row} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

// ── 助手文本 ──────────────────────────────────────────────

/**
 * 回答:**无壳正文**(dsh 的 assistant 就是一段干净 markdown)。
 *
 * 原来的"回答 · <agent>"眉条去掉了两件事:左缘细线(反馈:「把回答左边的竖线去掉」)
 * 和人称式标签 —— agent 名字在紧跟其上的分派行里已经有了,不必说两遍。
 *
 * 只有 `final` 与 `opening` 走这里:`narration`(调工具前的过程陈述)**进过程块**
 * (见 `groupProcess`),它不该以"和回答同级"的形态出现。
 */
function Say({
  row,
  sessionId,
  always,
  onCopy,
  onFork,
}: {
  row: SayRow;
  sessionId: string | null;
  always: boolean;
  onCopy: (text: string) => Promise<boolean>;
  onFork: (entryId: string) => void;
}) {
  return (
    <Plain kind="say" attrs={{ "data-tone": row.tone }}>
      <div className="say__text">
        {row.text}
        {row.live ? <span className="caret" /> : null}
      </div>
      {row.live ? null : (
        <MessageActions
          text={row.text}
          entryId={row.entryId}
          disabled={sessionId === null}
          align="start"
          always={always}
          onCopy={onCopy}
          onFork={onFork}
        />
      )}
    </Plain>
  );
}

// ── 工具 ──────────────────────────────────────────────────

function cost(row: ToolRowData): string {
  const parts: string[] = [];
  if (row.durationMs !== null) parts.push(`${row.durationMs}ms`);
  if (row.exitCode !== null) parts.push(`exit ${row.exitCode}`);
  return parts.join(" · ");
}

/**
 * 工具行:折叠态**恰好一行** —— 点 · 名字 · 参数 · 耗时 · 形符,扫读只看这一行。
 *
 * 注意这里的类名是 `tool-line__*`:CSS 里按它们上样式(`.tool-line__dot` 的
 * 状态色/脉动、`__mark` 的 ✓/✗)。**不要改名** —— 改名的结果就是控件静默失去
 * 全部样式与状态形符(上一次重排时我把它写成 `tool-dot`/`tool-args`,CSS 一条都
 * 没命中,而且把状态形符整个丢了)。
 *
 * 状态不单靠颜色:运行中是脉动的点,结束必须带形符(设计清单第 9 条)。
 */
function Tool({ row }: { row: ToolRowData }) {
  const [open, setOpen] = useState(false);
  const running = row.status === "running";
  const args = Object.entries(row.args)
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(" ");
  return (
    <div className="tool" data-status={row.status}>
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
            <span className="tool-line__mark" data-ok={row.status === "ok"}>
              {row.status === "ok" ? "✓" : "✗"}
            </span>
          )}
        </span>
      </button>
      {open ? (
        <div className="tool__body">
          {/* 插件给的结构化详情排在最前:它是给人看的,result 是给模型的 */}
          {row.details ? <Details details={row.details} /> : null}
          {args ? (
            <pre className="evidence">{JSON.stringify(row.args, null, 2)}</pre>
          ) : null}
          {row.result ? (
            <pre
              className={`evidence${row.status === "error" ? " evidence--error" : ""}`}
            >
              {row.result}
            </pre>
          ) : null}
          {row.error ? (
            <div className="process__text">错误原因:{row.error}</div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

// ── 标记 / 错误 ───────────────────────────────────────────

function Note({ row }: { row: NoteRow }) {
  return (
    <Plain kind="note">
      <b className="note__tag">{row.label}</b>
      <span className="note__rule" />
      {row.detail ? (
        <span className="note__tail">{row.detail.slice(0, 60)}</span>
      ) : null}
    </Plain>
  );
}

function Failure({ row }: { row: ErrorRow }) {
  return (
    <Plain kind="error">
      <b className="plain__tag">错误</b>
      <div className="say__text">{row.text}</div>
    </Plain>
  );
}

// ── 分发 ──────────────────────────────────────────────────

export interface RowViewProps {
  row: Row;
  /** 当前会话(分叉要用)。null = 新会话还没建好,分叉按钮置灰。 */
  sessionId: string | null;
  /** 这一行的动作是否**常显**(最后一条用;其余悬停才出现)。 */
  revealActions: boolean;
  onCopy: (text: string) => Promise<boolean>;
  onFork: (entryId: string) => void;
}

export function RowView({
  row,
  sessionId,
  revealActions,
  onCopy,
  onFork,
}: RowViewProps) {
  switch (row.kind) {
    // 过程类的三种行在**块**里渲染(见 BlockView / groupProcess)。
    // 单独喂进来时(理论上不会)就当成只含这一行的块,行为与块一致。
    case "think":
    case "tool":
      return <Process rows={[row]} />;
    case "you":
      return (
        <You
          row={row}
          sessionId={sessionId}
          always={revealActions}
          onCopy={onCopy}
          onFork={onFork}
        />
      );
    case "route":
      return <Route row={row} />;
    case "say":
      return row.tone === "narration" ? (
        <Process rows={[row]} />
      ) : (
        <Say
          row={row}
          sessionId={sessionId}
          always={revealActions}
          onCopy={onCopy}
          onFork={onFork}
        />
      );
    case "note":
      return <Note row={row} />;
    case "error":
      return <Failure row={row} />;
  }
}

/**
 * 按**块**渲染:`groupProcess` 折出来的每一块要么是单行,要么是一个过程块。
 *
 * 转录的入口是它(不是 `RowView`)—— 这样"哪些行收进过程块"这件事只有
 * `state/turn.ts` 一处真相,渲染层只负责画。
 */
export function BlockView({
  block,
  sessionId,
  revealActions,
  onCopy,
  onFork,
}: {
  block: Block;
  sessionId: string | null;
  revealActions: boolean;
  onCopy: (text: string) => Promise<boolean>;
  onFork: (entryId: string) => void;
}) {
  if (block.kind === "process") return <Process rows={block.rows} />;
  return (
    <RowView
      row={block.row}
      sessionId={sessionId}
      revealActions={revealActions}
      onCopy={onCopy}
      onFork={onFork}
    />
  );
}
