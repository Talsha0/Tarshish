"use client";

// Light / dark switch for the storefront. With no choice saved the site follows the system
// setting; pressing the button pins the other theme on <html data-theme> and in localStorage.
// Pressing it back to what the system already shows forgets the choice, so the site follows
// the system again. THEME_SCRIPT applies the saved choice before the first paint.

import { Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

export const THEME_KEY = "findhub-theme";

/** Inline <head> script: the saved theme before the first paint, so dark mode never flashes light. */
export const THEME_SCRIPT = `try{var t=localStorage.getItem("${THEME_KEY}");if(t==="dark"||t==="light")document.documentElement.dataset.theme=t}catch(e){}`;

const DARK_QUERY = "(prefers-color-scheme: dark)";

function systemIsDark(): boolean {
  return window.matchMedia(DARK_QUERY).matches;
}

function isDarkNow(): boolean {
  const forced = document.documentElement.dataset.theme;
  return forced ? forced === "dark" : systemIsDark();
}

export function ThemeToggle({ label }: { label: string }) {
  // Unknown until mounted: the server cannot see the visitor's theme. The icon itself is
  // picked by CSS (`dark:`), so it is right from the first paint either way.
  const [dark, setDark] = useState<boolean | undefined>(undefined);

  useEffect(() => {
    setDark(isDarkNow());
    const media = window.matchMedia(DARK_QUERY);
    const onChange = () => setDark(isDarkNow());
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  const toggle = () => {
    const next = !isDarkNow();
    const root = document.documentElement;
    const pinned = next === systemIsDark() ? null : next ? "dark" : "light";
    if (pinned) root.dataset.theme = pinned;
    else delete root.dataset.theme;
    try {
      if (pinned) localStorage.setItem(THEME_KEY, pinned);
      else localStorage.removeItem(THEME_KEY);
    } catch {
      // Storage blocked (private mode): the choice lasts for this page only.
    }
    setDark(next);
  };

  return (
    <button
      type="button"
      onClick={toggle}
      aria-pressed={dark}
      aria-label={label}
      title={label}
      className="grid size-10 place-items-center rounded-[10px] border border-line bg-surface text-ink transition-colors hover:border-line-strong hover:text-spruce"
    >
      <Moon aria-hidden="true" size={18} strokeWidth={2.1} className="dark:hidden" />
      <Sun aria-hidden="true" size={19} strokeWidth={2.1} className="hidden dark:block" />
    </button>
  );
}
