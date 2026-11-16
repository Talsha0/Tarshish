"use client";

// One deal in the grid. Everything on it is there to answer "can I trust this?" before
// "how much?": the store it comes from, the rating and order count, what we checked,
// then the price on the yellow label. The whole card opens Quick View (it is a real link
// to the product's deep-link page, so it can also be opened in a new tab or shared);
// the heart saves the deal and "Get deal" goes straight to the store confirmation.

import { motion } from "framer-motion";
import { Heart } from "lucide-react";
import type { MouseEvent } from "react";

import type { Product } from "@/lib/api";
import { formatOrders, productPath, titleOf } from "@/lib/format";
import { getDictionary, type Lang } from "@/lib/i18n";

import {
  CrowdBadge,
  FlashBadge,
  PriceBlock,
  ProductImage,
  RatingPill,
  SocialProofLine,
  type Standards,
  StorePill,
  UrgencyChip,
  VerifiedBadge,
} from "./ProductBits";

type ProductCardProps = {
  product: Product;
  lang: Lang;
  standards: Standards;
  saved: boolean;
  /** Draws attention to the card a deep link pointed at. */
  highlighted?: boolean;
  /** Load the photo right away (cards visible without scrolling). */
  eager?: boolean;
  onOpen: (product: Product) => void;
  onGetDeal: (product: Product) => void;
  onToggleSave: (product: Product) => void;
};

function isPlainClick(event: MouseEvent): boolean {
  return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
}

export function ProductCard({
  product,
  lang,
  standards,
  saved,
  highlighted = false,
  eager = false,
  onOpen,
  onGetDeal,
  onToggleSave,
}: ProductCardProps) {
  const t = getDictionary(lang);
  const title = titleOf(product, lang);
  const flash = product.badges.includes("flash_deal");
  const crowd = product.badges.includes("crowd_favorite");
  const verified = product.badges.includes("verified");
  // One chip sits on the photo: the flash badge when there is one, otherwise the urgency label.
  const urgencyOnPhoto = !flash && product.stock_urgency_label;
  const urgencyInBody = flash && product.stock_urgency_label;

  return (
    <article
      data-product-id={product.id}
      className={`group relative flex h-full flex-col overflow-hidden rounded-[14px] border bg-surface shadow-card transition-[transform,box-shadow,border-color] duration-200 ease-out hover:-translate-y-1 hover:border-line-strong hover:shadow-lift motion-reduce:transition-none motion-reduce:hover:translate-y-0 ${
        highlighted ? "border-spruce-bright ring-2 ring-spruce-bright/35" : "border-line"
      }`}
    >
      <div className="relative aspect-square overflow-hidden bg-paper">
        <ProductImage
          src={product.image_urls[0]}
          alt=""
          eager={eager}
          className="size-full transition-transform duration-500 ease-out group-hover:scale-[1.06] motion-reduce:transition-none motion-reduce:group-hover:scale-100"
        />
        <StorePill store={product.store} className="absolute start-2.5 top-2.5" />
        {flash ? <FlashBadge lang={lang} className="absolute bottom-2.5 start-2.5 shadow-sm" /> : null}
        {urgencyOnPhoto ? (
          <UrgencyChip label={urgencyOnPhoto} lang={lang} className="absolute bottom-2.5 start-2.5" />
        ) : null}
      </div>

      <div className="flex flex-1 flex-col gap-2.5 p-3 sm:p-4">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <RatingPill rating={product.rating} lang={lang} />
          <span className="text-xs font-medium text-ink-soft">{t.card.orders(formatOrders(product.orders_count, lang))}</span>
        </div>

        <h3 className="text-sm font-semibold leading-snug text-ink sm:text-[0.95rem]">
          <a
            href={productPath(lang, product.id)}
            onClick={(event) => {
              if (!isPlainClick(event)) return;
              event.preventDefault();
              onOpen(product);
            }}
            aria-label={t.card.quickView(title)}
            className="line-clamp-2 outline-none after:absolute after:inset-0 after:rounded-[14px] focus-visible:after:outline focus-visible:after:outline-2 focus-visible:after:-outline-offset-2 focus-visible:after:outline-spruce-bright"
          >
            <span dir="auto">{title}</span>
          </a>
        </h3>

        {crowd || verified ? (
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
            {crowd ? <CrowdBadge lang={lang} /> : null}
            {verified ? <VerifiedBadge lang={lang} standards={standards} className="relative z-10" /> : null}
          </div>
        ) : null}

        {product.social_proof || urgencyInBody ? (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
            {product.social_proof ? <SocialProofLine kind={product.social_proof} lang={lang} /> : null}
            {urgencyInBody ? <UrgencyChip label={urgencyInBody} lang={lang} /> : null}
          </div>
        ) : null}

        {/* Pinned to the bottom, so prices and buttons line up across a row of cards. */}
        <div className="mt-auto flex flex-col gap-3 pt-1.5">
          <PriceBlock product={product} lang={lang} />
          <button
            type="button"
            onClick={() => onGetDeal(product)}
            className="relative z-10 inline-flex h-10 items-center justify-center rounded-[10px] border border-spruce/25 bg-spruce-tint/60 px-3 text-sm font-bold text-spruce transition-colors hover:border-spruce hover:bg-spruce hover:text-white"
          >
            {t.card.getDeal}
          </button>
        </div>
      </div>

      <motion.button
        type="button"
        aria-pressed={saved}
        aria-label={saved ? t.card.unsave : t.card.save}
        title={saved ? t.card.unsave : t.card.save}
        onClick={() => onToggleSave(product)}
        whileTap={{ scale: 0.82 }}
        className={`absolute end-2.5 top-2.5 z-10 grid size-9 place-items-center rounded-full border shadow-sm transition-colors ${
          saved
            ? "border-flash/30 bg-flash-tint text-flash"
            : "border-line bg-surface/95 text-ink-soft hover:border-line-strong hover:text-flash"
        }`}
      >
        <motion.span
          key={saved ? "saved" : "idle"}
          initial={{ scale: saved ? 0.4 : 1 }}
          animate={{ scale: 1 }}
          transition={{ type: "spring", stiffness: 520, damping: 14 }}
          className="grid place-items-center"
        >
          <Heart aria-hidden="true" size={17} strokeWidth={2.25} className={saved ? "fill-current" : ""} />
        </motion.span>
      </motion.button>
    </article>
  );
}
