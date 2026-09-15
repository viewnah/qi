/**
 * 工作区 chip —— 对应 dsh `EmptyHero.tsx` 导出的 `WorkspaceChip`:
 * 「文件夹图标 + 标签 + 下箭头」,点开可切换工作区。
 *
 * 逐条对齐 dsh 的语义,它们都是**有意义的**,不是随手写的:
 *   · 标签为 `undefined` 时进入**占位态**:换用「合上的文件夹」+ 「选择工作区」文案;
 *     有标签时是「打开的文件夹」。所以图标本身编码了"是否已选定";
 *   · `aria-label` 恒为「选择工作区」(与可见文案解耦,可见文案会随状态变);
 *   · `aria-haspopup="menu"` + `aria-expanded` 表达弹层;
 *   · 弹层展开时**保持 hover 底色**(`.workspace[aria-expanded='true']`),
 *     所以 chip 在菜单开着的时候看起来仍是激活的;
 *   · `disabled` 是"锁定的回显"(没有 hover 反馈、没有指针手势),标签仍是全对比度。
 *
 * 与 dsh 的差别(写出来,不假装):dsh 的弹层是 `Menu` 原语(portal 定位、
 * 键盘导航、子菜单)。qi 这里只做**一个锚在 chip 下方的面板**,键盘只支持
 * Escape/Enter 两个键。要 1:1 得把 `ui-primitives` 的 Menu 整套移植,不在首页这一步。
 */
import { useState } from "react";
import {
  IconChevronDownOutline14,
  IconFolderClose16,
  IconFolderOpen16,
} from "./icons";

export interface WorkspaceOption {
  cwd: string;
  label: string;
}

export function WorkspaceChip({
  workspace,
  workspaces,
  disabled = false,
  onPick,
}: {
  /** 当前工作区 cwd;`null` → 占位态(合上的文件夹 + 「选择工作区」)。 */
  workspace: string | null;
  workspaces: WorkspaceOption[];
  /** 锁定态回显:无 hover 反馈、无指针手势。 */
  disabled?: boolean;
  onPick: (cwd: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");

  // 路径 → chip 标签:末段;纯分隔符的路径回落到原串(dsh `workspaceLabel` 同义)。
  const label = (() => {
    if (!workspace) return undefined;
    const parts = workspace.split("/").filter(Boolean);
    return parts.length > 0 ? parts[parts.length - 1] : workspace;
  })();

  const apply = (value: string | null) => {
    onPick(value);
    setOpen(false);
  };

  return (
    <div className="hero__workspaceAnchor">
      <button
        type="button"
        className="hero__workspace"
        aria-label="选择工作区"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        title={workspace ?? "使用服务端默认工作目录"}
        onClick={() => {
          setOpen((v) => !v);
          setDraft(workspace ?? "");
        }}
      >
        {label === undefined ? (
          <IconFolderClose16 className="hero__folder" size={16} />
        ) : (
          <IconFolderOpen16 className="hero__folder" size={16} />
        )}
        <span className="hero__workspaceLabel">{label ?? "选择工作区"}</span>
        <IconChevronDownOutline14 className="hero__chevron" size={12} />
      </button>

      {open ? (
        <div className="hero__menu" role="menu">
          <input
            value={draft}
            autoFocus
            aria-label="工作目录"
            placeholder="/absolute/path/to/project"
            className="hero__pathInput"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") apply(draft.trim() || null);
              if (event.key === "Escape") setOpen(false);
            }}
          />
          <button
            type="button"
            role="menuitem"
            className="hero__menuItem"
            onClick={() => apply(draft.trim() || null)}
          >
            使用这个目录
          </button>
          {workspaces.length > 0 ? <div className="hero__menuSep" /> : null}
          {workspaces.slice(0, 6).map((option) => (
            <button
              key={option.cwd}
              type="button"
              role="menuitem"
              className="hero__menuItem"
              title={option.cwd}
              onClick={() => apply(option.cwd)}
            >
              <span className="hero__menuItemLabel">{option.label}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
