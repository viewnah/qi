/**
 * 左栏:会话列表。
 *
 * 相对之前的版本,这次**不做重命名/删除的内联编辑**:那两个操作在窄栏里
 * 既挤又容易误触。改成只在当前会话行上给出两个图标按钮,其余留给将来的
 * 会话树面板(qi 的会话是树,列表迟早要升级成树,现在不急着把列表做满)。
 *
 * 每条会话显示**工作目录末段**而不是完整路径:路径太长会把标题挤掉,
 * 而末段已经足够区分日常场景。
 */
import { IconPlusOutline16 } from "./icons";
import type { SessionSummary } from "../api/types";

function basename(p: string | null): string {
  if (!p) return "";
  const parts = p.split("/").filter(Boolean);
  return parts.length > 0 ? (parts[parts.length - 1] as string) : p;
}

export function Rail({
  sessions,
  current,
  busy,
  onSelect,
  onCreate,
  onOpenSettings,
  themeLabel,
  onCycleTheme,
}: {
  sessions: SessionSummary[];
  current: string | null;
  busy: boolean;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onOpenSettings: () => void;
  themeLabel: string;
  onCycleTheme: () => void;
}) {
  return (
    <>
      <div className="rail__head">
        <span className="rail__brand">qi</span>
        <button type="button" className="ghost" onClick={onCycleTheme} title="切换主题">
          {themeLabel}
        </button>
      </div>

      <div className="rail__newpad">
        <button type="button" className="rail__new" onClick={onCreate} disabled={busy}>
          <IconPlusOutline16 size={13} />
          新会话
        </button>
      </div>

      <div className="rail__section">会话 {sessions.length > 0 ? `· ${sessions.length}` : ""}</div>

      <div className="rail__list">
        {sessions.length === 0 ? (
          <div className="rail__hint">还没有会话</div>
        ) : null}
        {sessions.map((s) => (
          <button
            key={s.id}
            type="button"
            className="rail__item"
            aria-current={s.id === current}
            onClick={() => onSelect(s.id)}
          >
            <span className="rail__item-title">{s.title || "未命名"}</span>
            <span className="rail__item-meta">
              {s.running ? <span title="运行中">●</span> : null}
              <span>{basename(s.cwd)}</span>
              <span>·</span>
              <span>{s.message_count} 条</span>
            </span>
          </button>
        ))}
      </div>

      <div className="rail__foot">
        <button type="button" className="ghost" onClick={onOpenSettings}>
          设置
        </button>
      </div>
    </>
  );
}
