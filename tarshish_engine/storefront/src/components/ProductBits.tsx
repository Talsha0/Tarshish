"use client";

// Small pieces shared by the product card, Quick View, the hero and the saved-deals drawer:
// the price label, the product image, the store pill and the trust badges.

import { BadgeCheck, PackageSearch, Star, TrendingUp, Users, Zap } from "lucide-react";
import { useState } from "react";

import type { Product, SocialProof, Store } from "@/lib/api";
import { formatNumber, formatRating, money } from "@/lib/format";
import { getDictionary, type Lang } from "@/lib/i18n";

type PriceFields = Pick<Product, "price" | "original_price" | "discount_percent" | "currency">;

const TAG_SIZE = { sm: "text-base", md: "text-xl", lg: "text-3xl" } as const;

/** Discount flag and struck-through old price above the yellow shelf label with the price to pay. */
export function PriceBlock({ product, lang, size = "md" }: { product: PriceFields; lang: Lang; size?: keyof typeof TAG_SIZE }) {
  const t = getDictionary(lang);
  const now = money(product.price, product.currency, lang);
  const was = product.original_price ? money(product.original_price, product.currency, lang) : null;
  return (
    <div className="flex flex-col items-start gap-1.5">
      {was && product.discount_percent ? (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span
            className={`inline-flex items-center gap-1 rounded-[4px] bg-ink font-bold text-tag ${
              size === "lg" ? "px-2 py-1 text-sm" : "px-1.5 py-0.5 text-xs"
            }`}
          >
            <span dir="ltr">-{product.discount_percent}%</span>
            <span>{t.card.sale}</span>
          </span>
          <s className={`text-ink-faint ${size === "lg" ? "text-base" : "text-sm"}`}>
            <span className="sr-only">{t.card.was(was.text)}</span>
            <span aria-hidden="true" dir="ltr">
              {was.text}
            </span>
          </s>
        </div>
      ) : null}
      <span className={`price-tag ${TAG_SIZE[size]}`}>
        <span className="sr-only">{t.card.now(now.text)}</span>
        <span aria-hidden="true" dir="ltr" className="inline-flex items-baseline gap-[0.1em]">
          <span className="price-tag__symbol">{now.symbol}</span>
          {now.amount}
        </span>
      </span>
    </div>
  );
}

/** A product photo that never leaves a broken-image icon behind. */
export function ProductImage({
  src,
  alt,
  eager = false,
  className = "",
}: {
  src: string | undefined;
  alt: string;
  eager?: boolean;
  className?: string;
}) {
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  if (!src || failedSrc === src) {
    return (
      <div role="img" aria-label={alt} className={`grid place-items-center bg-spruce-tint text-spruce/45 ${className}`}>
        <PackageSearch aria-hidden="true" className="size-1/4 max-h-12 max-w-12" strokeWidth={1.5} />
      </div>
    );
  }
  return (
    // Product photos come from marketplace CDNs of any host, so they are plain <img> elements
    // (sized by their container, lazy by default) rather than routed through the Next optimizer.
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={src}
      alt={alt}
      loading={eager ? "eager" : "lazy"}
      decoding="async"
      draggable={false}
      onError={() => setFailedSrc(src)}
      className={`object-cover ${className}`}
    />
  );
}

const STORE_DOT: Record<string, string> = { aliexpress: "#E62E04", temu: "#FB7701", amazon: "#FF9900" };

export function StorePill({ store, className = "" }: { store: Store; className?: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border border-line bg-surface/95 px-2 py-0.5 text-xs font-semibold text-ink ${className}`}
    >
      <span aria-hidden="true" className="size-1.5 rounded-full" style={{ background: STORE_DOT[store.key] ?? "#0F4C3A" }} />
      <span dir="ltr">{store.name}</span>
    </span>
  );
}

export function RatingPill({ rating, lang, className = "" }: { rating: number; lang: Lang; className?: string }) {
  const t = getDictionary(lang);
  const value = formatRating(rating);
  return (
    <span className={`inline-flex items-center gap-1 rounded-md bg-spruce-tint px-1.5 py-0.5 text-xs font-bold text-spruce ${className}`}>
      <Star aria-hidden="true" size={12} className="fill-current" strokeWidth={0} />
      <span className="sr-only">{t.card.ratingAria(value)}</span>
      <span aria-hidden="true" dir="ltr">
        {value}
      </span>
    </span>
  );
}

export function FlashBadge({ lang, className = "" }: { lang: Lang; className?: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full bg-flash py-0.5 pe-2.5 ps-2 text-xs font-bold text-white ${className}`}
    >
      <span aria-hidden="true" className="grid size-3.5 place-items-center rounded-full bg-white/20 motion-safe:animate-flash-pulse">
        <Zap size={10} className="fill-current" strokeWidth={0} />
      </span>
      {getDictionary(lang).card.badges.flash_deal}
    </span>
  );
}

export function CrowdBadge({ lang, className = "" }: { lang: Lang; className?: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border border-spruce/20 bg-surface px-2 py-0.5 text-xs font-bold text-spruce ${className}`}
    >
      <Users aria-hidden="true" size={12} strokeWidth={2.5} />
      {getDictionary(lang).card.badges.crowd_favorite}
    </span>
  );
}

export type Standards = { min_rating: number; min_orders: number };

export function VerifiedBadge({ lang, standards, className = "" }: { lang: Lang; standards: Standards; className?: string }) {
  const t = getDictionary(lang);
  const hint = t.card.verifiedHint(formatRating(standards.min_rating), formatNumber(standards.min_orders, lang));
  return (
    <span title={hint} className={`inline-flex items-center gap-1 text-xs font-semibold text-ink-soft ${className}`}>
      <BadgeCheck aria-hidden="true" size={14} className="shrink-0 text-spruce-bright" strokeWidth={2.25} />
      {t.card.badges.verified}
    </span>
  );
}

export function SocialProofLine({ kind, lang, className = "" }: { kind: SocialProof; lang: Lang; className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1 text-xs font-semibold text-ink-soft ${className}`}>
      <TrendingUp aria-hidden="true" size={13} className="shrink-0 text-spruce-bright" strokeWidth={2.5} />
      {getDictionary(lang).card.social[kind]}
    </span>
  );
}

/** The catalogue's free-text urgency label ("Limited Stock"), translated when it is a known one. */
export function urgencyText(label: string, lang: Lang): string {
  return getDictionary(lang).card.urgency[label] ?? label;
}

export function UrgencyChip({ label, lang, className = "" }: { label: string; lang: Lang; className?: string }) {
  return (
    <span className={`inline-flex items-center rounded-full bg-ink px-2.5 py-0.5 text-xs font-bold text-white ${className}`}>
      {urgencyText(label, lang)}
    </span>
  );
}
