"use client";

// The pill bar on the main storefront: "All" plus one pill per active category, each with
// its deal count. Choosing a pill filters the grid in place. On phones the row scrolls sideways.

import type { Catalog } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { categoryName, getDictionary, type Lang } from "@/lib/i18n";

type CategoryBarProps = {
  lang: Lang;
  catalog: Catalog;
  selected: string | null;
  onSelect: (category: string | null) => void;
};

export function CategoryBar({ lang, catalog, selected, onSelect }: CategoryBarProps) {
  const t = getDictionary(lang);
  const pills: { key: string | null; label: string; count: number }[] = [
    { key: null, label: t.categories.all, count: catalog.total },
    ...catalog.categories.map((entry) => ({ key: entry.key, label: categoryName(lang, entry.key), count: entry.products })),
  ];

  return (
    <div role="group" aria-label={t.footer.collections} className="scrollbar-none -mx-4 flex gap-2 overflow-x-auto px-4 sm:mx-0 sm:flex-wrap sm:px-0">
      {pills.map((pill) => {
        const active = pill.key === selected;
        return (
          <button
            key={pill.key ?? "all"}
            type="button"
            aria-pressed={active}
            onClick={() => onSelect(pill.key)}
            className={`inline-flex h-10 shrink-0 items-center gap-2 rounded-full border px-4 text-sm font-bold transition-colors ${
              active
                ? "border-spruce bg-spruce text-white"
                : "border-line-strong bg-surface text-ink hover:border-spruce hover:text-spruce"
            }`}
          >
            {pill.label}
            <span className={`text-xs font-semibold tabular-nums ${active ? "text-white/75" : "text-ink-faint"}`}>
              {formatNumber(pill.count, lang)}
            </span>
          </button>
        );
      })}
    </div>
  );
}
