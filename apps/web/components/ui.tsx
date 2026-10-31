"use client";

import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";

/* --------------------------------------------------------------- primitives */
export function Card({
  title,
  subtitle,
  right,
  children,
  className,
  dense,
}: {
  title?: React.ReactNode;
  subtitle?: React.ReactNode;
  right?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  dense?: boolean;
}) {
  return (
    <section
      className={clsx(
        "rounded-lg border border-terminal-border bg-terminal-card/70 backdrop-blur-[2px]",
        "shadow-[0_0_0_1px_rgba(15,23,42,0.6),0_10px_30px_-18px_rgba(0,0,0,0.9)]",
        className
      )}
    >
      {(title || right) && (
        <header className="flex items-start justify-between gap-3 border-b border-terminal-border/80 px-4 py-3">
          <div className="min-w-0">
            {title && <h2 className="truncate text-[13px] font-semibold tracking-wide text-slate-100">{title}</h2>}
            {subtitle && <p className="mt-0.5 truncate text-[11px] text-slate-500">{subtitle}</p>}
          </div>
          {right && <div className="shrink-0">{right}</div>}
        </header>
      )}
      <div className={dense ? "p-0" : "p-4"}>{children}</div>
    </section>
  );
}

type Tone = "neutral" | "good" | "bad" | "warn" | "info" | "muted";

const TONE_CLASS: Record<Tone, string> = {
  neutral: "border-slate-600/60 bg-slate-500/10 text-slate-300",
  good: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  bad: "border-rose-500/40 bg-rose-500/10 text-rose-300",
  warn: "border-amber-500/40 bg-amber-500/10 text-amber-300",
  info: "border-sky-500/40 bg-sky-500/10 text-sky-300",
  muted: "border-slate-700/70 bg-slate-800/40 text-slate-400",
};

export function Badge({ tone = "neutral", children, className }: { tone?: Tone; children: React.ReactNode; className?: string }) {
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider leading-4",
        TONE_CLASS[tone],
        className
      )}
    >
      {children}
    </span>
  );
}

export function statusTone(status: string | undefined | null): Tone {
  switch ((status || "").toUpperCase()) {
    case "FILLED":
    case "CONNECTED":
    case "ACTIVE":
    case "SYNCED":
    case "READY":
    case "SUCCEEDED":
    case "OK":
    case "OPEN":
      return "good";
    case "REJECTED":
    case "FAILED":
    case "MISMATCH":
    case "CRITICAL":
    case "LOCKED":
    case "RED":
      return "bad";
    case "UNKNOWN":
    case "PENDING":
    case "PAUSED":
    case "DEGRADED":
    case "WARNING":
    case "RECOVERY_MODE":
    case "YELLOW":
      return "warn";
    case "CANCELED":
    case "EXPIRED":
    case "CLOSED":
      return "muted";
    default:
      return "neutral";
  }
}

export function StatusBadge({ status }: { status: string | undefined | null }) {
  const text = (status || "UNKNOWN").toUpperCase();
  return <Badge tone={statusTone(text)}>{text}</Badge>;
}

export function Button({
  children,
  onClick,
  variant = "default",
  size = "md",
  disabled,
  loading,
  className,
  type = "button",
  title,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  variant?: "default" | "primary" | "danger" | "ghost" | "warn";
  size?: "sm" | "md";
  disabled?: boolean;
  loading?: boolean;
  className?: string;
  type?: "button" | "submit";
  title?: string;
}) {
  const variants = {
    default: "border-terminal-border bg-slate-800/60 text-slate-200 hover:bg-slate-700/70",
    primary: "border-emerald-500/50 bg-emerald-500/15 text-emerald-200 hover:bg-emerald-500/25",
    danger: "border-rose-500/50 bg-rose-500/15 text-rose-200 hover:bg-rose-500/25",
    warn: "border-amber-500/50 bg-amber-500/15 text-amber-200 hover:bg-amber-500/25",
    ghost: "border-transparent bg-transparent text-slate-400 hover:bg-slate-800/60 hover:text-slate-200",
  } as const;
  return (
    <button
      type={type}
      title={title}
      onClick={onClick}
      disabled={disabled || loading}
      className={clsx(
        "inline-flex items-center justify-center gap-1.5 rounded border font-medium transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-40",
        size === "sm" ? "px-2 py-1 text-[11px]" : "px-3 py-1.5 text-xs",
        variants[variant],
        className
      )}
    >
      {loading && <span className="h-3 w-3 animate-spin rounded-full border border-current border-t-transparent" />}
      {children}
    </button>
  );
}

export function Stat({
  label,
  value,
  sub,
  tone = "neutral",
  mono = true,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  tone?: Tone;
  mono?: boolean;
}) {
  const valueColor =
    tone === "good" ? "text-emerald-300" : tone === "bad" ? "text-rose-300" : tone === "warn" ? "text-amber-300" : "text-slate-100";
  return (
    <div className="rounded-md border border-terminal-border/70 bg-terminal-panel/60 px-3 py-2.5">
      <div className="text-[10px] font-medium uppercase tracking-wider text-slate-500">{label}</div>
      <div className={clsx("mt-1 text-[17px] font-semibold leading-tight", mono && "font-mono", valueColor)}>{value}</div>
      {sub && <div className="mt-1 text-[10px] leading-4 text-slate-500">{sub}</div>}
    </div>
  );
}

