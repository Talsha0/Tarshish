"use client";

// The storefront itself, shared by the three routes:
//
//   /{lang}                  every deal, with the category pill bar
//   /{lang}/c/{category}     one category hub (the category is fixed, search is scoped to it)
//   /{lang}/p/{productId}    the product's hub with its Quick View already open
//
// The server renders the first page of deals; from then on searching, filtering, sorting
// and "show more" fetch from the API and the grid shows skeleton cards while it waits.
//
// Quick View is driven by the address bar. Opening a deal pushes /{lang}/p/{id} into the
// browser history (no page load), so every open deal has a link that can be shared, the
// Back button closes it, and reloading lands on the deep-link page for the same deal.

import { motion } from "framer-motion";
import { RefreshCw, SearchX, SlidersHorizontal, X } from "lucide-react";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { ApiError, getCatalog, getProduct, getProducts, type Catalog, type Product, type ProductPage } from "@/lib/api";
import {
  EMPTY_FILTERS,
  countSidebarFilters,
  filtersToSearch,
  isNarrowed,
  toProductQuery,
  type Filters,
} from "@/lib/filters";
import { formatNumber, hubPath, productPath } from "@/lib/format";
import { categoryName, dirOf, getDictionary, type Lang } from "@/lib/i18n";
import { useSavedDeals } from "@/lib/saved";

import { CategoryBar } from "./CategoryBar";
import { FilterSidebar, SortSelect } from "./FilterSidebar";
import { Footer } from "./Footer";
import { Hero } from "./Hero";
import { Navbar } from "./Navbar";
import { Overlay } from "./Overlay";
import { ProductCard } from "./ProductCard";
import { QuickViewModal } from "./QuickViewModal";
import { RedirectConfirmModal } from "./RedirectConfirmModal";
import { SavedDealsDrawer } from "./SavedDealsDrawer";
import { GRID_CLASS, SkeletonCard, SkeletonGrid } from "./SkeletonCard";
import { hasHydrated, useToast } from "./Toaster";

export type StorefrontProps = {
  lang: Lang;
  /** Category fixed by the route (hub and product pages); `null` on the main storefront. */
  scopeCategory: string | null;
  initialFilters: Filters;
  /** First page of deals for `initialFilters`; `null` when the API could not be reached. */
  initialPage: ProductPage | null;
  catalog: Catalog | null;
  /** The biggest price drop in scope, shown in the hero. */
  featured: Product | null;
  /** The deal a deep link points at: its Quick View opens at once and its card is highlighted. */
  initialProduct?: Product | null;
};

const SEARCH_DEBOUNCE_MS = 280;
const DEFAULT_STANDARDS = { min_rating: 4.5, min_orders: 200 };
const PRODUCT_PATH = /^\/[a-z]{2}\/p\/(\d+)\/?$/;

const gridMotion = { hidden: {}, show: { transition: { staggerChildren: 0.045 } } };
const cardMotion = {
  hidden: { opacity: 0, y: 18 },
  show: { opacity: 1, y: 0, transition: { type: "spring", stiffness: 320, damping: 30 } },
} as const;

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Older browsers and non-secure origins: fall back to a hidden field.
    const field = document.createElement("textarea");
    field.value = text;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.appendChild(field);
    field.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    }
    field.remove();
    return copied;
  }
}

