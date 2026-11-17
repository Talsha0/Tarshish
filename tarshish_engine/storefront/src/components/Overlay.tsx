"use client";

// The one dialog primitive behind Quick View, the redirect confirmation, the saved-deals
// drawer and the mobile filter sheet: blurred backdrop, spring-in panel, focus kept inside,
// Escape and backdrop click to close, page scroll locked while open. Overlays can stack
// (the redirect confirmation opens above Quick View); only the top one reacts to keys.

import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useRef, type ReactNode } from "react";

type Placement = "center" | "end";

type OverlayProps = {
  open: boolean;
  onClose: () => void;
  /** id of the element that names the dialog. */
  labelledBy: string;
  /** "center": a modal (a bottom sheet on phones). "end": a drawer on the trailing edge. */
  placement?: Placement;
  dir: "rtl" | "ltr";
  /** Classes for the panel itself: width, height, radius. */
  className?: string;
  children: ReactNode;
};

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
const SPRING = { type: "spring", stiffness: 420, damping: 36, mass: 0.9 } as const;

const openStack: symbol[] = [];
let scrollLocks = 0;

function lockScroll(): () => void {
  if (scrollLocks === 0) {
    const gap = window.innerWidth - document.documentElement.clientWidth;
    document.body.style.overflow = "hidden";
    if (gap > 0) document.body.style.paddingInlineEnd = `${gap}px`; // keep the page from shifting sideways
  }
  scrollLocks += 1;
  return () => {
    scrollLocks -= 1;
    if (scrollLocks === 0) {
      document.body.style.overflow = "";
      document.body.style.paddingInlineEnd = "";
    }
  };
}

function Panel({ onClose, labelledBy, placement = "center", dir, className = "", children }: Omit<OverlayProps, "open">) {
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    const token = Symbol("overlay");
    openStack.push(token);
    const unlock = lockScroll();
    const returnFocusTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const panel = panelRef.current;
    (panel?.querySelector<HTMLElement>("[data-autofocus]") ?? panel)?.focus({ preventScroll: true });

    const onKeyDown = (event: KeyboardEvent) => {
      if (openStack[openStack.length - 1] !== token || !panel) return;
      if (event.key === "Escape") {
        event.preventDefault();
        closeRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (element) => element.offsetParent !== null,
      );
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) {
        event.preventDefault();
        return;
      }
      const active = document.activeElement;
      if (event.shiftKey && (active === first || active === panel)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      } else if (!panel.contains(active)) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      openStack.splice(openStack.indexOf(token), 1);
      unlock();
      returnFocusTo?.focus({ preventScroll: true });
    };
  }, []);

  const drawer = placement === "end";
  const offscreen = dir === "rtl" ? "-100%" : "100%";
  return (
    <div
      className={`fixed inset-0 z-50 flex ${drawer ? "justify-end" : "items-end justify-center sm:items-center sm:p-6"}`}
    >
      <motion.div
        aria-hidden="true"
        className="absolute inset-0 bg-scrim/50 backdrop-blur-sm"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.2 }}
        onClick={onClose}
      />
      <motion.div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        tabIndex={-1}
        className={`relative flex flex-col bg-surface shadow-panel outline-none ${className}`}
        initial={drawer ? { x: offscreen } : { opacity: 0, y: 32, scale: 0.97 }}
        animate={drawer ? { x: 0 } : { opacity: 1, y: 0, scale: 1 }}
        exit={drawer ? { x: offscreen } : { opacity: 0, y: 24, scale: 0.98 }}
        transition={SPRING}
      >
        {children}
      </motion.div>
    </div>
  );
}

export function Overlay({ open, ...panel }: OverlayProps) {
  return <AnimatePresence>{open ? <Panel key="panel" {...panel} /> : null}</AnimatePresence>;
}
