"use client";

// The wishlist drawer. Saved deals live in this browser's localStorage (lib/saved.ts):
// no account, nothing sent to a server. Each row opens the deal's Quick View.

import { AnimatePresence, motion } from "framer-motion";
import { Heart, Trash2, X } from "lucide-react";
import { useId } from "react";

import { productPath, titleOf } from "@/lib/format";
import { dirOf, getDictionary, type Lang } from "@/lib/i18n";
import type { SavedDeal } from "@/lib/saved";

import { Overlay } from "./Overlay";
import { PriceBlock, ProductImage, StorePill } from "./ProductBits";

type SavedDealsDrawerProps = {
  lang: Lang;
  open: boolean;
  deals: SavedDeal[];
  onClose: () => void;
  onOpenDeal: (productId: number) => void;
  onRemove: (productId: number) => void;
  onClear: () => void;
};

export function SavedDealsDrawer({ lang, open, deals, onClose, onOpenDeal, onRemove, onClear }: SavedDealsDrawerProps) {
  const t = getDictionary(lang);
  const titleId = useId();

  return (
    <Overlay open={open} onClose={onClose} labelledBy={titleId} placement="end" dir={dirOf(lang)} className="h-full w-full max-w-md">
      <header className="flex items-center justify-between gap-3 border-b border-line px-5 py-4">
        <h2 id={titleId} className="flex items-center gap-2.5 text-lg font-bold text-ink">
          <Heart aria-hidden="true" size={18} className="fill-flash text-flash" />
          {t.saved.title}
          {deals.length > 0 ? (
            <span className="rounded-full bg-paper px-2 py-0.5 text-sm font-semibold tabular-nums text-ink-soft">{deals.length}</span>
          ) : null}
        </h2>
        <button
          type="button"
          onClick={onClose}
          aria-label={t.filters.close}
          className="grid size-9 place-items-center rounded-lg text-ink-soft hover:bg-line/50 hover:text-ink"
        >
          <X aria-hidden="true" size={20} />
        </button>
      </header>

      {deals.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center">
          <span className="grid size-14 place-items-center rounded-full bg-paper text-ink-faint">
            <Heart aria-hidden="true" size={24} />
          </span>
          <p className="text-lg font-bold text-ink">{t.saved.emptyTitle}</p>
          <p className="max-w-xs text-sm text-ink-soft">{t.saved.emptyBody}</p>
          <button
            type="button"
            data-autofocus
            onClick={onClose}
            className="mt-2 inline-flex h-11 items-center rounded-[10px] bg-spruce px-5 text-sm font-bold text-white transition-colors hover:bg-spruce-deep"
          >
            {t.saved.browse}
          </button>
        </div>
      ) : (
        <>
          <ul className="flex-1 divide-y divide-line overflow-y-auto overscroll-contain px-5">
            <AnimatePresence initial={false}>
              {deals.map((deal) => {
                const title = titleOf(deal, lang);
                return (
                  <motion.li
                    key={deal.id}
                    layout
                    exit={{ opacity: 0, height: 0 }}
                    transition={{ type: "spring", stiffness: 500, damping: 40 }}
                    className="overflow-hidden"
                  >
                    <div className="relative flex gap-3.5 py-4">
                      <ProductImage src={deal.image_urls[0]} alt="" className="size-20 shrink-0 rounded-xl border border-line" />
                      <div className="flex min-w-0 flex-1 flex-col items-start gap-1.5">
                        <StorePill store={deal.store} />
                        <a
                          href={productPath(lang, deal.id)}
                          onClick={(event) => {
                            if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey) return;
                            event.preventDefault();
                            onOpenDeal(deal.id);
                          }}
                          className="line-clamp-2 text-sm font-semibold leading-snug text-ink after:absolute after:inset-0 hover:text-spruce"
                        >
                          <span dir="auto">{title}</span>
                        </a>
                        <PriceBlock product={deal} lang={lang} size="sm" />
                      </div>
                      <button
                        type="button"
                        onClick={() => onRemove(deal.id)}
                        aria-label={`${t.saved.remove}: ${title}`}
                        title={t.saved.remove}
                        className="relative z-10 grid size-9 shrink-0 place-items-center self-start rounded-lg text-ink-faint hover:bg-flash-tint hover:text-flash"
                      >
                        <Trash2 aria-hidden="true" size={16} />
                      </button>
                    </div>
                  </motion.li>
                );
              })}
            </AnimatePresence>
          </ul>
          <footer className="flex items-center justify-between gap-3 border-t border-line px-5 py-3.5 pb-[max(0.875rem,env(safe-area-inset-bottom))]">
            <p className="text-xs text-ink-soft">{t.saved.device}</p>
            <button
              type="button"
              onClick={onClear}
              className="text-sm font-semibold text-ink-soft underline decoration-line-strong underline-offset-4 hover:text-flash"
            >
              {t.saved.clear}
            </button>
          </footer>
        </>
      )}
    </Overlay>
  );
}
