"use client";

// Shown for addresses that do not exist: an unknown category, a deal that is no longer listed.

import { SearchX } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { DEFAULT_LANG, getDictionary, isLang } from "@/lib/i18n";

export default function NotFound() {
  const segment = usePathname().split("/")[1] ?? "";
  const lang = isLang(segment) ? segment : DEFAULT_LANG;
  const t = getDictionary(lang);
  return (
    <main className="mx-auto flex min-h-dvh max-w-md flex-col items-center justify-center gap-3 px-6 text-center">
      <span className="grid size-14 place-items-center rounded-full bg-surface text-ink-faint">
        <SearchX aria-hidden="true" size={26} />
      </span>
      <h1 className="font-display text-3xl font-black text-ink">{t.notFound.title}</h1>
      <p className="text-base text-ink-soft">{t.notFound.body}</p>
      <Link
        href={`/${lang}`}
        className="mt-3 inline-flex h-12 items-center rounded-xl bg-spruce px-6 text-base font-bold text-white transition-colors hover:bg-spruce-deep"
      >
        {t.categories.allDeals}
      </Link>
    </main>
  );
}
