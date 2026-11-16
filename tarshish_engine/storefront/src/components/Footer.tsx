// Who runs this catalogue, how it earns money, and the way into each collection.

import Link from "next/link";

import type { Catalog } from "@/lib/api";
import { hubPath } from "@/lib/format";
import { categoryName, getDictionary, type Lang } from "@/lib/i18n";

export function Footer({ lang, catalog }: { lang: Lang; catalog: Catalog | null }) {
  const t = getDictionary(lang);
  const categories = catalog?.categories ?? [];
  return (
    <footer className="mt-16 border-t border-line bg-surface">
      <div className="mx-auto grid max-w-shell gap-8 px-4 py-10 sm:px-6 md:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <div className="flex max-w-[62ch] flex-col gap-2.5 text-sm text-ink-soft">
          <p className="font-display text-xl font-black text-spruce">FindHub</p>
          <p>{t.footer.about}</p>
          <p>{t.footer.disclosure}</p>
          <p>{t.footer.prices}</p>
        </div>
        {categories.length > 0 ? (
          <nav aria-label={t.footer.collections}>
            <h2 className="mb-3 text-sm font-bold text-ink">{t.footer.collections}</h2>
            <ul className="flex flex-col gap-2 text-sm">
              <li>
                <Link href={hubPath(lang, null)} className="font-semibold text-ink-soft underline-offset-4 hover:text-spruce hover:underline">
                  {t.categories.allDeals}
                </Link>
              </li>
              {categories.map((entry) => (
                <li key={entry.key}>
                  <Link
                    href={hubPath(lang, entry.key)}
                    className="font-semibold text-ink-soft underline-offset-4 hover:text-spruce hover:underline"
                  >
                    {categoryName(lang, entry.key)}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        ) : null}
      </div>
    </footer>
  );
}