export function Storefront({
  lang,
  scopeCategory,
  initialFilters,
  initialPage,
  catalog: initialCatalog,
  featured,
  initialProduct = null,
}: StorefrontProps) {
  const t = getDictionary(lang);
  const dir = dirOf(lang);
  const pathname = usePathname();
  const toast = useToast();
  const saved = useSavedDeals();
  const sheetTitleId = useId();

  // -- what is shown ---------------------------------------------------------------- //
  const [filters, setFilters] = useState<Filters>(initialFilters);
  const [searchText, setSearchText] = useState(initialFilters.q);
  const [result, setResult] = useState<ProductPage | null>(initialPage);
  const [status, setStatus] = useState<"ready" | "loading" | "error">(initialPage ? "ready" : "error");
  const [loadingMore, setLoadingMore] = useState(false);
  const [reload, setReload] = useState(0);
  const [catalog, setCatalog] = useState<Catalog | null>(initialCatalog);
  // The grid staggers in whenever it is drawn in the browser (after a search, a filter, or a
  // client-side page change). The server-rendered first page is the exception: it is already
  // on screen when the scripts start, and hiding it again just to animate it would be a flash.
  const [animateGrid, setAnimateGrid] = useState(hasHydrated);

  const category = scopeCategory ?? filters.category;
  const requestKey = JSON.stringify(toProductQuery(filters, scopeCategory));
  const loadedKey = useRef<string | null>(initialPage ? requestKey : null);

  const patchFilters = useCallback((patch: Partial<Filters>) => setFilters((current) => ({ ...current, ...patch })), []);

  // Typing searches after a short pause; everything else applies immediately.
  useEffect(() => {
    const timer = window.setTimeout(() => patchFilters({ q: searchText.trim() }), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [searchText, patchFilters]);

  useEffect(() => {
    if (loadedKey.current === requestKey) return;
    const controller = new AbortController();
    setStatus("loading");
    getProducts(toProductQuery(filters, scopeCategory), controller.signal)
      .then((page) => {
        loadedKey.current = requestKey;
        setResult(page);
        setAnimateGrid(true);
        setStatus("ready");
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setStatus("error");
      });
    return () => controller.abort();
    // `filters` and `scopeCategory` are captured by `requestKey`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestKey, reload]);

  // The category list arrives with the page; fetch it here only if the server could not.
  useEffect(() => {
    if (catalog || status !== "ready") return;
    const controller = new AbortController();
    getCatalog(controller.signal).then(setCatalog).catch(() => {});
    return () => controller.abort();
  }, [catalog, status]);

  const loadMore = async () => {
    if (!result || loadingMore || result.page >= result.pages) return;
    const key = requestKey;
    setLoadingMore(true);
    try {
      const next = await getProducts(toProductQuery(filters, scopeCategory, result.page + 1));
      if (loadedKey.current !== key) return; // the filters changed while this page was on its way
      setResult((current) => {
        if (!current) return next;
        const seen = new Set(current.items.map((item) => item.id));
        return { ...next, items: [...current.items, ...next.items.filter((item) => !seen.has(item.id))] };
      });
    } catch {
      toast(t.results.errorTitle, "error");
    } finally {
      setLoadingMore(false);
    }
  };

  // -- Quick View, driven by the address bar ----------------------------------------- //
  const activeId = useMemo(() => {
    const match = PRODUCT_PATH.exec(pathname);
    return match ? Number(match[1]) : null;
  }, [pathname]);
  const [fetched, setFetched] = useState<Record<number, Product>>({});
  const openedHere = useRef(false);
  const lastShown = useRef<Product | null>(initialProduct);

  const basePath = hubPath(lang, scopeCategory);
  const search = filtersToSearch(filters, scopeCategory === null);

  const activeProduct = useMemo(() => {
    if (activeId === null) return null;
    return (
      result?.items.find((item) => item.id === activeId) ??
      (initialProduct?.id === activeId ? initialProduct : null) ??
      (featured?.id === activeId ? featured : null) ??
      fetched[activeId] ??
      null
    );
  }, [activeId, result, initialProduct, featured, fetched]);
  if (activeProduct) lastShown.current = activeProduct;

  const openProduct = useCallback(
    (productId: number) => {
      openedHere.current = true;
      window.history.pushState(null, "", productPath(lang, productId));
    },
    [lang],
  );

  const closeProduct = useCallback(() => {
    if (openedHere.current) {
      openedHere.current = false;
      window.history.back();
    } else {
      // Arrived by a deep link: there is nothing of ours to go back to, so settle on the hub.
      window.history.replaceState(null, "", basePath + search);
    }
  }, [basePath, search]);

  // A deal opened without its data in hand (from the saved list, or by Back/Forward).
  useEffect(() => {
    if (activeId === null || activeProduct) return;
    const controller = new AbortController();
    getProduct(activeId, controller.signal)
      .then((product) => setFetched((current) => ({ ...current, [product.id]: product })))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        const gone = error instanceof ApiError && error.status === 404;
        if (gone) saved.remove(activeId);
        toast(gone ? t.quickView.unavailable : t.results.errorTitle, "error");
        closeProduct();
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, activeProduct]);

  // Keep the address in step with the filters (shareable, and kept by the language switch).
  const firstSync = useRef(true);
  useEffect(() => {
    if (firstSync.current) {
      firstSync.current = false;
      return;
    }
    if (activeId !== null) return;
    const target = basePath + search;
    if (window.location.pathname + window.location.search !== target) {
      window.history.replaceState(null, "", target);
    }
  }, [basePath, search, activeId]);

  // -- actions ------------------------------------------------------------------------ //
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [leavingFor, setLeavingFor] = useState<Product | null>(null);
  const [copiedCoupons, setCopiedCoupons] = useState<ReadonlySet<number>>(new Set());

  const toggleSave = useCallback(
    (product: Product) => toast(saved.toggle(product) ? t.toast.saved : t.toast.unsaved),
    [saved, toast, t],
  );

  const copyCoupon = useCallback(
    async (product: Product) => {
      if (!product.coupon_code) return;
      if (await copyText(product.coupon_code)) {
        setCopiedCoupons((current) => new Set(current).add(product.id));
        toast(t.toast.coupon);
      } else {
        toast(t.toast.copyFailed, "error");
      }
    },
    [toast, t],
  );

  const copyLink = useCallback(
    async (product: Product) => {
      const copied = await copyText(new URL(productPath(lang, product.id), window.location.origin).toString());
      toast(copied ? t.quickView.linkCopied : t.toast.copyFailed, copied ? "ok" : "error");
    },
    [lang, toast, t],
  );

  const clearFilters = () => {
    setSearchText("");
    setFilters({ ...EMPTY_FILTERS, sortBy: filters.sortBy });
  };

  // -- derived view --------------------------------------------------------------------- //
  const standards = catalog?.standards ?? DEFAULT_STANDARDS;
  const sidebarCount = countSidebarFilters(filters);
  const narrowed = isNarrowed(filters);
  const items = useMemo(() => {
    const list = result?.items ?? [];
    // A deep-linked deal leads its hub's grid until the shopper starts narrowing.
    if (!initialProduct || narrowed || filters.sortBy) return list;
    return [initialProduct, ...list.filter((item) => item.id !== initialProduct.id)];
  }, [result, initialProduct, narrowed, filters.sortBy]);
  const total = result?.total ?? 0;
  const searchPlaceholder = category ? t.nav.searchIn(categoryName(lang, category)) : t.nav.searchPlaceholder;
  const shownProduct = activeProduct ?? (activeId === null ? lastShown.current : null);
  const sidebar = (
    <FilterSidebar lang={lang} filters={filters} stores={result?.facets.stores ?? []} onChange={patchFilters} onClear={clearFilters} />
  );

  return (
    <>
      <Navbar
        lang={lang}
        query={searchText}
        onQueryChange={setSearchText}
        searchPlaceholder={searchPlaceholder}
        savedCount={saved.deals.length}
        onOpenSaved={() => setDrawerOpen(true)}
      />

      <Hero lang={lang} category={scopeCategory} catalog={catalog} featured={featured} onOpen={(product) => openProduct(product.id)} />

      <main id="deals" className="mx-auto max-w-shell scroll-mt-28 px-4 pt-6 sm:px-6 sm:pt-8">
        {scopeCategory === null && catalog && catalog.categories.length > 0 ? (
          <div className="mb-5">
            <CategoryBar lang={lang} catalog={catalog} selected={filters.category} onSelect={(key) => patchFilters({ category: key })} />
          </div>
        ) : null}

        <div className="lg:grid lg:grid-cols-[14.5rem_minmax(0,1fr)] lg:gap-8">
          <aside className="hidden lg:block">
            <div className="sticky top-24 rounded-[14px] border border-line bg-surface p-5">{sidebar}</div>
          </aside>

          <section aria-busy={status === "loading"}>
            <div className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-2.5">
              <p aria-live="polite" className="me-auto min-w-0 text-sm text-ink-soft">
                {status === "error" ? null : (
                  <>
                    {filters.q ? <span className="font-semibold text-ink">{t.results.forQuery(filters.q)} </span> : null}
                    <span className={status === "loading" ? "opacity-50" : ""}>
                      {t.categories.deals(formatNumber(total, lang), total)}
                    </span>
                  </>
                )}
              </p>
              <button
                type="button"
                onClick={() => setSheetOpen(true)}
                className="inline-flex h-10 items-center gap-2 rounded-[10px] border border-line-strong bg-surface px-3 text-sm font-semibold text-ink hover:border-ink-faint lg:hidden"
              >
                <SlidersHorizontal aria-hidden="true" size={16} />
                {t.filters.open(sidebarCount)}
              </button>
              <SortSelect lang={lang} value={filters.sortBy} searching={filters.q !== ""} onChange={(sortBy) => patchFilters({ sortBy })} />
            </div>

            {status === "loading" ? (
              <SkeletonGrid label={t.results.loading} count={Math.min(Math.max(items.length, 4), 8)} />
            ) : status === "error" ? (
              <div className="flex flex-col items-center gap-3 rounded-[14px] border border-line bg-surface px-6 py-14 text-center">
                <span className="grid size-12 place-items-center rounded-full bg-flash-tint text-flash">
                  <RefreshCw aria-hidden="true" size={22} />
                </span>
                <h2 className="text-xl font-bold text-ink">{t.results.errorTitle}</h2>
                <p className="max-w-md text-sm text-ink-soft">{t.results.errorBody}</p>
                <button
                  type="button"
                  onClick={() => setReload((count) => count + 1)}
                  className="mt-2 inline-flex h-11 items-center rounded-[10px] bg-spruce px-5 text-sm font-bold text-on-spruce hover:bg-spruce-deep"
                >
                  {t.results.retry}
                </button>
              </div>
            ) : items.length === 0 ? (
              <div className="flex flex-col items-center gap-3 rounded-[14px] border border-line bg-surface px-6 py-14 text-center">
                <span className="grid size-12 place-items-center rounded-full bg-paper text-ink-faint">
                  <SearchX aria-hidden="true" size={22} />
                </span>
                <h2 className="text-xl font-bold text-ink">
                  {filters.q ? t.results.emptySearchTitle(filters.q) : t.results.emptyTitle}
                </h2>
                <p className="max-w-md text-sm text-ink-soft">{t.results.emptyBody}</p>
                {narrowed ? (
                  <button
                    type="button"
                    onClick={clearFilters}
                    className="mt-2 inline-flex h-11 items-center rounded-[10px] bg-spruce px-5 text-sm font-bold text-on-spruce hover:bg-spruce-deep"
                  >
                    {t.filters.clear}
                  </button>
                ) : null}
              </div>
            ) : (
              <>
                <motion.ul className={GRID_CLASS} variants={gridMotion} initial={animateGrid ? "hidden" : false} animate="show">
                  {items.map((product, index) => (
                    <motion.li key={product.id} variants={cardMotion} className="min-w-0">
                      <ProductCard
                        product={product}
                        lang={lang}
                        standards={standards}
                        saved={saved.isSaved(product.id)}
                        highlighted={initialProduct?.id === product.id}
                        eager={index < 4}
                        onOpen={(item) => openProduct(item.id)}
                        onGetDeal={setLeavingFor}
                        onToggleSave={toggleSave}
                      />
                    </motion.li>
                  ))}
                  {loadingMore
                    ? Array.from({ length: 4 }, (_, index) => (
                        <li key={`more-${index}`}>
                          <SkeletonCard />
                        </li>
                      ))
                    : null}
                </motion.ul>

                {result && result.page < result.pages ? (
                  <div className="mt-8 flex flex-col items-center gap-2.5">
                    <p className="text-sm text-ink-soft">
                      {t.results.showing(formatNumber(result.items.length, lang), formatNumber(total, lang))}
                    </p>
                    <button
                      type="button"
                      onClick={loadMore}
                      disabled={loadingMore}
                      className="inline-flex h-11 items-center rounded-[10px] border border-spruce/30 bg-surface px-6 text-sm font-bold text-spruce transition-colors hover:border-spruce disabled:opacity-60"
                    >
                      {loadingMore ? t.results.loadingMore : t.results.more}
                    </button>
                  </div>
                ) : null}
              </>
            )}
          </section>
        </div>
      </main>

      <Footer lang={lang} catalog={catalog} />

      <Overlay
        open={sheetOpen}
        onClose={() => setSheetOpen(false)}
        labelledBy={sheetTitleId}
        dir={dir}
        className="max-h-[88dvh] w-full rounded-t-[22px] sm:max-w-md sm:rounded-[22px]"
      >
        <header className="flex items-center justify-between border-b border-line px-5 py-4">
          <h2 id={sheetTitleId} className="text-lg font-bold text-ink">
            {t.filters.title}
          </h2>
          <button
            type="button"
            onClick={() => setSheetOpen(false)}
            aria-label={t.filters.close}
            className="grid size-9 place-items-center rounded-lg text-ink-soft hover:bg-line/50 hover:text-ink"
          >
            <X aria-hidden="true" size={20} />
          </button>
        </header>
        <div className="overflow-y-auto overscroll-contain px-5 py-4">{sidebar}</div>
        <div className="border-t border-line p-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
          <button
            type="button"
            data-autofocus
            onClick={() => setSheetOpen(false)}
            className="inline-flex h-12 w-full items-center justify-center rounded-xl bg-spruce px-5 text-base font-bold text-on-spruce hover:bg-spruce-deep"
          >
            {status === "loading" ? t.results.loading : t.filters.show(formatNumber(total, lang), total)}
          </button>
        </div>
      </Overlay>

      <SavedDealsDrawer
        lang={lang}
        open={drawerOpen}
        deals={saved.deals}
        onClose={() => setDrawerOpen(false)}
        onOpenDeal={(productId) => {
          setDrawerOpen(false);
          openProduct(productId);
        }}
        onRemove={saved.remove}
        onClear={saved.clear}
      />

      <QuickViewModal
        lang={lang}
        open={activeId !== null}
        product={shownProduct}
        standards={standards}
        saved={shownProduct ? saved.isSaved(shownProduct.id) : false}
        couponCopied={shownProduct ? copiedCoupons.has(shownProduct.id) : false}
        onClose={closeProduct}
        onToggleSave={toggleSave}
        onCopyCoupon={copyCoupon}
        onCopyLink={copyLink}
        onGetDeal={setLeavingFor}
      />

      <RedirectConfirmModal
        lang={lang}
        product={leavingFor}
        couponCopied={leavingFor ? copiedCoupons.has(leavingFor.id) : false}
        referrerPath={pathname}
        onCopyCoupon={copyCoupon}
        onClose={() => setLeavingFor(null)}
      />
    </>
  );
}
