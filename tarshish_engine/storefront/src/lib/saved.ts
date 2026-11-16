"use client";

// Saved deals: a wishlist kept in this browser's localStorage, with no account and no server.
//
// One small store shared by every component (heart buttons, the Navbar counter, the
// drawer) through `useSyncExternalStore`, and kept in step across tabs by the browser's
// `storage` event. A snapshot of each product is stored so the drawer can draw itself
// offline and before the catalogue has loaded.

import { useCallback, useSyncExternalStore } from "react";

import type { Product } from "./api";

const STORAGE_KEY = "findhub.saved-deals.v1";
const MAX_SAVED = 100;

export type SavedDeal = Pick<
  Product,
  | "id"
  | "title"
  | "title_he"
  | "category"
  | "store"
  | "price"
  | "original_price"
  | "discount_percent"
  | "currency"
  | "rating"
  | "image_urls"
> & { savedAt: number };

const EMPTY: SavedDeal[] = [];
const listeners = new Set<() => void>();
let snapshot: SavedDeal[] | null = null;

function isSavedDeal(value: unknown): value is SavedDeal {
  if (typeof value !== "object" || value === null) return false;
  const deal = value as Record<string, unknown>;
  return (
    typeof deal.id === "number" &&
    typeof deal.title === "string" &&
    typeof deal.price === "number" &&
    typeof deal.currency === "string" &&
    Array.isArray(deal.image_urls) &&
    typeof deal.store === "object" &&
    deal.store !== null
  );
}

function read(): SavedDeal[] {
  if (snapshot !== null) return snapshot;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    snapshot = Array.isArray(parsed) ? parsed.filter(isSavedDeal) : EMPTY;
  } catch {
    snapshot = EMPTY; // private mode, blocked storage or a corrupted value
  }
  return snapshot;
}

function write(next: SavedDeal[]): void {
  snapshot = next;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Storage is full or blocked: the list still works until the tab closes.
  }
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === STORAGE_KEY) {
      snapshot = null;
      listener();
    }
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

function toSavedDeal(product: Product): SavedDeal {
  return {
    id: product.id,
    title: product.title,
    title_he: product.title_he,
    category: product.category,
    store: product.store,
    price: product.price,
    original_price: product.original_price,
    discount_percent: product.discount_percent,
    currency: product.currency,
    rating: product.rating,
    image_urls: product.image_urls.slice(0, 1),
    savedAt: Date.now(),
  };
}

export function useSavedDeals() {
  // The server (and the first client render) sees an empty list; the real one arrives right after hydration.
  const deals = useSyncExternalStore(subscribe, read, () => EMPTY);

  const isSaved = useCallback((productId: number) => deals.some((deal) => deal.id === productId), [deals]);

  /** Saves or removes the product; returns true when it is now saved. */
  const toggle = useCallback((product: Product): boolean => {
    const current = read();
    if (current.some((deal) => deal.id === product.id)) {
      write(current.filter((deal) => deal.id !== product.id));
      return false;
    }
    write([toSavedDeal(product), ...current].slice(0, MAX_SAVED));
    return true;
  }, []);

  const remove = useCallback((productId: number) => {
    write(read().filter((deal) => deal.id !== productId));
  }, []);

  const clear = useCallback(() => write([]), []);

  return { deals, isSaved, toggle, remove, clear };
}
