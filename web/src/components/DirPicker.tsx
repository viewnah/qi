/**
 * 「选择目录」——**服务器路径**浏览。
 *
 * 与 dsh 的关键差别(dsh 是 Electron 桌面应用,它弹的是**操作系统**的文件选择器):
 * qi-web 是服务端,前端碰不到用户的磁盘,所以这里走 qi 自己的
 * `GET /api/fs/dirs` 逐级浏览**服务器**上的目录。形状与交互照 **pi-web**
 * (=同源的服务端形态,实测自 `@agegr/pi-web` 的 `/api/cwd/browse` 与它的中文词汇):
 *
 *   · 「目录路径」+ 「转到目录」 —— 也可以直接把路径粘进来;
 *   · 「转到上级目录」(`parent` 为 `null` 时禁用 —— 已经在根上);
 *   · 目录列表,点一行就进去;空目录显示「没有子目录」,加载中显示「正在加载目录…」;
 *   · 确认键是「选择此文件夹」,选的是**当前所在目录**,不是列表里高亮的那一行。
 *
 * 单独成组件的理由:`ConfirmDialog` 只负责"标题 + 行 + 动作"的壳,浏览器的
 * 状态(当前路径、列表、加载、错误)整块收在这里,调用方只给一个初始路径与
 * 一个"选中之后干什么"的回调。
 */
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, ContractError } from "../api/client";
import type { DirectoryListing } from "../api/types";
import { IconFolderClose16 } from "./icons";
import { ConfirmDialog } from "./ConfirmDialog";

function describe(err: unknown): string {
  if (err instanceof ContractError) return err.message;
  if (err instanceof ApiError) return err.detail;
  return String(err);
}

export function DirPicker({
  initialPath,
  onPick,
  onClose,
}: {
  /** 一打开就落在哪个目录(通常是当前项目/默认工作目录)。 */
  initialPath: string;
  /**
   * 选中某个目录之后干什么(建会话、记住它……)。回 `null` = 成功、可以关闭;
   * 回一个字符串 = 失败,**显示为弹层里的错误**而不是关掉它。
   */
  onPick: (path: string) => Promise<string | null>;
  onClose: () => void;
}) {
  const [listing, setListing] = useState<DirectoryListing | null>(null);
  const [draft, setDraft] = useState(initialPath);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  const open = useCallback(async (path: string) => {
    setLoading(true);
    try {
      const next = await api.dirs(path);
      setListing(next);
      setDraft(next.path);
      setError(null);
    } catch (err) {
      // 失败时**保留当前列表**并就地报错:路径打错不该把浏览状态清空。
      setError(describe(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void open(initialPath);
  }, [initialPath, open]);

  const confirm = async () => {
    if (listing === null || busy) return;
    setBusy(true);
    try {
      const failure = await onPick(listing.path);
      if (failure === null) onClose();
      else setError(failure);
    } finally {
      setBusy(false);
    }
  };

  const parent = listing?.parent ?? null;

  return (
    <ConfirmDialog
      title="选择目录"
      rows={[{ label: "当前目录", value: listing?.path ?? draft ?? "…" }]}
      confirmLabel="选择此文件夹"
      onConfirm={() => void confirm()}
      onCancel={onClose}
    >
      <div className="picker">
        <div className="picker__bar">
          <input
            className="dialog__input"
            value={draft}
            autoFocus
            aria-label="目录路径"
            placeholder="~/src/my-project"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void open(draft);
            }}
          />
          <button
            type="button"
            className="btn"
            onClick={() => void open(draft)}
            disabled={loading}
          >
            转到
          </button>
        </div>

        <div className="picker__shortcuts">
          <button
            type="button"
            className="ghost"
            disabled={parent === null}
            onClick={() => void open(parent ?? "")}
          >
            转到上级目录
          </button>
          <button
            type="button"
            className="ghost"
            onClick={() => void open(listing?.home ?? "")}
          >
            主目录
          </button>
          {/* Windows 才有盘符:没盘符就选不到别的分区。 */}
          {(listing?.roots ?? []).map((root) => (
            <button
              key={root.path}
              type="button"
              className="ghost"
              onClick={() => void open(root.path)}
            >
              {root.name}
            </button>
          ))}
        </div>

        <div className="picker__list" aria-label="子目录">
          {loading && listing === null ? (
            <div className="picker__hint">正在加载目录…</div>
          ) : (listing?.entries.length ?? 0) === 0 ? (
            <div className="picker__hint">没有子目录</div>
          ) : (
            (listing?.entries ?? []).map((entry) => (
              <button
                key={entry.path}
                type="button"
                className="picker__row"
                title={entry.path}
                onClick={() => void open(entry.path)}
              >
                <IconFolderClose16 size={16} />
                <span className="picker__name">{entry.name}</span>
              </button>
            ))
          )}
        </div>

        {error === null ? null : <div className="dialog__error">{error}</div>}
      </div>
    </ConfirmDialog>
  );
}
