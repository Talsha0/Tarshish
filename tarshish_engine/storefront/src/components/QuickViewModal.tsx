"use client";

// Quick View: the full picture of one deal without leaving the grid. Gallery, the
// complete title, the exact rating and order count with what we checked, the price, a
// one-click coupon box, and one primary action: "Get Deal on <store>", which opens the
// redirect confirmation. A centred modal on wide screens, a bottom sheet on phones.

import { AnimatePresence, motion } from "framer-motion";
import { BadgeCheck, Check, ChevronLeft, ChevronRight, Copy, Heart, Link2, Star, X } from "lucide-react";
import { useEffect, useId, useState } from "react";

import type { Product } from "@/lib/api";
import { catalogMoney, formatNumber, formatRating, money, titleOf } from "@/lib/format";
import { dirOf, getDictionary, type Lang } from "@/lib/i18n";

import { Overlay } from "./Overlay";
import {
  CrowdBadge,
  FlashBadge,
  PriceBlock,
  ProductImage,
  SocialProofLine,
  type Standards,
  StorePill,
  UrgencyChip,
  VerifiedBadge,
} from "./ProductBits";

type QuickViewModalProps = {
  lang: Lang;
  open: boolean;
  /** `null` while the deal is still loading (opened from a saved deal or a deep link). */
  product: Product | null;
  standards: Standards;
  saved: boolean;
  couponCopied: boolean;
  onClose: () => void;
  onToggleSave: (product: Product) => void;
  onCopyCoupon: (product: Product) => void;
  onCopyLink: (product: Product) => void;
  onGetDeal: (product: Product) => void;
};

