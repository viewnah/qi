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
import { useEffect, useState } from "react";
import type { UiNode } from "../api/types";
import { itemDepth, parseBanners, type SubSegment } from "../state/segments";
import {
  IconBranchOutline16,
  IconCheckOutline16,
  IconChevronDownOutline14,
  IconCopyOutline16,
} from "./icons";
import { Markdown } from "../markdown/render";
import { processLabel } from "../state/stats";
import { latestActivityLine } from "../state/turn";
import type {
  Block,
  ErrorRow,
  NoteRow,
  ProcessRow,
  RouteRow,
  Row,
  SayRow,
  SubActivity,
  ToolRowData,
  YouRow,
} from "../state/turn";

// ── 插件 UI:声明式词汇表 + 必不准少的 JSON 兜底 ─────────────
//
// 这是"插件不用改 API 就能渲染"的落点(见 design/web.md §16)。插件把
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
            // 层级走 `depth`(label 的前导空格只是给纯文本消费方的兜底,
            // 渲染时裁掉 —— 否则 depth 缩进与空格缩进会叠成两份)
            const depth = itemDepth(it);
            return (
              <li
                key={i}
                className="ui-list__item"
                data-state={state}
                style={{ paddingInlineStart: `${(depth - 1) * 14}px` }}
              >
                {/* 状态不单靠颜色:形状本身也不同(✓ / ▸ / ○ / ✗) */}
                <span className="ui-list__mark" data-state={state}>
                  {state === "done"
                    ? "✓"
                    : state === "active"
                      ? "▸"
                      : state === "error"
                        ? "✗"
                        : "○"}
                </span>
                <span className="ui-list__label" data-state={state}>
                  {it.label.trimStart()}
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

/** 委派正文:横幅契约切出的「一个 agent 一张卡」(TUI `AgentCard` 同构)。
 *
 * 形状抄 TUI:卡头 `名字 ← 归属 · 深度 ✓/✗`,卡内三区 `·` 活动 → `✎` 思考 → 输出,
 * 层级用缩进表达、**不画树地图**(TUI 的明确取舍:委派关系在活动行与深度里已自明,
 * 地图是重复的第二份信息)。解析不到就退回原文(调用方的 `<pre>`)。 */
function Segments({ segs }: { segs: SubSegment[] }) {
  return (
    <div className="segs">
      {segs.map((s, i) => (
        <div
          key={i}
          className="seg"
          data-ok={s.ok}
          style={{ marginInlineStart: `${(s.depth - 1) * 12}px` }}
        >
          <div className="seg__head">
            <span className="seg__mark">{s.ok ? "✓" : "✗"}</span>
            <span className="seg__name">{s.name}</span>
            <span className="seg__meta">
              {s.parent ? `← ${s.parent} · ` : ""}深度 {s.depth}
            </span>
          </div>
          {s.activity.length > 0 ? (
            <div className="seg__zone">
              {s.activity.map((line, j) => (
                <div key={j} className="seg__line" data-zone="act">
                  {line}
                </div>
              ))}
            </div>
          ) : null}
          {s.thoughts.length > 0 ? (
            <div className="seg__zone">
              {s.thoughts.map((line, j) => (
                <div key={j} className="seg__line" data-zone="think">
                  {line}
                </div>
              ))}
            </div>
          ) : null}
          {s.output ? <div className="seg__out">{s.output}</div> : null}
        </div>
      ))}
    </div>
  );
}

/**
 * **委派直播**:运行中的工具卡里,每个子 run 一张小卡(名字 · 父 · 深度 · 活动)。
 *
 * 行语言与结束后的 `Segments` 同一套(`seg__*`),所以前后读起来是同一段对话;
 * 虚线边框是唯一的区别 —— 它还没定稿。结束后这张表整个消失,由 `result` 的
 * 横幅卡接管(同一批文字不显两遍,见 `ToolRowData.activity`)。
 */
/**
 * **一个子智能体一张卡** —— “谁是谁”靠**结构**区分,不靠色相。
 *
 * dsw 的呈现 token 池里只有 deepseek / red / green / amber 四个色相,其中三个是
 * 状态语义色:拿绿色标 agent,用户会读成“它成功了”。所以身份的载体是:
 *   · 首字母**徽标**(方块,一眼扫得到);
 *   · 名字**加粗、正文档位**(不再是 11px caption);
 *   · 层级靠**缩进 + 左侧深度条**(明度编码层级,不误导);
 *   · 归属写全(`← 由 X 指派 · 深度 N`);
 *   · **活着才有脉动点与更亮的边框** —— 名字一多,眼睛先找到正在跑的那个。
 *
 * 行语言与结束后的 `Segments` 同一套(`seg__line`),前后读起来是同一段对话;
 * 结束后这张表整个消失,结果交给横幅卡(同一批文字不显两遍)。
 */
function SubagentCard({ e }: { e: SubActivity }) {
  const running = e.state === "running";
  return (
    <div
      className="sub"
      data-state={e.state}
      data-depth={Math.min(Math.max(e.depth, 1), 3)}
      style={{ marginInlineStart: `${(e.depth - 1) * 14}px` }}
    >
      <div className="sub__head">
        <AgentBadge name={e.role || e.run} />
        <span className="sub__name">{e.role || e.run}</span>
        <span className="sub__meta">
          {e.parent ? `← 由 ${e.parent} 指派 · ` : ""}深度 {e.depth}
        </span>
        {running ? (
          <i className="sub__pulse" data-live="true" />
        ) : (
          <span className="sub__mark" data-ok={e.state === "done"}>
            {e.state === "done" ? "✓" : "✗"}
          </span>
        )}
      </div>
      {e.task ? <div className="sub__task">{e.task}</div> : null}
      {e.lines.length > 0 || (e.phase === "stream" && e.text) ? (
        <div className="sub__body">
          {e.lines.map((l, j) => (
            <div key={j} className="seg__line" data-zone={l.zone}>
              {l.text}
            </div>
          ))}
          {e.phase === "stream" && e.text ? (
            <div className="seg__line" data-zone="stream">
              {e.text}
              <i className="seg__caret" aria-hidden="true" />
            </div>
          ) : null}
        </div>
      ) : null}
      {e.state === "error" && e.error ? (
        <div className="sub__error">{e.error}</div>
      ) : null}
    </div>
  );
}

/** 首字母徽标 —— 身份的第一眼(不靠色相,也不靠差一点点的字号)。 */
function AgentBadge({ name }: { name: string }) {
  const ch = (name.trim()[0] ?? "?").toUpperCase();
  return (
    <span className="sub__badge" aria-hidden="true">
      {ch}
    </span>
  );
}

function ActivityPane({ entries }: { entries: SubActivity[] }) {
  return (
    <div className="subs">
      {entries.map((e) => (
        <SubagentCard key={e.run} e={e} />
      ))}
    </div>
  );
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
  // 流式中的行带 `data-live` —— 光标与提亮都靠它(CSS 见 `.process__text[data-live]`)
  return (
    <div className="process__text" data-live={row.live}>
      {row.text}
    </div>
  );
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
  const [touched, setTouched] = useState(false);
  const live = rows.some((row) =>
    row.kind === "think"
      ? row.live
      : row.kind === "tool"
        ? row.status === "running"
        : false,
  );
  // 运行中自动展开(**用户没动过才自动**):直播与流式思考必须看得见。
  // 用户关了就听用户的,之后不再强开;结束后也不自动合上 —— 刚看完的
  // 过程不能在眼前消失,要收是用户的事。
  useEffect(() => {
    if (live && !touched) setOpen(true);
  }, [live, touched]);
  return (
    <div className="process" data-open={open || undefined} data-live={live || undefined}>
      <button
        type="button"
        className="disclose process__head"
        aria-expanded={open}
        onClick={() => {
          setTouched(true);
          setOpen((v) => !v);
        }}
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
 * 回答:**无壳正文**,而且**渲染 markdown**(见 `markdown/`)。
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
      <Markdown text={row.text} onCopy={onCopy} streaming={row.live} />
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
  const [touched, setTouched] = useState(false);
  const running = row.status === "running";
  // 委派卡:一有直播就自动展开(同样只在用户没动过时)。结束后保持展开,
  // 横幅卡(Segments)直接接管视线 —— 用户刚盯着看的东西不能自己合上。
  // 普通工具(无直播)不自动展开:每次调用都弹开是闪动,不是信息。
  const autoLive = running && row.activity.length > 0;
  useEffect(() => {
    if (autoLive && !touched) setOpen(true);
  }, [autoLive, touched]);
  const args = Object.entries(row.args)
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(" ");
  // 展开时才解析:折叠行是常态,不为看不见的文本付解析代价。
  // 解析不出来 = null → 走下面的原始 `<pre>`,永远有退路。
  const segs = open && row.result ? parseBanners(row.result) : null;
  // 运行中:折叠行带上最新一行直播(委派的那几秒里,它比参数更有信息量);
  // 没直播就还是原来的空白 —— 不是委派的工具不受影响。
  const live = running ? latestActivityLine(row.activity) : null;
  return (
    <div className="tool" data-status={row.status}>
      <button
        type="button"
        className="disclose disclose--fill"
        aria-expanded={open}
        onClick={() => {
          setTouched(true);
          setOpen((v) => !v);
        }}
      >
        <Caret open={open} />
        <span className="tool-line disclose--grow">
          <i className="tool-line__dot" data-live={running} />
          <span className="tool-line__name">{row.tool}</span>
          <span className="tool-line__args">{args}</span>
          {live ? <span className="tool-line__live">{live}</span> : null}
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
          {/* 顺序 = 读的顺序:**先输入**(这次调用要干什么),再看过程与结果。
              参数以前排在卡片下面,读起来像“结果先出、原因当附注”。 */}
          {args ? (
            <pre className="evidence">{JSON.stringify(row.args, null, 2)}</pre>
          ) : null}
          {row.details ? <Details details={row.details} /> : null}
          {/* 直播只在运行中出现;结束后 result 的横幅卡接管(同一批文字不显两遍) */}
          {running && row.activity.length > 0 ? (
            <ActivityPane entries={row.activity} />
          ) : null}
          {/* 正文按横幅契约切成卡(切不动就整个退回原文,一个字不丢) */}
          {segs ? <Segments segs={segs} /> : null}
          {row.result ? (
            segs ? (
              <details className="seg-raw">
                <summary className="seg-raw__sum">原始输出</summary>
                <pre
                  className={`evidence${
                    row.status === "error" ? " evidence--error" : ""
                  }`}
                >
                  {row.result}
                </pre>
              </details>
            ) : (
              <pre
                className={`evidence${
                  row.status === "error" ? " evidence--error" : ""
                }`}
              >
                {row.result}
              </pre>
            )
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
