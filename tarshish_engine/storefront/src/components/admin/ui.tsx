"use client";

// Small building blocks shared by every admin page.

import { CircleAlert, CircleCheck, LoaderCircle, X } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { adminApi, errorText } from "@/lib/admin/client";

// --------------------------------------------------------------------------- //
// Data loading
// --------------------------------------------------------------------------- //
export function useResource<T>(path: string | null, refreshMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(Boolean(path));
  const alive = useRef(true);

  const reload = useCallback(async () => {
    if (!path) return;
    try {
      const result = await adminApi<T>(path);
      if (alive.current) {
        setData(result);
        setError(null);
      }
    } catch (err) {
      if (alive.current) setError(errorText(err));
    } finally {
      if (alive.current) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    alive.current = true;
    setLoading(Boolean(path));
    void reload();
    if (!refreshMs) return () => void (alive.current = false);
    const timer = window.setInterval(() => void reload(), refreshMs);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
    };
  }, [reload, refreshMs, path]);

  return { data, error, loading, reload, setData };
}

// --------------------------------------------------------------------------- //
// Toasts
// --------------------------------------------------------------------------- //
type Toast = { id: number; kind: "ok" | "error"; text: string };
const ToastContext = createContext<(kind: Toast["kind"], text: string) => void>(() => undefined);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((kind: Toast["kind"], text: string) => {
    const id = Date.now() + Math.random();
    setToasts((list) => [...list.slice(-3), { id, kind, text }]);
    window.setTimeout(() => setToasts((list) => list.filter((toast) => toast.id !== id)), kind === "error" ? 9000 : 4500);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed right-4 top-4 z-[60] flex w-[min(26rem,calc(100vw-2rem))] flex-col gap-2" aria-live="polite">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={`pointer-events-auto flex items-start gap-2 rounded-lg border px-3 py-2 text-sm shadow-lg ${
              toast.kind === "ok" ? "border-adm-ok/40 bg-adm-panel" : "border-adm-bad/40 bg-adm-panel"
            }`}
          >
            {toast.kind === "ok" ? (
              <CircleCheck className="mt-0.5 h-4 w-4 shrink-0 text-adm-ok" />
            ) : (
              <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-adm-bad" />
            )}
            <span className="min-w-0 break-words">{toast.text}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}

// --------------------------------------------------------------------------- //
// Visual atoms
// --------------------------------------------------------------------------- //
const TONES = {
  ok: "bg-adm-ok/10 text-adm-ok border-adm-ok/30",
  warn: "bg-adm-warn/10 text-adm-warn border-adm-warn/30",
  bad: "bg-adm-bad/10 text-adm-bad border-adm-bad/30",
  neutral: "bg-adm-raised text-adm-soft border-adm-line",
  accent: "bg-adm-accent/10 text-adm-accent border-adm-accent/30",
} as const;
export type Tone = keyof typeof TONES;

export function Badge({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-semibold ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}

export function statusTone(status: string): Tone {
  switch (status) {
    case "ready":
    case "Ready":
    case "Configured":
    case "SUCCEEDED":
    case "PUBLISHED":
    case "ok":
      return "ok";
    case "RUNNING":
    case "QUEUED":
    case "PENDING":
      return "accent";
    case "disabled":
    case "warning":
      return "warn";
    default:
      return "bad";
  }
}

const STATUS_TEXT: Record<string, string> = {
  ready: "Ready",
  missing_credentials: "Credentials missing",
  disabled: "Disabled",
  no_publisher: "No publisher",
};

export function StatusBadge({ status, title }: { status: string; title?: string }) {
  return (
    <Badge tone={statusTone(status)} title={title}>
      {(status === "RUNNING" || status === "QUEUED") && <LoaderCircle className="h-3 w-3 animate-spin" />}
      {STATUS_TEXT[status] ?? status}
    </Badge>
  );
}

export function EnvChip({ name, status, required = true }: { name: string | null; status: string; required?: boolean }) {
  const ok = status === "Configured";
  return (
    <span
      className={`adm-mono inline-flex max-w-full items-center gap-1 break-all rounded-md border px-1.5 py-0.5 text-[11px] ${
        ok ? TONES.ok : required ? TONES.bad : TONES.neutral
      }`}
      title={ok ? "Set in the backend environment" : required ? "Missing from the backend environment" : "Optional, not set"}
    >
      {ok ? "✓" : required ? "✗" : "·"} {name ?? "—"}
    </span>
  );
}

export function Panel({
  title,
  subtitle,
  actions,
  children,
  className = "",
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`adm-card p-4 ${className}`}>
      {(title || actions) && (
        <header className="mb-3 flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            {title && <h2 className="text-sm font-semibold">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-adm-soft">{subtitle}</p>}
          </div>
          {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function ErrorNote({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-adm-bad/30 bg-adm-bad/10 px-3 py-2 text-sm text-adm-bad">
      <CircleAlert className="h-4 w-4 shrink-0" />
      <span className="min-w-0 flex-1 break-words">{error}</span>
      {onRetry && (
        <button type="button" className="adm-btn" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-6 text-sm text-adm-soft">
      <LoaderCircle className="h-4 w-4 animate-spin" /> {label}
    </div>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-1 max-w-3xl text-sm text-adm-soft">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Modal({
  title,
  onClose,
  children,
  wide = false,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/50 p-4 sm:p-8" role="dialog" aria-modal="true" aria-label={title}>
      <div className={`adm-card w-full ${wide ? "max-w-3xl" : "max-w-lg"} shadow-2xl`}>
        <div className="flex items-center justify-between border-b border-adm-line px-4 py-3">
          <h2 className="text-base font-semibold">{title}</h2>
          <button type="button" className="rounded-md p-1 text-adm-soft hover:bg-adm-raised" onClick={onClose} aria-label="Close">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="p-4">{children}</div>
      </div>
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="block">
      <span className="adm-label">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[11px] text-adm-faint">{hint}</span>}
    </label>
  );
}

export function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (value: boolean) => void; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition ${
        checked ? "border-adm-accent bg-adm-accent" : "border-adm-line bg-adm-raised"
      }`}
    >
      <span
        className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow transition ${checked ? "translate-x-[18px]" : "translate-x-[3px]"}`}
      />
    </button>
  );
}

/** Run an async action with a busy flag and toast feedback. */
export function useAction() {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const run = useCallback(
    async <T,>(key: string, action: () => Promise<T>, success?: string | ((result: T) => string)) => {
      setBusy(key);
      try {
        const result = await action();
        if (success) toast("ok", typeof success === "function" ? success(result) : success);
        return result;
      } catch (err) {
        toast("error", errorText(err));
        return undefined;
      } finally {
        setBusy(null);
      }
    },
    [toast],
  );
  return { busy, run };
}
