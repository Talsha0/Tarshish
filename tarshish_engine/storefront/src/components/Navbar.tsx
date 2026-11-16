"use client";

// Top bar: brand, search, language switch (HE / EN) and the saved-deals button with its
// counter. On phones the search field drops to its own full-width row.

import { AnimatePresence, motion } from "framer-motion";
import { Check, Heart, Search, X } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { LANGS, getDictionary, switchLangPath, type Lang } from "@/lib/i18n";

type NavbarProps = {
  lang: Lang;
  query: string;
  onQueryChange: (query: string) => void;
  /** "Search in Sports" on a category hub; the general hint elsewhere. */
  searchPlaceholder: string;
  savedCount: number;
  onOpenSaved: () => void;
};

const LANG_LABEL: Record<Lang, string> = { he: "עב", en: "EN" };
const LANG_NAME: Record<Lang, string> = { he: "עברית", en: "English" };

export function Navbar({ lang, query, onQueryChange, searchPlaceholder, savedCount, onOpenSaved }: NavbarProps) {
  const t = getDictionary(lang);
  const pathname = usePathname();
  const router = useRouter();

  return (
    <header className="sticky top-0 z-40 border-b border-line bg-surface/95 backdrop-blur supports-[backdrop-filter]:bg-surface/85">
      <a
        href="#deals"
        className="sr-only focus:not-sr-only focus:absolute focus:start-4 focus:top-3 focus:z-50 focus:rounded-lg focus:bg-ink focus:px-3 focus:py-2 focus:text-sm focus:font-semibold focus:text-white"
      >
        {t.nav.skip}
      </a>
      <div className="mx-auto flex max-w-shell flex-wrap items-center gap-x-4 gap-y-2.5 px-4 py-3 sm:px-6 lg:flex-nowrap lg:gap-x-8">
        <Link href={`/${lang}`} aria-label={t.nav.home} className="order-1 flex shrink-0 items-center gap-2 rounded-md" dir="ltr">
          <span aria-hidden="true" className="price-tag price-tag--mark text-sm">
            <Check size={13} strokeWidth={3.5} />
          </span>
          <span className="font-display text-2xl font-black leading-none tracking-tight text-spruce">FindHub</span>
        </Link>

        <form
          role="search"
          className="relative order-3 w-full lg:order-2 lg:mx-auto lg:w-auto lg:max-w-2xl lg:flex-1"
          onSubmit={(event) => {
            event.preventDefault();
            document.getElementById("deals")?.scrollIntoView({ block: "start" });
          }}
        >
          <label htmlFor="deal-search" className="sr-only">
            {t.nav.searchLabel}
          </label>
          <Search aria-hidden="true" size={18} className="pointer-events-none absolute start-3.5 top-1/2 -translate-y-1/2 text-ink-faint" />
          <input
            id="deal-search"
            type="search"
            inputMode="search"
            enterKeyHint="search"
            autoComplete="off"
            spellCheck={false}
            maxLength={80}
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder={searchPlaceholder}
            className="h-11 w-full rounded-[10px] border border-line-strong bg-paper pe-10 ps-11 text-base text-ink placeholder:text-ink-faint transition-colors hover:border-ink-faint focus:border-spruce-bright focus:bg-surface focus:outline-none focus:ring-2 focus:ring-spruce-bright/25"
          />
          {query ? (
            <button
              type="button"
              onClick={() => {
                onQueryChange("");
                document.getElementById("deal-search")?.focus();
              }}
              aria-label={t.nav.clearSearch}
              className="absolute end-1.5 top-1/2 grid size-8 -translate-y-1/2 place-items-center rounded-lg text-ink-soft hover:bg-line/60 hover:text-ink"
            >
              <X aria-hidden="true" size={16} />
            </button>
          ) : null}
        </form>

        <div className="order-2 ms-auto flex shrink-0 items-center gap-2 lg:order-3 lg:ms-0">
          <nav aria-label={t.nav.language} className="flex h-10 items-center rounded-[10px] border border-line bg-paper p-0.5">
            {LANGS.map((code) =>
              code === lang ? (
                <span
                  key={code}
                  aria-current="true"
                  className="grid h-full min-w-10 place-items-center rounded-lg bg-surface px-2 text-sm font-bold text-ink shadow-sm"
                >
                  <span className="sr-only">{LANG_NAME[code]}</span>
                  <span aria-hidden="true">{LANG_LABEL[code]}</span>
                </span>
              ) : (
                <a
                  key={code}
                  href={switchLangPath(pathname, code)}
                  hrefLang={code}
                  lang={code}
                  onClick={(event) => {
                    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                    // Same page, other language, with the current filters still in the address.
                    event.preventDefault();
                    router.push(switchLangPath(window.location.pathname, code) + window.location.search);
                  }}
                  className="grid h-full min-w-10 place-items-center rounded-lg px-2 text-sm font-semibold text-ink-soft transition-colors hover:text-ink"
                >
                  <span className="sr-only">{LANG_NAME[code]}</span>
                  <span aria-hidden="true">{LANG_LABEL[code]}</span>
                </a>
              ),
            )}
          </nav>

          <button
            type="button"
            onClick={onOpenSaved}
            aria-label={t.nav.savedCount(savedCount)}
            title={t.nav.saved}
            className="relative grid size-10 place-items-center rounded-[10px] border border-line bg-surface text-ink transition-colors hover:border-line-strong hover:text-flash"
          >
            <Heart aria-hidden="true" size={19} strokeWidth={2.1} className={savedCount > 0 ? "fill-flash text-flash" : ""} />
            <AnimatePresence>
              {savedCount > 0 ? (
                <motion.span
                  key={savedCount}
                  aria-hidden="true"
                  initial={{ scale: 0.4, opacity: 0 }}
                  animate={{ scale: 1, opacity: 1 }}
                  exit={{ scale: 0.4, opacity: 0 }}
                  transition={{ type: "spring", stiffness: 520, damping: 22 }}
                  className="absolute -end-1.5 -top-1.5 grid h-5 min-w-5 place-items-center rounded-full bg-ink px-1 text-[0.7rem] font-bold leading-none text-white"
                >
                  {savedCount > 99 ? "99+" : savedCount}
                </motion.span>
              ) : null}
            </AnimatePresence>
          </button>
        </div>
      </div>
    </header>
  );
}