export function DataAge({ iso, stale }: { iso: string | null | undefined; stale?: boolean }) {
  const [, force] = useState(0);
  useEffect(() => {
    const t = setInterval(() => force((n) => n + 1), 5000);
    return () => clearInterval(t);
  }, []);
  if (!iso) return <Badge tone="warn">数据未知 / NO DATA</Badge>;
  const age = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  const tone: Tone = stale || age > 300 ? "warn" : "good";
  return (
    <Badge tone={tone}>
      {stale || age > 300 ? "数据过期" : "数据新鲜"} · {age < 60 ? `${age}s` : `${Math.floor(age / 60)}m`} · {new Date(iso).toISOString().slice(11, 19)}Z
    </Badge>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="py-8 text-center text-xs text-slate-500">{children}</div>;
}

export function ErrorLine({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  if (!error) return null;
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="flex items-center justify-between gap-3 rounded border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-[11px] text-rose-200">
      <span className="truncate">数据不可用 / DATA UNKNOWN — {msg}</span>
      {onRetry && (
        <Button size="sm" variant="ghost" onClick={onRetry}>
          重试
        </Button>
      )}
    </div>
  );
}

export function Table({
  columns,
  rows,
  empty = "暂无数据",
  rowKey,
  onRowClick,
}: {
  columns: Array<{ key: string; header: React.ReactNode; align?: "left" | "right" | "center"; className?: string }>;
  rows: Array<Record<string, React.ReactNode>>;
  empty?: string;
  rowKey?: (row: Record<string, React.ReactNode>, i: number) => string;
  onRowClick?: (row: Record<string, React.ReactNode>) => void;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-[11px]">
        <thead>
          <tr className="border-b border-terminal-border text-[10px] uppercase tracking-wider text-slate-500">
            {columns.map((c) => (
              <th
                key={c.key}
                className={clsx(
                  "whitespace-nowrap px-2.5 py-2 font-medium",
                  c.align === "right" ? "text-right" : c.align === "center" ? "text-center" : "text-left",
                  c.className
                )}
              >
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={columns.length} className="px-3 py-8 text-center text-xs text-slate-500">
                {empty}
              </td>
            </tr>
          )}
          {rows.map((row, i) => (
            <tr
              key={rowKey ? rowKey(row, i) : i}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              className={clsx(
                "border-b border-terminal-border/40 transition-colors last:border-0",
                onRowClick && "cursor-pointer hover:bg-slate-800/40"
              )}
            >
              {columns.map((c) => (
                <td
                  key={c.key}
                  className={clsx(
                    "whitespace-nowrap px-2.5 py-1.5 text-slate-300",
                    c.align === "right" ? "text-right font-mono" : c.align === "center" ? "text-center" : "text-left"
                  )}
                >
                  {row[c.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Modal({
  open,
  onClose,
  title,
  children,
  width = "max-w-lg",
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  width?: string;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div
        className={clsx("w-full rounded-lg border border-terminal-border bg-terminal-card shadow-2xl", width)}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-terminal-border px-4 py-3">
          <h3 className="text-[13px] font-semibold text-slate-100">{title}</h3>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-200" aria-label="close">
            ✕
          </button>
        </div>
        <div className="max-h-[75vh] overflow-y-auto p-4">{children}</div>
      </div>
    </div>
  );
}

export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: React.ReactNode;
  hint?: string;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[10px] font-medium uppercase tracking-wider text-slate-500">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[10px] text-slate-500">{hint}</span>}
    </label>
  );
}

export const inputClass =
  "w-full rounded border border-terminal-border bg-terminal-panel px-2.5 py-1.5 font-mono text-xs text-slate-200 outline-none placeholder:text-slate-600 focus:border-emerald-500/60";

/* ------------------------------------------------------------------- toasts */
type Toast = { id: number; tone: Tone; text: string };
const ToastCtx = createContext<{ push: (tone: Tone, text: string) => void }>({ push: () => {} });

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const idRef = useRef(1);
  const push = useCallback((tone: Tone, text: string) => {
    const id = idRef.current++;
    setToasts((cur) => [...cur, { id, tone, text }]);
    setTimeout(() => setToasts((cur) => cur.filter((t) => t.id !== id)), 6000);
  }, []);
  const value = useMemo(() => ({ push }), [push]);
  return (
    <ToastCtx.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-[380px] flex-col gap-2">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={clsx(
              "pointer-events-auto rounded border px-3 py-2 text-[11px] leading-relaxed shadow-lg",
              TONE_CLASS[t.tone]
            )}
          >
            {t.text}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export const useToast = () => useContext(ToastCtx);

/* ------------------------------------------------------------------- hooks */
export function usePoll<T>(
  fn: () => Promise<T>,
  ms = 5000
): { data: T | null; error: Error | null; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const alive = useRef(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const reload = useCallback(() => {
    fnRef
      .current()
      .then((d) => {
        if (alive.current) {
          setData(d);
          setError(null);
        }
      })
      .catch((e) => {
        if (alive.current) setError(e instanceof Error ? e : new Error(String(e)));
      })
      .finally(() => {
        if (alive.current) setLoading(false);
      });
  }, []);

  useEffect(() => {
    alive.current = true;
    reload();
    if (!ms) return () => undefined;
    const t = setInterval(reload, ms);
    return () => {
      alive.current = false;
      clearInterval(t);
    };
  }, [ms, reload]);

  return { data, error, loading, reload };
}

export function useAction() {
  const [busy, setBusy] = useState<string | null>(null);
  const { push } = useToast();
  const run = useCallback(
    async (key: string, fn: () => Promise<unknown>, success?: string) => {
      setBusy(key);
      try {
        const result = await fn();
        if (success) push("good", success);
        return result;
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        const detail = (err as { detail?: unknown })?.detail;
        push("bad", `${msg}${detail ? ` — ${typeof detail === "string" ? detail : JSON.stringify(detail).slice(0, 300)}` : ""}`);
        return null;
      } finally {
        setBusy(null);
      }
    },
    [push]
  );
  return { busy, run };
}
