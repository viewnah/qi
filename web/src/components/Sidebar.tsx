/**
 * 会话栏:按 **cwd** 分组(会话头里记着工作目录,所以历史会话也能归位)。
 * 运行中的会话用一个点标出 —— 不引入未读红点之类的焦虑信号。
 */
import { useState } from "react";
import type { SessionSummary } from "../api/types";

function groupOf(session: SessionSummary): string {
  if (!session.cwd) return "(未知项目)";
  const parts = session.cwd.split("/").filter(Boolean);
  return parts.length <= 2 ? session.cwd : `…/${parts.slice(-2).join("/")}`;
}

export function Sidebar({
  sessions,
  current,
  busy,
  onSelect,
  onCreate,
  onRename,
  onDelete,
  onOpenSettings,
}: {
  sessions: SessionSummary[];
  current: string | null;
  busy: boolean;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  onOpenSettings: () => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  const groups = new Map<string, SessionSummary[]>();
  for (const session of sessions) {
    const key = groupOf(session);
    const bucket = groups.get(key);
    if (bucket) bucket.push(session);
    else groups.set(key, [session]);
  }

  const commit = (id: string) => {
    const title = draft.trim();
    setEditing(null);
    if (title) onRename(id, title);
  };

  return (
    <aside className="sidebar">
      {/* dsh 的 logoRow:高 60px,品牌在左、图标钮在右 */}
      <div className="sidebar__head">
        <span className="brand">qi</span>
        <button
          type="button"
          className="sidebar__icon"
          title="设置"
          aria-label="设置"
          onClick={onOpenSettings}
        >
          ⚙
        </button>
      </div>
      {/* dsh 把新会话做成独立一行(38px 高、半径 12、0.5px 边) */}
      <button
        type="button"
        className="sidebar__new"
        onClick={onCreate}
        disabled={busy}
        title="新建会话"
      >
        ＋ 新会话
      </button>
      <div className="sidebar__list">
        {sessions.length === 0 ? (
          <div className="sidebar__group">还没有会话</div>
        ) : (
          [...groups.entries()].map(([group, rows]) => (
            <div key={group}>
              <div className="sidebar__group" title={group}>
                {group}
              </div>
              {rows.map((session) => (
                <div
                  key={session.id}
                  className="session"
                  aria-current={session.id === current}
                >
                  {editing === session.id ? (
                    <input
                      value={draft}
                      autoFocus
                      aria-label="会话名"
                      onChange={(event) => setDraft(event.target.value)}
                      onBlur={() => commit(session.id)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") commit(session.id);
                        if (event.key === "Escape") setEditing(null);
                      }}
                    />
                  ) : (
                    <button
                      type="button"
                      className="session"
                      style={{ padding: 0 }}
                      onClick={() => onSelect(session.id)}
                      title={session.title || session.id}
                    >
                      <span
                        className={`session__dot ${session.running ? "session__dot--running" : ""}`}
                        aria-hidden="true"
                      />
                      <span className="session__title">
                        {session.title || "(无标题)"}
                      </span>
                      <span className="session__meta">
                        {session.message_count}
                      </span>
                    </button>
                  )}
                  <span className="session__actions">
                    <button
                      type="button"
                      className="sidebar__icon"
                      title="改名"
                      onClick={() => {
                        setEditing(session.id);
                        setDraft(session.title);
                      }}
                    >
                      ✎
                    </button>
                    <button
                      type="button"
                      className="sidebar__icon"
                      title={session.running ? "运行中不能删除" : "删除"}
                      disabled={session.running}
                      onClick={() => onDelete(session.id)}
                    >
                      ✕
                    </button>
                  </span>
                </div>
              ))}
            </div>
          ))
        )}
      </div>
    </aside>
  );
}
