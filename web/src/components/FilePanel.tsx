/**
 * 右侧**文件面板**:一栏多页签(与 dsh 一致)—— 固定的「文件」树 + 每个打开的文件一个页签。
 *
 * 版式与行为照 dsh 的 `ui-sidebar-right`(面板 + 页签栏)+ `ui-sidebar-files`(树)+
 * `ui-sidebar-documentpreview`(预览),但按 qi 的现状收窄了两处(写在这里便于对照):
 *
 *   1. **页签式,但不搬 dsh 的停靠系统**。dsh 的右栏建在 `ui-dockkit` 上(分屏 / 浮层 /
 *      拖动重排 / 每栏多页签)。qi 只要**页签栏**这一层:点文件开一个页签、页签栏来回切。
 *      页签几何照它 `dockkit.module.css` 的 `.tab`:28px 高、r12、激活态 `label-primary`
 *      + `markdown-tag` 底、关闭钮悬停/激活才显形、**最后一个不可关**。
 *   2. **一次只列一层**(懒展开,与 dsh 一致:整棵递归列会在 `node_modules` 上卡死);
 *      **文件行用扩展名徽标而不是图标** —— dsh 的文件图标是 60 个按语言的遮罩美术
 *      (`code-file-icon-artwork.ts`),qi 不搬。
 *
 * 安全面在**后端**(路径边界 = 会话 cwd,与文件工具同一条;原字节只给白名单类型)。
 * 前端这里只有一条:**HTML 进 `<iframe sandbox="">`** —— 空 sandbox = 不透明源,
 * 脚本不执行、拿不到本站凭证(后端还会再加一个 `CSP: sandbox` 响应头,双保险)。
 */
import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { FileContent, FileEntry, FileListing } from "../api/types";
import {
  IconChevronDownOutline14,
  IconCloseFill14,
  IconFolderOpen16,
  IconRefreshOutline16,
} from "./icons";

/**
 * 一个页签:固定的「文件」树,或某个文件。
 *
 * 内容(`content`)挂在**页签**上而不是面板上:切回一个已读过的文件不该重新读盘,
 * 也不该把另一个页签的内容串过来。
 */
type Tab =
  | { id: "files"; kind: "files" }
  | {
      id: string;
      kind: "file";
      path: string;
      name: string;
      size: number;
      raw: boolean;
      content: FileContent | null;
    };

const FILES_TAB: Tab = { id: "files", kind: "files" };
/** 文件页签的 id 由路径派生:同一个文件打开两次只会有一个页签(dsh 同理)。 */
const fileTabId = (path: string): string => `file:${path}`;

/** 扩展名 → 展示类型。**qi 自定**,不是照搬 dsh 的 48 类(见文件头第 2 条)。 */
type FileKind = "code" | "markdown" | "html" | "image" | "pdf" | "other";

const EXT_KIND: Record<string, FileKind> = {};
for (const ext of ["py", "ts", "tsx", "js", "jsx", "mjs", "go", "rs", "java", "c", "h",
                   "cpp", "hpp", "rb", "php", "swift", "kt", "sh", "bash", "zsh",
                   "sql", "toml", "yaml", "yml", "json", "css", "scss", "vue", "xml"]) {
  EXT_KIND[ext] = "code";
}
for (const ext of ["md", "mdx"]) EXT_KIND[ext] = "markdown";
for (const ext of ["html", "htm"]) EXT_KIND[ext] = "html";
for (const ext of ["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif"]) {
  EXT_KIND[ext] = "image";
}
EXT_KIND.pdf = "pdf";

function kindOf(name: string): FileKind {
  const dot = name.lastIndexOf(".");
  if (dot <= 0) return "other";
  return EXT_KIND[name.slice(dot + 1).toLowerCase()] ?? "other";
}

