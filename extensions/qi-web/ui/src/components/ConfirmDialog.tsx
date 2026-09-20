/**
 * 二次确认层。**所有会落盘的写操作都必须过它**(design/web.md §8.3 的安全策略):
 * 弹层里要显示目标 provider、**目标文件的绝对路径**与掩码后的值,让"我要改什么"一目了然。
 */
import type { ReactNode } from "react";

export interface ConfirmRow {
    label: string;
    value: string;
}

export function ConfirmDialog({
    title,
    rows,
    warning,
    confirmLabel = "确认",
    danger = false,
    onConfirm,
    onCancel,
    children,
}: {
    title: string;
    rows: ConfirmRow[];
    warning?: string;
    confirmLabel?: string;
    danger?: boolean;
    onConfirm: () => void;
    onCancel: () => void;
    children?: ReactNode;
}) {
    return (
        <div
            className="overlay"
            role="dialog"
            aria-modal="true"
            aria-label={title}
            onKeyDown={(event) => {
                if (event.key === "Escape") onCancel();
            }}
        >
            <div className="dialog">
                <div className="dialog__title">{title}</div>
                <dl className="dialog__rows">
                    {rows.map((row) => (
                        <div key={row.label} style={{ display: "contents" }}>
                            <dt>{row.label}</dt>
                            <dd>{row.value}</dd>
                        </div>
                    ))}
                </dl>
                {children}
                {warning ? <div className="dialog__warn">{warning}</div> : null}
                <div className="dialog__actions">
                    <button type="button" className="btn" onClick={onCancel}>
                        取消
                    </button>
                    <button
                        type="button"
                        className={`btn ${danger ? "btn--danger" : "btn--primary"}`}
                        onClick={onConfirm}
                    >
                        {confirmLabel}
                    </button>
                </div>
            </div>
        </div>
    );
}
