// Numbers, prices and titles as the shopper reads them.
//
// The catalogue stores prices in ILS. Hebrew shows them as they are (₪). English shows
// an approximate US-dollar price converted at NEXT_PUBLIC_ILS_PER_USD, and says so
// wherever a price can be acted on.

import type { Product } from "./api";
import { localeOf, type Lang } from "./i18n";

const configuredRate = Number(process.env.NEXT_PUBLIC_ILS_PER_USD);
export const ILS_PER_USD = Number.isFinite(configuredRate) && configuredRate > 0 ? configuredRate : 3.5;

export type Money = {
  symbol: string;
  /** Digits only, e.g. "189.90". */
  amount: string;
  /** Symbol and digits, e.g. "₪189.90". Wrap in a `dir="ltr"` element when shown. */
  text: string;
  /** True when the value was converted from the catalogue currency. */
  converted: boolean;
};

function digits(value: number, alwaysCents: boolean): string {
  const cents = alwaysCents || !Number.isInteger(Math.round(value * 100) / 100);
  return new Intl.NumberFormat("en-US", {
    minimumFractionDigits: cents ? 2 : 0,
    maximumFractionDigits: 2,
  }).format(value);
}

export function money(value: number, currency: string, lang: Lang): Money {
  if (currency === "ILS" && lang === "en") {
    const amount = digits(value / ILS_PER_USD, true);
    return { symbol: "$", amount, text: `$${amount}`, converted: true };
  }
  if (currency === "ILS") {
    const amount = digits(value, false);
    return { symbol: "₪", amount, text: `₪${amount}`, converted: false };
  }
  const parts = new Intl.NumberFormat("en-US", { style: "currency", currency, currencyDisplay: "narrowSymbol" })
    .formatToParts(value);
  const symbol = parts.find((part) => part.type === "currency")?.value ?? currency;
  const amount = parts.filter((part) => part.type !== "currency" && part.type !== "literal").map((part) => part.value).join("");
  return { symbol, amount, text: `${symbol}${amount}`, converted: false };
}

/** The price exactly as stored, for the "converted from" note. */
export function catalogMoney(value: number, currency: string): Money {
  return money(value, currency, "he");
}

export function currencySymbol(lang: Lang): string {
  return lang === "en" ? "$" : "₪";
}

/** A price typed into the filter (display currency) as a catalogue price (ILS). */
export function toCatalogPrice(displayValue: number, lang: Lang): number {
  const value = lang === "en" ? displayValue * ILS_PER_USD : displayValue;
  return Math.round(value * 100) / 100;
}

export function formatNumber(value: number, lang: Lang): string {
  return new Intl.NumberFormat(localeOf(lang)).format(value);
}

export function formatRating(value: number): string {
  return value.toFixed(1);
}

/** 3412 -> "3,400+", 960 -> "950+", 350 -> "350+": never more than the real count. */
export function formatOrders(count: number, lang: Lang): string {
  const step = count >= 1000 ? 100 : count >= 100 ? 50 : 1;
  const floor = Math.floor(count / step) * step;
  return `${formatNumber(floor, lang)}+`;
}

/** Hebrew title on the Hebrew storefront when the product has one. */
export function titleOf(product: Pick<Product, "title" | "title_he">, lang: Lang): string {
  return lang === "he" && product.title_he ? product.title_he : product.title;
}

export function productPath(lang: Lang, productId: number): string {
  return `/${lang}/p/${productId}`;
}

export function hubPath(lang: Lang, category: string | null): string {
  return category ? `/${lang}/c/${encodeURIComponent(category)}` : `/${lang}`;
}
