/**
 * 右栏:**遥测**。这是这次重设计里最像"设计判断"的一块。
 *
 * 聊天界面普遍不告诉你两件事:它**为什么**这么做,以及**还剩多少**上下文。
 * 对 harness 来说这两件事是一等公民 —— 分派理由决定了你要不要换 agent,
 * 上下文占用决定了你要不要开新会话或压缩。
 *
 * 内容三块,都是**读转录算出来的**(不新增后端接口):
 *   1. 本轮分派链:每次 dispatch 的 agent / 来源 / 置信度 / 理由
 *   2. 上下文占用:usage 与模型窗口的比值(窗口由 /api/config 给)
 *   3. 动作计数:工具调用数、失败数、思考字数
 */
import type { Row, TurnState } from "../state/turn";

function countTools(rows: Row[]) {
  let ok = 0;
  let bad = 0;
  for (const r of rows) {
    if (r.kind === "tool") {
      if (r.status === "ok") ok += 1;
      else if (r.status === "error") bad += 1;
    }
  }
  return { ok, bad, total: ok + bad };
}

function thinkingChars(rows: Row[]): number {
  let n = 0;
  for (const r of rows) if (r.kind === "think") n += r.text.length;
  return n;
}

export function Telemetry({
  turn,
  model,
  contextWindow,
}: {
  turn: TurnState;
  model: string | null;
  /** 模型上下文窗口(tokens,来自 `/api/config` 的默认模型元数据)。0/取不到就不画条。 */
  contextWindow: number;
}) {
  const routes = turn.rows.filter((r) => r.kind === "route").slice(-6);
  const tools = countTools(turn.rows);
  const total =
    typeof turn.usage.total_tokens === "number"
      ? turn.usage.total_tokens
      : null;
  /**
   * 占用看的是 **`context_tokens`**(最后一次 LLM 调用看到的 prompt 大小),
   * 而不是 `total_tokens` —— 后者是多步**相加**的结果,一步就能超过窗口。
   * 旧宿主不给这个键时回落到 total(那是以前的行为,至少不会空屏)。
   */
  const used =
    typeof turn.usage.context_tokens === "number" && turn.usage.context_tokens > 0
      ? turn.usage.context_tokens
      : total;
  const ratio =
    used !== null && contextWindow > 0 ? Math.min(1, used / contextWindow) : null;

  return (
    <div className="tele__scroll">
      {/* 上下文占用 —— chat 界面最缺的那个信息 */}
      <div className="tele__group">
        <div className="tele__label">上下文</div>
        {ratio === null ? (
          <div className="tele__empty">
            {used === null ? "本轮还没有用量" : "模型窗口未知,只报用量"}
          </div>
        ) : (
          <div className="meter">
            <div className="meter__bar">
              <div
                className="meter__fill"
                data-warn={ratio > 0.75}
                style={{ width: `${ratio * 100}%` }}
              />
            </div>
            <div className="meter__note">
              {used} / {contextWindow} tokens · {(ratio * 100).toFixed(1)}%
            </div>
          </div>
        )}
        {used !== null && ratio === null ? (
          <div className="meter__note">{used} tokens</div>
        ) : null}
      </div>

      {/* 本轮分派链 */}
      <div className="tele__group">
        <div className="tele__label">分派</div>
        {routes.length === 0 ? (
          <div className="tele__empty">本轮还没有分派决策</div>
        ) : (
          routes.map((r) =>
            r.kind === "route" ? (
              <div key={r.key} className="route-card">
                <div className="route-card__head">
                  <span className="route-card__name">{r.label}</span>
                  <span className="route-card__conf">
                    {r.source} {Math.round(r.confidence * 100)}%
                  </span>
                </div>
                {r.reasoning ? (
                  <div className="route-card__why">{r.reasoning}</div>
                ) : null}
              </div>
            ) : null,
          )
        )}
      </div>

      {/* 动作 */}
      <div className="tele__group">
        <div className="tele__label">动作</div>
        <dl className="tele__kv">
          <dt>工具调用</dt>
          <dd>{tools.total}</dd>
          <dt>失败</dt>
          <dd>{tools.bad}</dd>
          <dt>思考</dt>
          <dd>{thinkingChars(turn.rows)} 字</dd>
          <dt>LLM 调用</dt>
          <dd>
            {typeof turn.usage.llm_calls === "number"
              ? turn.usage.llm_calls
              : "—"}
          </dd>
        </dl>
      </div>

      {/* 环境 */}
      <div className="tele__group">
        <div className="tele__label">环境</div>
        <dl className="tele__kv">
          <dt>模型</dt>
          <dd title={model ?? ""}>{model ?? "未配置"}</dd>
          <dt>工作目录</dt>
          <dd title={turn.host.cwd ?? ""}>{turn.host.cwd ?? "—"}</dd>
        </dl>
      </div>
    </div>
  );
}
