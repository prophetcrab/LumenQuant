import type { CSSProperties, ReactNode } from "react";
import { useEffect } from "react";
import { pct } from "../shared/format";

export function Card({
  title,
  subtitle,
  actions,
  children,
  padded = true,
  className = "",
  style,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  padded?: boolean;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <section className={`card ${className}`} style={style}>
      {(title || actions) && (
        <header className="card-header">
          <div>
            {title ? <div className="card-title">{title}</div> : null}
            {subtitle ? <div className="card-subtitle">{subtitle}</div> : null}
          </div>
          {actions ? <div className="row">{actions}</div> : null}
        </header>
      )}
      <div className={padded ? "card-body" : ""}>{children}</div>
    </section>
  );
}

export function Stat({
  label,
  value,
  meta,
  delta,
  accent,
}: {
  label: ReactNode;
  value: ReactNode;
  meta?: ReactNode;
  delta?: number | null;
  accent?: "positive" | "negative" | "neutral";
}) {
  const tone =
    accent === "positive" ? "up" : accent === "negative" ? "down" : delta && delta !== 0 ? (delta > 0 ? "up" : "down") : "";
  return (
    <div className="card stat-card">
      <div className="stat-label">{label}</div>
      <div className="stat-value num">{value}</div>
      <div className="stat-meta">
        {delta !== undefined && delta !== null ? (
          <span className={`delta num ${tone}`}>
            {delta > 0 ? "▲" : delta < 0 ? "▼" : "•"} {pct(delta)}
          </span>
        ) : null}
        {meta}
      </div>
    </div>
  );
}

export function Badge({
  children,
  tone = "",
}: {
  children: ReactNode;
  tone?: "green" | "red" | "amber" | "indigo" | "";
}) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function EmptyState({
  title,
  text,
  action,
}: {
  title: string;
  text?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      {text ? <div className="empty-text">{text}</div> : null}
      {action}
    </div>
  );
}

export function Loader({ label = "加载中" }: { label?: string }) {
  return (
    <div className="empty" style={{ flexDirection: "row", gap: 10, padding: 24 }}>
      <span className="spinner" />
      <span>{label}</span>
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="empty">
      <div className="empty-title">加载失败</div>
      <div className="empty-text">{message}</div>
      {onRetry ? (
        <button className="button small" onClick={onRetry}>
          重试
        </button>
      ) : null}
    </div>
  );
}

export function SkeletonRows({ rows = 4 }: { rows?: number }) {
  return (
    <div className="stack" style={{ padding: 4 }}>
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="skeleton" style={{ height: 18, width: `${92 - index * 9}%` }} />
      ))}
    </div>
  );
}

export function Field({
  label,
  hint,
  error,
  children,
}: {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  children: ReactNode;
}) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {error ? <span className="error-text">{error}</span> : hint ? <span className="helper">{hint}</span> : null}
    </label>
  );
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <div className="segmented">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          className={option.value === value ? "active" : ""}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function Modal({
  open,
  title,
  children,
  onClose,
  footer,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  onClose: () => void;
  footer?: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-title">{title}</div>
        <div className="stack">{children}</div>
        {footer ? <div className="modal-actions">{footer}</div> : null}
      </div>
    </div>
  );
}

export function PageGrid({ children, columns }: { children: ReactNode; columns: string }) {
  return (
    <div className="grid" style={{ gridTemplateColumns: columns }}>
      {children}
    </div>
  );
}