/** 徽标上的字:扩展名(最长 4 字符);没有扩展名就一个中点。 */
function badgeOf(name: string): string {
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot === name.length - 1) return "·";
  return name.slice(dot + 1, dot + 5).toLowerCase();
}

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function FilePanel({
  sessionId,
  onClose,
}: {
  sessionId: string | null;
  onClose: () => void;
}) {
  const [tabs, setTabs] = useState<Tab[]>([FILES_TAB]);
  const [activeId, setActiveId] = useState<string>("files");
  /** 树的状态留在面板上(不随页签切换而丢):切走再切回来还在同一层。 */
  const [dir, setDir] = useState("");
  const [listing, setListing] = useState<FileListing | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** 每次刷新 +1,用来强制重列当前层。 */
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const next = await api.files(sessionId, dir);
        if (!alive) return;
        setListing(next);
        setError(null);
      } catch (err) {
        if (!alive) return;
        setListing(null);
        setError(err instanceof ApiError ? err.detail : String(err));
      }
    })();
    return () => {
      alive = false;
    };
  }, [dir, sessionId, nonce]);

  // 换会话就把页签收回到「文件」:否则会留着一堆"属于上一个会话"的文件挂着
  // (它们的路径在新会话里可能根本不存在,或指向别的东西)。
  useEffect(() => {
    setTabs([FILES_TAB]);
    setActiveId("files");
    setDir("");
  }, [sessionId]);

  const readFile = useCallback(
    (entry: FileEntry, id: string) => {
      void (async () => {
        try {
          const content = await api.fileContent(sessionId, entry.path);
          setTabs((prev) =>
            prev.map((tab) => (tab.id === id && tab.kind === "file" ? { ...tab, content } : tab)),
          );
        } catch (err) {
          // 读失败也**留在页签里**显示原因:关掉页签会让"点了没反应"更难查。
          const content: FileContent = {
            path: entry.path,
            kind: "binary",
            size: entry.size,
            truncated: false,
            lang: "",
            text: err instanceof ApiError ? err.detail : String(err),
          };
          setTabs((prev) =>
            prev.map((tab) => (tab.id === id && tab.kind === "file" ? { ...tab, content } : tab)),
          );
        }
      })();
    },
    [sessionId],
  );

  const openFile = useCallback(
    (entry: FileEntry) => {
      const id = fileTabId(entry.path);
      setActiveId(id);
      setTabs((prev) => {
        if (prev.some((tab) => tab.id === id)) return prev;   // 已开过:只切过去,不重读
        return [...prev, { id, kind: "file", path: entry.path, name: entry.name,
                           size: entry.size, raw: entry.raw, content: null }];
      });
      readFile(entry, id);
    },
    [readFile],
  );

  /** 关一个页签。关掉当前页签时切到左邻居(没有就右邻居)—— dsh 的页签栏同规矩。 */
  const closeTab = (id: string) => {
    setTabs((prev) => {
      if (prev.length <= 1) return prev;                    // 最后一个不可关(面板自己有 ×)
      const index = prev.findIndex((tab) => tab.id === id);
      if (index < 0) return prev;
      const next = prev.filter((tab) => tab.id !== id);
      if (id === activeId) {
        setActiveId(next[Math.max(0, index - 1)]?.id ?? "files");
      }
      return next;
    });
  };

  const active = tabs.find((tab) => tab.id === activeId) ?? FILES_TAB;
  const rootName = listing?.root.split("/").filter(Boolean).pop() ?? "(会话目录)";

  return (
    <div className="files">
      {/* 页签栏 = 面板的整个上沿(dsh `SidebarRight` 的注释就是这么说的:
          "the pane's tab strip... is the panel's whole top edge")。 */}
      <div className="files__strip">
        <div className="files__tabs">
          {tabs.map((tab) => {
            const on = tab.id === active.id;
            return (
              <div
                key={tab.id}
                className="files__tab"
                data-active={on || undefined}
                role="tab"
                aria-selected={on}
                tabIndex={on ? 0 : -1}
                title={tab.kind === "file" ? tab.path : "会话目录"}
                onClick={() => setActiveId(tab.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    setActiveId(tab.id);
                  }
                }}
              >
                <span className="files__tabtitle">
                  {tab.kind === "file" ? tab.name : rootName}
                </span>
                {tabs.length > 1 ? (
                  <button
                    type="button"
                    className="files__tabclose"
                    aria-label={`关闭 ${tab.kind === "file" ? tab.name : "文件"}`}
                    title="关闭"
                    onClick={(event) => {
                      event.stopPropagation();   // 别让关闭顺带把页签切过去
                      closeTab(tab.id);
                    }}
                  >
                    <IconCloseFill14 size={12} />
                  </button>
                ) : null}
              </div>
            );
          })}
        </div>
        <div className="files__stripend">
          {active.kind === "files" ? (
            <button
              type="button"
              className="files__icon"
              title="重新列这一层"
              aria-label="重新列这一层"
              onClick={() => setNonce((n) => n + 1)}
            >
              <IconRefreshOutline16 size={14} />
            </button>
          ) : null}
          <button
            type="button"
            className="files__icon"
            title="收起文件面板"
            aria-label="收起文件面板"
            onClick={onClose}
          >
            <IconCloseFill14 size={14} />
          </button>
        </div>
      </div>

      {/* 文件页签里唯一需要自己头部的东西:当前在哪一层(带上一级)。 */}
      {active.kind === "files" ? (
        <div className="files__crumbrow">
          <button
            type="button"
            className="files__icon"
            title="上一级"
            aria-label="上一级"
            disabled={dir === ""}
            onClick={() => setDir(listing?.parent ?? "")}
          >
            <IconChevronDownOutline14 size={12} className="files__back" />
          </button>
          <span className="files__path" title={listing?.path ?? ""}>
            {rootName}
            {dir === ""
              ? null
              : dir.split("/").filter(Boolean).map((seg) => (
                  <span key={seg} className="files__crumb">
                    /{seg}
                  </span>
                ))}
          </span>
        </div>
      ) : null}

      <div className="files__body">
        {active.kind === "file" ? (
          <PreviewBody tab={active} sessionId={sessionId} />
        ) : error === null ? listing === null ? (
          <p className="files__note">读取中…</p>
        ) : (
          <ul className="filetree">
            {listing.entries.length === 0 ? (
              <li className="files__note">这个目录是空的。</li>
            ) : null}
            {listing.entries.map((entry) => (
              <li key={entry.path}>
                <button
                  type="button"
                  className="filetree__row"
                  data-kind={entry.kind}
                  onClick={() => {
                    if (entry.kind === "dir") setDir(entry.path);
                    else openFile(entry);
                  }}
                >
                  {entry.kind === "dir" ? (
                    <span className="filetree__folder">
                      <IconFolderOpen16 size={16} />
                    </span>
                  ) : (
                    <span className="filetree__badge" data-kind={kindOf(entry.name)}>
                      {badgeOf(entry.name)}
                    </span>
                  )}
                  <span className="filetree__name">{entry.name}</span>
                  {entry.kind === "file" ? (
                    <span className="filetree__meta">{humanSize(entry.size)}</span>
                  ) : null}
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="files__note">{error}</p>
        )}
      </div>
    </div>
  );
}

/** 预览正文:按类型选渲染方式。各分支的安全口径见文件头。 */
function PreviewBody({
  tab,
  sessionId,
}: {
  tab: Extract<Tab, { kind: "file" }>;
  sessionId: string | null;
}) {
  const raw = api.fileRawUrl(sessionId, tab.path);

  // 原字节:图片 / PDF / HTML(后端只给白名单类型;其它类型这里也走不到)
  if (tab.raw) {
    if (kindOf(tab.name) === "image") {
      return (
        <div className="fileprev">
          <img className="fileprev__img" src={raw} alt={tab.name} />
        </div>
      );
    }
    if (kindOf(tab.name) === "pdf") {
      return (
        <div className="fileprev">
          <iframe className="fileprev__frame" src={raw} title={tab.name} />
        </div>
      );
    }
    if (kindOf(tab.name) === "html") {
      return (
        <div className="fileprev">
          {/* 空 sandbox = 不透明源:脚本不执行、拿不到本站凭证(后端另有 CSP: sandbox) */}
          <iframe className="fileprev__frame" src={raw} title={tab.name} sandbox="" />
          <p className="files__note">HTML 预览:脚本已禁用(沙箱)。</p>
        </div>
      );
    }
  }

  if (tab.content === null) return <p className="files__note">读取中…</p>;
  if (tab.content.kind === "binary") {
    return (
      <div className="fileprev">
        <p className="files__note">
          这个类型不支持预览。{humanSize(tab.size)}
        </p>
      </div>
    );
  }
  return (
    <div className="fileprev">
      <pre className="fileprev__pre">{tab.content.text}</pre>
      {tab.content.truncated ? (
        <p className="files__note">
          文件较大,只显示前 {Math.round(tab.content.text.length / 1024)} KB。
        </p>
      ) : null}
    </div>
  );
}
