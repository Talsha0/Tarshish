"use client";

// The band under the Navbar. On the main storefront it states what FindHub is and shows the
// biggest live price drop; on a category hub it carries that category's own headline,
// its deal count and links to the other collections (the page people land on from a bio link).

import { ArrowRight } from "lucide-react";
import Link from "next/link";

import type { Catalog, Product } from "@/lib/api";
import { formatNumber, formatRating, hubPath, productPath, titleOf } from "@/lib/format";
import { categoryName, getDictionary, hubCopy, type Lang } from "@/lib/i18n";

import { PriceBlock, ProductImage, RatingPill, StorePill } from "./ProductBits";

type HeroProps = {
  lang: Lang;
  /** The hub's category, or `null` on the main storefront. */
  category: string | null;
  catalog: Catalog | null;
  featured: Product | null;
  onOpen: (product: Product) => void;
};

export function Hero({ lang, category, catalog, featured, onOpen }: HeroProps) {
  const t = getDictionary(lang);
  const standards = catalog?.standards ?? { min_rating: 4.5, min_orders: 200 };
  const hub = category ? hubCopy(lang, category) : null;
  const hubCount = category ? (catalog?.categories.find((entry) => entry.key === category)?.products ?? null) : null;
  const others = category ? (catalog?.categories.filter((entry) => entry.key !== category) ?? []) : [];

  return (
    <section className="bg-hero text-white">
      <div className="mx-auto grid max-w-shell items-center gap-8 px-4 py-9 sm:px-6 sm:py-12 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,0.85fr)] lg:gap-14 lg:py-14">
        <div>
          {hub ? (
            <Link
              href={hubPath(lang, null)}
              className="mb-4 inline-flex items-center gap-1.5 rounded-md text-sm font-semibold text-white/75 underline decoration-white/30 underline-offset-4 hover:text-white"
            >
              <ArrowRight aria-hidden="true" size={15} className="rotate-180 rtl:rotate-0" />
              {t.categories.allDeals}
            </Link>
          ) : null}
          <h1 className="max-w-[18ch] text-balance font-display text-3xl font-black leading-[1.08] sm:text-4xl lg:text-5xl">
            {hub ? hub.title : t.hero.headline}
          </h1>
          <p className="mt-4 max-w-[58ch] text-base text-white/80 sm:text-lg">
            {hub
              ? hub.blurb
              : t.hero.blurb(formatRating(standards.min_rating), formatNumber(standards.min_orders, lang))}
          </p>
          {hub ? (
            <div className="mt-6 flex flex-wrap items-center gap-x-3 gap-y-2.5">
              {hubCount !== null ? (
                <span className="rounded-full bg-tag px-3 py-1 text-sm font-bold text-tag-ink">
                  {t.categories.checkedDeals(formatNumber(hubCount, lang), hubCount)}
                </span>
              ) : null}
              {others.length > 0 ? (
                <nav aria-label={t.categories.more} className="flex flex-wrap items-center gap-2">
                  <span className="text-sm text-white/65">{t.categories.more}</span>
                  {others.map((entry) => (
                    <Link
                      key={entry.key}
                      href={hubPath(lang, entry.key)}
                      className="rounded-full border border-white/25 px-3 py-1 text-sm font-semibold text-white transition-colors hover:border-white hover:bg-white/10"
                    >
                      {categoryName(lang, entry.key)}
                    </Link>
                  ))}
                </nav>
              ) : null}
            </div>
          ) : null}
        </div>

        {featured ? (
          <article className="group relative flex items-stretch gap-4 rounded-2xl bg-surface p-3.5 text-ink shadow-panel sm:p-4">
            <div className="relative w-28 shrink-0 self-stretch overflow-hidden rounded-xl bg-paper sm:w-40">
              <ProductImage
                src={featured.image_urls[0]}
                alt=""
                eager
                className="absolute inset-0 size-full transition-transform duration-500 ease-out group-hover:scale-[1.05] motion-reduce:transition-none"
              />
            </div>
            <div className="flex min-w-0 flex-1 flex-col items-start gap-2 py-0.5">
              <p className="text-sm font-bold text-spruce-bright">
                {category ? t.hero.featuredIn(categoryName(lang, category)) : t.hero.featured}
              </p>
              <h2 className="text-base font-semibold leading-snug">
                <a
                  href={productPath(lang, featured.id)}
                  onClick={(event) => {
                    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                    event.preventDefault();
                    onOpen(featured);
                  }}
                  className="line-clamp-2 after:absolute after:inset-0 after:rounded-2xl"
                >
                  <span dir="auto">{titleOf(featured, lang)}</span>
                </a>
              </h2>
              <div className="flex flex-wrap items-center gap-2">
                <StorePill store={featured.store} />
                <RatingPill rating={featured.rating} lang={lang} />
              </div>
              <div className="mt-auto pt-1.5">
                <PriceBlock product={featured} lang={lang} />
              </div>
            </div>
          </article>
        ) : null}
      </div>
    </section>
  );
}