function Gallery({ product, lang, title }: { product: Product; lang: Lang; title: string }) {
  const t = getDictionary(lang);
  const images = product.image_urls;
  const [index, setIndex] = useState(0);
  useEffect(() => setIndex(0), [product.id]);
  const current = Math.min(index, Math.max(images.length - 1, 0));
  const step = (delta: number) => setIndex((current + delta + images.length) % images.length);
  const rtl = dirOf(lang) === "rtl";

  return (
    <div className="flex flex-col gap-3">
      <div
        className="relative aspect-square overflow-hidden rounded-2xl border border-line bg-paper"
        onKeyDown={(event) => {
          if (images.length < 2) return;
          if (event.key === "ArrowRight") step(rtl ? -1 : 1);
          if (event.key === "ArrowLeft") step(rtl ? 1 : -1);
        }}
      >
        <AnimatePresence initial={false} mode="popLayout">
          <motion.div
            key={images[current] ?? "none"}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="absolute inset-0"
          >
            <ProductImage src={images[current]} alt={title} eager className="size-full" />
          </motion.div>
        </AnimatePresence>
        {product.badges.includes("flash_deal") ? <FlashBadge lang={lang} className="absolute start-3 top-3 shadow-sm" /> : null}
        {images.length > 1 ? (
          <>
            <button
              type="button"
              onClick={() => step(-1)}
              aria-label={t.quickView.image(((current - 1 + images.length) % images.length) + 1, images.length)}
              className="absolute start-2.5 top-1/2 grid size-9 -translate-y-1/2 place-items-center rounded-full border border-line bg-surface/95 text-ink shadow-sm hover:border-line-strong"
            >
              <ChevronLeft aria-hidden="true" size={18} className="rtl:rotate-180" />
            </button>
            <button
              type="button"
              onClick={() => step(1)}
              aria-label={t.quickView.image(((current + 1) % images.length) + 1, images.length)}
              className="absolute end-2.5 top-1/2 grid size-9 -translate-y-1/2 place-items-center rounded-full border border-line bg-surface/95 text-ink shadow-sm hover:border-line-strong"
            >
              <ChevronRight aria-hidden="true" size={18} className="rtl:rotate-180" />
            </button>
          </>
        ) : null}
      </div>
      {images.length > 1 ? (
        <div className="flex gap-2.5">
          {images.map((src, position) => (
            <button
              key={src}
              type="button"
              onClick={() => setIndex(position)}
              aria-label={t.quickView.image(position + 1, images.length)}
              aria-current={position === current}
              className={`size-16 overflow-hidden rounded-xl border-2 transition-colors ${
                position === current ? "border-spruce" : "border-line hover:border-line-strong"
              }`}
            >
              <ProductImage src={src} alt="" className="size-full" />
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function Stars({ rating }: { rating: number }) {
  const percent = Math.max(0, Math.min(100, (rating / 5) * 100));
  const row = (className: string) => (
    <span className={`flex gap-0.5 ${className}`}>
      {Array.from({ length: 5 }, (_, index) => (
        <Star key={index} size={16} className="shrink-0 fill-current" strokeWidth={0} />
      ))}
    </span>
  );
  return (
    <span aria-hidden="true" dir="ltr" className="relative inline-block">
      {row("text-line-strong")}
      <span className="absolute inset-y-0 start-0 overflow-hidden" style={{ width: `${percent}%` }}>
        {row("text-tag-deep")}
      </span>
    </span>
  );
}

function LoadingBody({ label }: { label: string }) {
  return (
    <div role="status" aria-label={label} className="grid gap-6 p-5 sm:p-7 md:grid-cols-2">
      <div className="skeleton aspect-square !rounded-2xl" />
      <div className="flex flex-col gap-4">
        <div className="skeleton h-5 w-24" />
        <div className="skeleton h-7 w-full" />
        <div className="skeleton h-7 w-2/3" />
        <div className="skeleton h-5 w-40" />
        <div className="skeleton h-10 w-36" />
        <div className="skeleton mt-auto h-12 w-full !rounded-xl" />
      </div>
    </div>
  );
}

export function QuickViewModal({
  lang,
  open,
  product,
  standards,
  saved,
  couponCopied,
  onClose,
  onToggleSave,
  onCopyCoupon,
  onCopyLink,
  onGetDeal,
}: QuickViewModalProps) {
  const t = getDictionary(lang);
  const titleId = useId();
  const title = product ? titleOf(product, lang) : t.quickView.loading;
  const store = product?.store.name ?? "";
  const now = product ? money(product.price, product.currency, lang) : null;
  const saving =
    product?.original_price && product.original_price > product.price
      ? money(product.original_price - product.price, product.currency, lang)
      : null;

  return (
    <Overlay
      open={open}
      onClose={onClose}
      labelledBy={titleId}
      dir={dirOf(lang)}
      className="max-h-[94dvh] w-full rounded-t-[22px] sm:max-h-[min(92dvh,46rem)] sm:max-w-4xl sm:rounded-[22px]"
    >
      <button
        type="button"
        onClick={onClose}
        aria-label={t.quickView.close}
        className="absolute end-3 top-3 z-20 grid size-10 place-items-center rounded-full border border-line bg-surface/95 text-ink shadow-sm hover:border-line-strong"
      >
        <X aria-hidden="true" size={20} />
      </button>

      {!product ? (
        <>
          <h2 id={titleId} className="sr-only">
            {t.quickView.loading}
          </h2>
          <LoadingBody label={t.quickView.loading} />
        </>
      ) : (
        <>
          <div className="grid gap-6 overflow-y-auto overscroll-contain p-5 sm:p-7 md:grid-cols-[minmax(0,0.95fr)_minmax(0,1.05fr)] md:gap-8">
            <Gallery product={product} lang={lang} title={title} />

            <div className="flex min-w-0 flex-col gap-4">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2 pe-12">
                <StorePill store={product.store} />
                {product.badges.includes("verified") ? <VerifiedBadge lang={lang} standards={standards} /> : null}
              </div>

              <h2 id={titleId} dir="auto" className="text-xl font-bold leading-snug text-ink sm:text-2xl">
                {title}
              </h2>

              <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
                <span className="flex items-center gap-2">
                  <Stars rating={product.rating} />
                  <span className="font-bold text-ink">{t.quickView.ratingOutOf(formatRating(product.rating))}</span>
                </span>
                <span className="text-ink-soft">{t.quickView.ordersOn(formatNumber(product.orders_count, lang), store)}</span>
              </div>

              {product.badges.includes("crowd_favorite") || product.social_proof || product.stock_urgency_label ? (
                <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                  {product.badges.includes("crowd_favorite") ? <CrowdBadge lang={lang} /> : null}
                  {product.stock_urgency_label ? <UrgencyChip label={product.stock_urgency_label} lang={lang} /> : null}
                  {product.social_proof ? <SocialProofLine kind={product.social_proof} lang={lang} /> : null}
                </div>
              ) : null}

              <div className="flex flex-wrap items-end gap-x-4 gap-y-2 border-y border-line py-4">
                <PriceBlock product={product} lang={lang} size="lg" />
                {saving ? (
                  <span className="pb-1.5 text-sm font-semibold text-spruce-bright">{t.quickView.save(saving.text)}</span>
                ) : null}
              </div>

              {product.coupon_code ? (
                <div className="rounded-xl border border-dashed border-spruce/45 bg-spruce-tint/50 p-3.5">
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-xs font-semibold text-ink-soft">{t.quickView.couponTitle}</p>
                      <p dir="ltr" className="select-all truncate text-start text-xl font-extrabold tracking-[0.08em] text-ink rtl:text-end">
                        {product.coupon_code}
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => onCopyCoupon(product)}
                      className={`inline-flex h-10 shrink-0 items-center gap-2 rounded-[10px] px-3.5 text-sm font-bold transition-colors ${
                        couponCopied ? "bg-spruce text-white" : "border border-spruce/35 bg-surface text-spruce hover:border-spruce"
                      }`}
                    >
                      {couponCopied ? <Check aria-hidden="true" size={16} strokeWidth={3} /> : <Copy aria-hidden="true" size={16} />}
                      <span aria-live="polite">{couponCopied ? t.quickView.copied : t.quickView.copy}</span>
                    </button>
                  </div>
                  <p className="mt-1.5 text-xs text-ink-soft">{t.quickView.couponHint(store)}</p>
                </div>
              ) : null}

              <div>
                <h3 className="mb-2 text-sm font-bold text-ink">{t.quickView.checksTitle}</h3>
                <ul className="flex flex-col gap-1.5 text-sm text-ink-soft">
                  {[
                    t.quickView.checkRating(formatRating(product.rating), formatRating(standards.min_rating)),
                    t.quickView.checkOrders(formatNumber(product.orders_count, lang), formatNumber(standards.min_orders, lang)),
                    t.quickView.checkLink(store),
                  ].map((line) => (
                    <li key={line} className="flex items-start gap-2">
                      <BadgeCheck aria-hidden="true" size={16} className="mt-0.5 shrink-0 text-spruce-bright" />
                      <span>{line}</span>
                    </li>
                  ))}
                </ul>
              </div>

              <p className="text-xs leading-relaxed text-ink-faint">
                {now?.converted
                  ? t.quickView.priceNoteConverted(catalogMoney(product.price, product.currency).text, store)
                  : t.quickView.priceNote(store)}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2.5 border-t border-line bg-surface p-4 pb-[max(1rem,env(safe-area-inset-bottom))] sm:rounded-b-[22px] sm:px-7">
            <button
              type="button"
              aria-pressed={saved}
              aria-label={saved ? t.card.unsave : t.card.save}
              title={saved ? t.card.unsave : t.card.save}
              onClick={() => onToggleSave(product)}
              className={`grid size-12 shrink-0 place-items-center rounded-xl border transition-colors ${
                saved ? "border-flash/30 bg-flash-tint text-flash" : "border-line-strong text-ink-soft hover:border-ink-faint hover:text-flash"
              }`}
            >
              <Heart aria-hidden="true" size={20} className={saved ? "fill-current" : ""} />
            </button>
            <button
              type="button"
              aria-label={t.quickView.share}
              title={t.quickView.share}
              onClick={() => onCopyLink(product)}
              className="grid size-12 shrink-0 place-items-center rounded-xl border border-line-strong text-ink-soft transition-colors hover:border-ink-faint hover:text-ink"
            >
              <Link2 aria-hidden="true" size={20} />
            </button>
            <button
              type="button"
              data-autofocus
              onClick={() => onGetDeal(product)}
              className="inline-flex h-12 min-w-0 flex-1 items-center justify-center rounded-xl bg-spruce px-5 text-base font-bold text-white transition-colors hover:bg-spruce-deep"
            >
              <span className="truncate">{t.quickView.cta(store)}</span>
            </button>
          </div>
        </>
      )}
    </Overlay>
  );
}
