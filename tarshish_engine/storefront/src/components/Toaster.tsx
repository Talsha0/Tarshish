"use client";

// Short confirmations ("Coupon Copied", "Added to Saved Deals") that appear above the
// bottom edge and leave on their own. Announced politely to screen readers.

import { AnimatePresence, MotionConfig, motion } from "framer-motion";
import { Check, CircleAlert } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";

type Tone = "ok" | "error";
type Toast = { id: number; message: string; tone: Tone };
type ShowToast = (message: string, tone?: Tone) => void;

const ToastContext = createContext<ShowToast>(() => {});
const VISIBLE_MS = 2600;

/** True once the first page has hydrated; later client-side page loads may animate their entrance. */
let hydrated = false;
export function hasHydrated(): boolean {
  return hydrated;
}

export function useToast(): ShowToast {
  return useContext(ToastContext);
}

export function Providers({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);
  const timers = useRef(new Set<number>());

  useEffect(() => {
    hydrated = true;
    const pending = timers.current;
    return () => pending.forEach((timer) => window.clearTimeout(timer));
  }, []);

  const show = useCallback<ShowToast>((message, tone = "ok") => {
    const id = nextId.current++;
    setToasts((current) => [...current.slice(-2), { id, message, tone }]);
    const timer = window.setTimeout(() => {
      timers.current.delete(timer);
      setToasts((current) => current.filter((toast) => toast.id !== id));
    }, VISIBLE_MS);
    timers.current.add(timer);
  }, []);

  return (
    <MotionConfig reducedMotion="user">
      <ToastContext.Provider value={show}>
        {children}
        <div
          role="status"
          aria-live="polite"
          className="pointer-events-none fixed inset-x-0 bottom-0 z-[60] flex flex-col items-center gap-2 px-4 pb-[max(1.25rem,env(safe-area-inset-bottom))]"
        >
          <AnimatePresence initial={false}>
            {toasts.map((toast) => (
              <motion.div
                key={toast.id}
                layout
                initial={{ opacity: 0, y: 18, scale: 0.94 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: 8, scale: 0.96 }}
                transition={{ type: "spring", stiffness: 500, damping: 34 }}
                className="pointer-events-auto flex max-w-sm items-center gap-2.5 rounded-xl bg-ink px-4 py-3 text-sm font-semibold text-white shadow-panel"
              >
                <span
                  className={`grid size-5 shrink-0 place-items-center rounded-full ${
                    toast.tone === "ok" ? "bg-tag text-tag-ink" : "bg-flash text-white"
                  }`}
                >
                  {toast.tone === "ok" ? <Check size={13} strokeWidth={3.5} /> : <CircleAlert size={13} strokeWidth={3} />}
                </span>
                {toast.message}
              </motion.div>
            ))}
          </AnimatePresence>
        </div>
      </ToastContext.Provider>
    </MotionConfig>
  );
}
