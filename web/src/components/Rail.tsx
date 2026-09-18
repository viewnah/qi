/**
 * 左栏:**品牌行 · 新会话 · 工作区(二级目录)· 底部设置**。
 *
 * ## 二级目录
 *
 * 一级是**项目**(会话的 `cwd`,见 `state/projects.ts`),二级是**会话本身**。
 * 分组不是新数据,是前端派生的;这里只负责把它画出来。
 *
 * ## 几何为什么是这些数字
 *
 * 逐条照搬 dsh(`SidebarRoot.module.css` / `ui-workspace/rows/Rows.module.css`),
 * 并与官方截图 `data/dsh.png` 的像素实测对得上(2506×1758 = 1253×879 @2x):
 *
 * | 项 | 值 | 来源 / 实测 |
 * | --- | --- | --- |
 * | 栏宽 / 左右内衬 | 280 / 12 | `--dsh-sidebar-width` / `--dsh-sidebar-inline-padding` |
 * | 品牌行高 | 60 | `.logoRow`,实测 y 27–46 |
 * | 新会话按钮 | 高 38、半径 12 | `.newSession`,实测 y 74–112、x 14–266 |
 * | 区块标题 | 高 36 | `.sectionHeader`,实测 y 133–146 |
 * | 一级行 | 高 34,文件夹槽 16 + gap 6 → 标题离行左 30 | `.projectRow`,实测标题 x 43 |
 * | 二级行 | 高 32,状态槽 16 + 标题 margin-left 4 → 标题离行左 28 | `.sessionRow`,实测标题 x 41 |
 * | 行间距 / 段落间距 | 2 / 4 | `.groupSection > * + *` / `.groupSection + .groupSection` |
 * | 选中/悬停底 | 半径 8、距栏边 12 | 实测选中行矩形 x 12–266、高 32 |
 *
 * 二级行**不比一级缩进更多**——这不是疏忽,是 dsh 的做法:一级行左边是
 * **文件夹**,二级行左边是 **16px 状态槽**,两者的标题起点几乎重合
 * (30 vs 28)。层级靠"文件夹 + 展开箭头"和分组来读,不靠缩进;因此列表在
 * 280px 里能放下更多字。
 *
 * ## 与 dsh 的两处**有意的**不同
 *
 * 1. **没有折叠成 56px 窄栏的开关**。dsh 的 logoRow 右侧那个 panel 图标是折叠
 *    开关,折叠后靠窄栏里的图标回展。qi 没做窄栏形态,所以不放这个按钮 ——
 *    宁可少一个控件,也不放一个点了没反应的图标。
 * 2. **一级行的动作是"重命名工作区 / 删除工作区 / 在此新建会话"**(dsh 还有拖拽排序)。
 *    注意"重命名"改的是**显示名**,不是目录名 —— 项目就是目录,改目录名等于改所有
 *    会话的 cwd,那是另一件事(见 `src/qi_agent/workspaces.py`)。拖拽排序对
 *    "按目录分组"没有意义,不做。
 *
 * 行尾的「…」菜单由 `RowMenu` 渲染(portal + 视口翻转),两个层级的动作项在那里,
 * 不在这里拼 DOM。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import type { RefObject } from "react";
import {
  IconFolderClose16,
  IconFolderOpen16,
  IconNewChatOutline16,
  IconPlusOutline16,
  IconSettingsOutline16,
  IconTriangleRightFill14,
} from "./icons";
import { RowMenu } from "./RowMenu";
import { SidebarSection } from "./SidebarSection";
import {
  IconBranchOutline16,
  IconEditOutline16,
  IconTrashOutline16,
} from "./icons";
import { filterGroups, relativeLabel, stampOf } from "../state/projects";
import type { ProjectGroup } from "../state/projects";
import type { SessionSummary } from "../api/types";

export function Rail({
  groups,
  current,
  busy,
  now,
  onSelect,
  onCreate,
  onRenameSession,
  onForkSession,
  onDeleteSession,
  onRenameWorkspace,
  onDeleteWorkspace,
  onClearUngrouped,
  onAddWorkspace,
  onOpenSettings,
  settingsOpen = false,
  settingsTriggerRef,
}: {
  groups: ProjectGroup[];
  current: string | null;
  busy: boolean;
  /** 当前 epoch 毫秒(行尾相对时间由它算出;由 App 定时推进)。 */
  now: number;
  onSelect: (id: string) => void;
  /** 新建会话。`cwd` 给了就在该项目里建,否则用默认工作目录。 */
  onCreate: (cwd?: string) => void;
  /** 会话行「…」里的三件事(重命名 / 分叉 / 删除),由 App 弹层确认。 */
  onRenameSession: (session: SessionSummary) => void;
  onForkSession: (id: string) => void;
  onDeleteSession: (session: SessionSummary) => void;
  /** 工作区行「…」里的两件事。改名只改**显示名**,目录不动;删除见 docs/web.md。 */
  onRenameWorkspace: (group: ProjectGroup) => void;
  onDeleteWorkspace: (group: ProjectGroup) => void;
  /** 「未分组」桶的清除:这一桶里的会话一起删(它们没有 cwd,没有工作区能带走它们)。 */
  onClearUngrouped: (group: ProjectGroup) => void;
  /** 「添加工作区」:qi 的工作区就是目录,所以这个动作 = 在某个目录下新建一条会话。 */
  onAddWorkspace: () => void;
  onOpenSettings: () => void;
  /** 设置浮层当前是否开着(只为了 `aria-expanded`,交互不走它)。 */
  settingsOpen?: boolean;
  /** 「设置」行本身的 ref:浮层关掉时把焦点**还给**它(见 Settings 的 `returnFocusTo`)。 */
  settingsTriggerRef?: RefObject<HTMLButtonElement | null>;
}) {
  // 折叠的是**组**,不是单条会话:单条折叠只省一行,却多一次点击。
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  /** 搜索串(搜索框自己管展开与焦点,见 SidebarSection)。 */
  const [query, setQuery] = useState("");

  /**
   * 搜索时显示的是**过滤后的树**,并且忽略折叠状态 —— 搜出来的东西被折叠着等于没搜。
   * 过滤规则在 `filterGroups` 里(纯函数,有单测)。
   */
  const visible = useMemo(() => filterGroups(groups, query), [groups, query]);
  const filtered = query.trim().length > 0;

  // 打开某个会话时,它所在的组必须可见 —— 否则"点开了一个自己看不见的行"。
  // 只在 current 变化时被动展开,不反过来把用户手动折叠的组顶开。
  const currentGroup = groups.find((g) => g.containsCurrent)?.key;
  useEffect(() => {
    if (currentGroup === undefined) return;
    setCollapsed((prev) =>
      prev[currentGroup] ? { ...prev, [currentGroup]: false } : prev,
    );
  }, [currentGroup]);

  const toggleGroup = useCallback((key: string) => {
    setCollapsed((prev) => ({ ...prev, [key]: !prev[key] }));
  }, []);

  return (
    <>
      {/* 品牌行:纯文字字标「Qi Web」—— 不放 mark、不放图标。
          dsh 这里同时也是"新会话"的快捷键,qi 不重复那个入口(下面就是一个
          38px 的新会话按钮),所以不做成一个假按钮。 */}
      <div className="rail__brandrow">
        <span className="rail__wordmark">Qi Web</span>
      </div>

      <button
        type="button"
        className="rail__new"
        onClick={() => onCreate()}
        disabled={busy}
      >
        <IconNewChatOutline16 size={14} />
        <span className="rail__new-label">新会话</span>
      </button>

      {/* 区块标题(标签 / 搜索 / 添加工作区)整个交给 SidebarSection ——
          它自己的展开状态与输入框 ref 都收在那里,这里只留"查询串"去过滤列表。 */}
      <SidebarSection
        query={query}
        onQueryChange={setQuery}
        onAddWorkspace={onAddWorkspace}
      />

      <div className="rail__list" role="tree" aria-label="工作区与会话">
        {groups.length === 0 ? (
          <div className="rail__hint">还没有会话</div>
        ) : null}
        {filtered && visible.length === 0 ? (
          <div className="rail__hint">没有匹配「{query.trim()}」的会话</div>
        ) : null}

        {visible.map((group) => {
          // 搜索期间一律展开:命中的会话被折叠着,等于没搜到。
          const open = filtered || !collapsed[group.key];
          return (
            <div className="rail__group" key={group.key}>
              {/* 一级:项目。`role=treeitem` + 内部按钮(按钮不能嵌按钮)。 */}
              <div
                className="rail__project"
                role="treeitem"
                aria-expanded={open}
                tabIndex={0}
                onClick={() => toggleGroup(group.key)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    toggleGroup(group.key);
                  }
                }}
              >
                {/* 文件夹 ↔ 展开箭头:hover / 键盘聚焦时换成箭头(dsh 同款)。
                    两个槽都是 16px,所以换的时候标题不会跳。 */}
                <span
                  className="rail__slot rail__folder"
                  data-active={group.containsCurrent}
                >
                  {open ? (
                    <IconFolderOpen16 size={16} />
                  ) : (
                    <IconFolderClose16 size={16} />
                  )}
                </span>
                <span className="rail__slot rail__chevron">
                  <IconTriangleRightFill14
                    size={12}
                    className="rail__arrow"
                    {...(open ? { "data-open": "true" } : {})}
                  />
                </span>
                <span className="rail__title">{group.label}</span>
                <span className="rail__actions">
                  {/* 「未分组」= 没有 cwd 的旧会话:没有目录,所以不能改名、也没有
                      "删除工作区"可言;它需要的恰恰是**批量清掉**——那些会话不属于任何
                      目录,删除工作区带不走它们,否则只能一条条点。 */}
                  {group.cwd === null ? (
                    <RowMenu
                      subject={`未分组的 ${group.sessions.length} 条会话`}
                      items={[
                        {
                          id: "clear",
                          label: "清除会话",
                          icon: <IconTrashOutline16 size={16} />,
                          danger: true,
                        },
                      ]}
                      onSelect={() => onClearUngrouped(group)}
                    />
                  ) : (
                    <>
                      <RowMenu
                        subject={`工作区「${group.label}」`}
                        items={[
                          {
                            id: "rename",
                            label: "重命名工作区",
                            icon: <IconEditOutline16 size={16} />,
                          },
                          {
                            id: "delete",
                            label: "删除工作区",
                            icon: <IconTrashOutline16 size={16} />,
                            danger: true,
                          },
                        ]}
                        onSelect={(id) => {
                          if (id === "rename") onRenameWorkspace(group);
                          else if (id === "delete") onDeleteWorkspace(group);
                        }}
                      />
                      <button
                        type="button"
                        className="rail__rowbtn"
                        title={`在「${group.label}」新建会话`}
                        aria-label={`在「${group.label}」新建会话`}
                        disabled={busy}
                        onClick={(e) => {
                          e.stopPropagation();
                          onCreate(group.cwd ?? undefined);
                        }}
                      >
                        <IconPlusOutline16 size={16} />
                      </button>
                    </>
                  )}
                </span>
              </div>

              {/* 二级:会话。 */}
              {open
                ? group.sessions.map((session) => (
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
                      <span className="rail__title">
                        {session.title || "未命名"}
                      </span>
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
                  ))
                : null}
            </div>
          );
        })}
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
          {/* 图标**不套 `.rail__slot`**:那个槽是列表行的规矩(它的颜色是
              `label-tertiary`),会把按钮的 `label-primary` 盖成灰的 ——
              dsh 的 `TriggerContent` 也是直接把图标放进按钮里。 */}
          <IconSettingsOutline16 size={16} />
          <span className="rail__title">设置</span>
        </button>
      </div>
    </>
  );
}
