/**
 * 左栏:**品牌行 · 新会话 · 搜索 · 会话列表 · 底部设置**。
 *
 * ## 固定工作目录后没有项目层级
 *
 * qi-lab 的工作目录由启动参数固定(`qi lab --cwd <dir>`),界面不再提供切换/添加
 * 工作区的入口。所以左栏是**一层会话列表**,不再有「项目 → 会话」的二级目录。
 *
 * ## 几何
 *
 * 行高、内衬、选中底仍照 dsh 的会话行(`.sessionRow`,实测 x 12–266、高 32)。
 * 没有一级项目行之后,列表直接用会话行,标签与搜索仍由 `SidebarSection` 管。
 *
 * 行尾的「…」菜单由 `RowMenu` 渲染(portal + 视口翻转),动作项在那里,不在这里拼 DOM。
 */
import { useMemo, useState } from "react";
import type { RefObject } from "react";
import {
  IconNewChatOutline16,
  IconPanelLeftOutline16,
  IconSettingsOutline16,
} from "./icons";
import { RowMenu } from "./RowMenu";
import { SidebarSection } from "./SidebarSection";
import {
  IconBranchOutline16,
  IconEditOutline16,
  IconTrashOutline16,
} from "./icons";
import { relativeLabel, stampOf } from "../state/projects";
import type { SessionSummary } from "../api/types";

export function Rail({
  sessions,
  current,
  busy,
  now,
  onSelect,
  onCreate,
  onRenameSession,
  onForkSession,
  onDeleteSession,
  onOpenSettings,
  settingsOpen = false,
  settingsTriggerRef,
  railCollapsed,
  onToggleCollapse,
  onExpandRail,
}: {
  /** 固定工作目录下的会话(后端已过滤,顺序 = mtime 倒序)。 */
  sessions: SessionSummary[];
  current: string | null;
  busy: boolean;
  /** 当前 epoch 毫秒(行尾相对时间由它算出;由 App 定时推进)。 */
  now: number;
  onSelect: (id: string) => void;
  /** 新建会话。工作目录固定,不需要参数。 */
  onCreate: () => void;
  /** 会话行「…」里的三件事(重命名 / 分叉 / 删除),由 App 弹层确认。 */
  onRenameSession: (session: SessionSummary) => void;
  onForkSession: (id: string) => void;
  onDeleteSession: (session: SessionSummary) => void;
  onOpenSettings: () => void;
  /** 设置浮层当前是否开着(只为了 `aria-expanded`,交互不走它)。 */
  settingsOpen?: boolean;
  /** 「设置」行本身的 ref:浮层关掉时把焦点**还给**它(见 Settings 的 `returnFocusTo`)。 */
  settingsTriggerRef?: RefObject<HTMLButtonElement | null>;
  /** 左栏是否折叠成 56px 竖条(见 app.css 的 `data-rail="collapsed"`)。 */
  railCollapsed: boolean;
  onToggleCollapse: () => void;
  /** **幂等的**"展开左栏"。折叠态点放大镜时会用到。 */
  onExpandRail: () => void;
}) {
  /** 搜索串(搜索框自己管展开与焦点,见 SidebarSection)。 */
  const [query, setQuery] = useState("");
  const filtered = query.trim().length > 0;

  /** 左栏只搜标题。会话内容要读文件,左栏要的是"我那条会话跑哪去了"。 */
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return sessions;
    return sessions.filter((session) =>
      (session.title || "").toLowerCase().includes(needle),
    );
  }, [sessions, query]);

  return (
    <>
      {/* 品牌行:纯文字字标「Qi Web」—— 不放 mark、不放图标。 */}
      <div className="rail__brandrow">
        <span className="rail__wordmark">Qi Web</span>
        <button
          type="button"
          className="rail__collapse"
          aria-expanded={!railCollapsed}
          aria-label={railCollapsed ? "展开侧栏" : "收起侧栏"}
          title={railCollapsed ? "展开侧栏" : "收起侧栏"}
          onClick={onToggleCollapse}
        >
          <IconPanelLeftOutline16 size={16} />
        </button>
      </div>

      <button
        type="button"
        className="rail__new"
        onClick={onCreate}
        disabled={busy}
      >
        <IconNewChatOutline16 size={14} />
        <span className="rail__new-label">新会话</span>
      </button>

      <SidebarSection
        query={query}
        onQueryChange={setQuery}
        collapsed={railCollapsed}
        onExpandRail={onExpandRail}
      />

      <div className="rail__list" role="tree" aria-label="会话">
        {sessions.length === 0 ? (
          <div className="rail__hint">还没有会话</div>
        ) : null}
        {filtered && visible.length === 0 ? (
          <div className="rail__hint">没有匹配「{query.trim()}」的会话</div>
        ) : null}

        {visible.map((session) => (
          <div
            className="rail__session"
            key={session.id}
            role="treeitem"
            aria-selected={session.id === current}
            tabIndex={0}
            onClick={() => onSelect(session.id)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onSelect(session.id);
              }
            }}
          >
            <span className="rail__slot">
              {session.running ? (
                <i className="rail__dot" title="运行中" />
              ) : null}
            </span>
            <span className="rail__title">{session.title || "未命名"}</span>
            <span className="rail__time">
              {relativeLabel(stampOf(session), now)}
            </span>
            <span className="rail__actions">
              <RowMenu
                subject={`会话「${session.title || "未命名"}」`}
                items={[
                  {
                    id: "rename",
                    label: "重命名",
                    icon: <IconEditOutline16 size={16} />,
                  },
                  {
                    id: "fork",
                    label: "分叉",
                    icon: <IconBranchOutline16 size={16} />,
                  },
                  {
                    id: "delete",
                    label: "删除",
                    icon: <IconTrashOutline16 size={16} />,
                    danger: true,
                  },
                ]}
                onSelect={(id) => {
                  if (id === "rename") onRenameSession(session);
                  else if (id === "fork") onForkSession(session.id);
                  else if (id === "delete") onDeleteSession(session);
                }}
              />
            </span>
          </div>
        ))}
      </div>

      <div className="rail__foot">
        <button
          ref={settingsTriggerRef}
          type="button"
          className="rail__footrow"
          aria-haspopup="dialog"
          aria-expanded={settingsOpen}
          onClick={onOpenSettings}
        >
          <IconSettingsOutline16 size={16} />
          <span className="rail__title">设置</span>
        </button>
      </div>
    </>
  );
}
