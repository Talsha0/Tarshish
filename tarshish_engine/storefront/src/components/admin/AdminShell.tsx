"use client";

import { Gauge, ListOrdered, LogOut, Menu, Moon, Share2, Sun, SunMoon, Workflow, X } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { logout } from "@/app/admin/actions";

import { ConsoleDrawer, JobsProvider } from "./jobs";
import { ToastProvider } from "./ui";

const NAV = [
  { href: "/admin", label: "Overview", icon: Gauge },
  { href: "/admin/accounts", label: "Social accounts", icon: Share2 },
  { href: "/admin/pipelines", label: "Pipelines", icon: Workflow },
  { href: "/admin/queue", label: "Queue & jobs", icon: ListOrdered },
];

type Theme = "system" | "light" | "dark";
const THEME_KEY = "tarshish-admin-theme";

function ThemeSwitch() {
  const [theme, setTheme] = useState<Theme>("system");
  useEffect(() => {
    try {
      const saved = localStorage.getItem(THEME_KEY);
      if (saved === "light" || saved === "dark") setTheme(saved);
    } catch {
      /* storage unavailable: follow the system */
    }
  }, []);
  const choose = (next: Theme) => {
    setTheme(next);
    const root = document.documentElement;
    if (next === "system") delete root.dataset.theme;
    else root.dataset.theme = next;
    try {
      if (next === "system") localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, next);
    } catch {
      /* ignore */
    }
  };
  const options: { value: Theme; icon: typeof Sun; label: string }[] = [
    { value: "light", icon: Sun, label: "Light" },
    { value: "system", icon: SunMoon, label: "System" },
    { value: "dark", icon: Moon, label: "Dark" },
  ];
  return (
    <div className="flex rounded-lg border border-adm-line p-0.5" role="radiogroup" aria-label="Theme">
      {options.map(({ value, icon: Icon, label }) => (
        <button
          key={value}
          type="button"
          role="radio"
          aria-checked={theme === value}
          title={label}
          onClick={() => choose(value)}
          className={`flex flex-1 items-center justify-center rounded-md py-1 ${
            theme === value ? "bg-adm-raised text-adm-ink" : "text-adm-faint hover:text-adm-ink"
          }`}
        >
          <Icon className="h-3.5 w-3.5" />
        </button>
      ))}
    </div>
  );
}

function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <div className="flex h-full flex-col gap-4 p-4">
      <Link href="/admin" className="flex items-center gap-2" onClick={onNavigate}>
        <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-adm-accent text-sm font-bold text-adm-accent-ink">
          T
        </span>
        <span className="leading-tight">
          <span className="block text-sm font-semibold">Tarshish</span>
          <span className="block text-[11px] text-adm-faint">Admin control center</span>
        </span>
      </Link>
      <nav className="flex flex-col gap-0.5">
        {NAV.map(({ href, label, icon: Icon }) => {
          const active = href === "/admin" ? pathname === "/admin" : pathname.startsWith(href);
          return (
            <Link
              key={href}
              href={href}
              onClick={onNavigate}
              aria-current={active ? "page" : undefined}
              className={`flex items-center gap-2 rounded-lg px-2.5 py-2 text-sm ${
                active ? "bg-adm-accent/10 font-semibold text-adm-accent" : "text-adm-soft hover:bg-adm-raised hover:text-adm-ink"
              }`}
            >
              <Icon className="h-4 w-4" />
              {label}
            </Link>
          );
        })}
      </nav>
      <div className="mt-auto space-y-3">
        <ThemeSwitch />
        <Link href="/he" className="block text-xs text-adm-faint hover:text-adm-ink">
          ↗ Open the storefront
        </Link>
        <form action={logout}>
          <button type="submit" className="adm-btn w-full">
            <LogOut className="h-4 w-4" /> Log out
          </button>
        </form>
      </div>
    </div>
  );
}

export function AdminShell({ children }: { children: ReactNode }) {
  const [menu, setMenu] = useState(false);
  return (
    <ToastProvider>
      <JobsProvider>
        <aside className="fixed inset-y-0 left-0 z-30 hidden w-60 border-r border-adm-line bg-adm-panel lg:block">
          <Sidebar />
        </aside>
        <header className="sticky top-0 z-30 flex items-center gap-2 border-b border-adm-line bg-adm-panel px-4 py-2 lg:hidden">
          <button type="button" className="rounded-md p-1.5 hover:bg-adm-raised" onClick={() => setMenu(true)} aria-label="Open menu">
            <Menu className="h-5 w-5" />
          </button>
          <span className="text-sm font-semibold">Tarshish Admin</span>
        </header>
        {menu && (
          <div className="fixed inset-0 z-50 bg-black/40 lg:hidden" onClick={() => setMenu(false)}>
            <div className="h-full w-64 bg-adm-panel" onClick={(event) => event.stopPropagation()}>
              <div className="flex justify-end p-2">
                <button type="button" className="rounded-md p-1.5 hover:bg-adm-raised" onClick={() => setMenu(false)} aria-label="Close menu">
                  <X className="h-5 w-5" />
                </button>
              </div>
              <Sidebar onNavigate={() => setMenu(false)} />
            </div>
          </div>
        )}
        <main className="min-h-screen px-4 pb-28 pt-5 sm:px-6 lg:ml-60 lg:px-8">
          <div className="mx-auto max-w-7xl">{children}</div>
        </main>
        <ConsoleDrawer />
      </JobsProvider>
    </ToastProvider>
  );
}
