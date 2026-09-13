/**
 * 工作区 chip —— 对应 dsh 的 `WorkspaceChip`(`EmptyHero.d.ts` 里导出的那个):
 * 「文件夹图标 + 路径 basename + 下箭头」,点开可切换工作区。
 *
 * dsh 的语义:**首个消息之前**工作区始终可切换。qi 里工作区 = 会话的 `cwd`
 * (会话头里记着它),所以首页选工作区就等于决定新会话建在哪个目录。
 */
import { useState } from "react";

export interface WorkspaceOption {
  cwd: string;
  label: string;
}

export function WorkspaceChip({
  workspace,
  workspaces,
  onPick,
}: {
  workspace: string | null;
  workspaces: WorkspaceOption[];
  onPick: (cwd: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");

  const apply = (value: string | null) => {
    onPick(value);
    setOpen(false);
  };

  return (
    <>
      <button
        type="button"
        className="workspace-chip"
        aria-expanded={open}
        onClick={() => {
          setOpen((v) => !v);
          setDraft(workspace ?? "");
        }}
        title={workspace ?? "使用服务端默认工作目录"}
      >
        <span aria-hidden="true">📁</span>
        <span className="workspace-chip__label">
          {workspace ?? "选择工作区"}
        </span>
        <span className="workspace-chip__chevron" aria-hidden="true">
          ▾
        </span>
      </button>

      {open ? (
        <div className="composer__menu">
          <input
            value={draft}
            autoFocus
            aria-label="工作目录"
            placeholder="/absolute/path/to/project"
            className="composer__pathInput"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") apply(draft.trim() || null);
              if (event.key === "Escape") setOpen(false);
            }}
          />
          <button
            type="button"
            className="ghost"
            onClick={() => apply(draft.trim() || null)}
          >
            确定
          </button>
        </div>
      ) : null}

      {open && workspaces.length > 0 ? (
        <div className="composer__menu">
          {workspaces.slice(0, 6).map((option) => (
            <button
              key={option.cwd}
              type="button"
              className="chip"
              title={option.cwd}
              onClick={() => apply(option.cwd)}
            >
              {option.label}
            </button>
          ))}
        </div>
      ) : null}
    </>
  );
}
