"use client";

// The step between FindHub and the store. It says plainly where the shopper is going,
// reminds them of the coupon, and only on "Continue to Store" does it log the click
// (POST /api/track-click/{id}) and open the affiliate link in a new tab.
//
// The new tab is opened in the same instant as the click and pointed at the store once
// the API has answered; browsers block tabs that are opened later, after a network call.

import { ArrowRight, Check, Copy, ExternalLink, LoaderCircle } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { trackClick, type Product } from "@/lib/api";
import { dirOf, getDictionary, type Lang } from "@/lib/i18n";

import { Overlay } from "./Overlay";
import { useToast } from "./Toaster";

type RedirectConfirmModalProps = {
  lang: Lang;
  /** The deal being left for; `null` keeps the dialog closed. */
  product: Product | null;
  couponCopied: boolean;
  /** Storefront path the shopper is on, stored with the click. */
  referrerPath: string;
  onCopyCoupon: (product: Product) => void;
  onClose: () => void;
};

export function RedirectConfirmModal({ lang, product, couponCopied, referrerPath, onCopyCoupon, onClose }: RedirectConfirmModalProps) {
  const t = getDictionary(lang);
  const toast = useToast();
  const titleId = useId();
  const [opening, setOpening] = useState(false);
  // Keep showing the last deal while the dialog animates out.
  const [shown, setShown] = useState<Product | null>(product);
  useEffect(() => {
    if (product) setShown(product);
  }, [product]);
  const deal = product ?? shown;

  const continueToStore = async () => {
    if (!product || opening) return;
    setOpening(true);
    const tab = window.open("", "_blank");
    try {
      const receipt = await trackClick(product.id, { language: lang, referrerPath });
      if (!/^https?:\/\//i.test(receipt.affiliate_url)) throw new Error("Unexpected link");
      if (tab) {
        tab.opener = null;
        tab.location.replace(receipt.affiliate_url);
      } else {
        window.location.assign(receipt.affiliate_url); // pop-ups are blocked: go there in this tab
      }
      onClose();
    } catch {
      tab?.close();
      toast(t.redirect.failed, "error");
    } finally {
      setOpening(false);
    }
  };

  return (
    <Overlay
      open={product !== null}
      onClose={onClose}
      labelledBy={titleId}
      dir={dirOf(lang)}
      className="w-full rounded-t-[22px] p-6 pb-[max(1.5rem,env(safe-area-inset-bottom))] sm:max-w-md sm:rounded-[22px] sm:p-7"
    >
      {deal ? (
        <>
          <span aria-hidden="true" className="mb-4 grid size-12 place-items-center rounded-full bg-spruce-tint text-spruce">
            <ExternalLink size={22} />
          </span>
          <h2 id={titleId} className="text-xl font-bold text-ink">
            {t.redirect.title(deal.store.name)}
          </h2>
          <p className="mt-2 text-base text-ink-soft">
            {t.redirect.bodyBefore}
            <strong className="font-bold text-ink" dir="ltr">
              {deal.store.name}
            </strong>
            {t.redirect.bodyAfter}
          </p>

          {deal.coupon_code ? (
            couponCopied ? (
              <p className="mt-4 flex items-start gap-2.5 rounded-xl bg-spruce-tint/70 p-3.5 text-sm font-semibold text-spruce">
                <Check aria-hidden="true" size={17} strokeWidth={3} className="mt-0.5 shrink-0" />
                <span>{t.redirect.couponCopied(deal.coupon_code)}</span>
              </p>
            ) : (
              <div className="mt-4 flex items-center justify-between gap-3 rounded-xl border border-dashed border-spruce/45 bg-spruce-tint/50 p-3.5">
                <p className="min-w-0 text-sm text-ink-soft">
                  {t.redirect.couponRemind}{" "}
                  <strong dir="ltr" className="font-bold tracking-[0.06em] text-ink">
                    {deal.coupon_code}
                  </strong>
                </p>
                <button
                  type="button"
                  onClick={() => onCopyCoupon(deal)}
                  className="inline-flex h-9 shrink-0 items-center gap-1.5 rounded-[10px] border border-spruce/35 bg-surface px-3 text-sm font-bold text-spruce hover:border-spruce"
                >
                  <Copy aria-hidden="true" size={15} />
                  {t.quickView.copy}
                </button>
              </div>
            )
          ) : null}

          <div className="mt-6 flex flex-col-reverse gap-2.5 sm:flex-row sm:justify-end">
            <button
              type="button"
              onClick={onClose}
              className="inline-flex h-12 items-center justify-center rounded-xl border border-line-strong px-5 text-base font-semibold text-ink transition-colors hover:border-ink-faint"
            >
              {t.redirect.cancel}
            </button>
            <button
              type="button"
              data-autofocus
              onClick={continueToStore}
              disabled={opening}
              className="inline-flex h-12 items-center justify-center gap-2 rounded-xl bg-spruce px-6 text-base font-bold text-on-spruce transition-colors hover:bg-spruce-deep disabled:opacity-80"
            >
              {opening ? t.redirect.opening : t.redirect.confirm}
              {opening ? (
                <LoaderCircle aria-hidden="true" size={18} className="animate-spin" />
              ) : (
                <ArrowRight aria-hidden="true" size={18} strokeWidth={2.5} className="rtl:rotate-180" />
              )}
            </button>
          </div>

          <p className="mt-4 text-xs leading-relaxed text-ink-faint">
            {t.redirect.newTab} {t.redirect.disclosure}
          </p>
        </>
      ) : null}
    </Overlay>
  );
}
