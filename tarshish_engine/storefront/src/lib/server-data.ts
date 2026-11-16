// What a storefront page needs before it can be drawn, fetched in parallel on the server.
// A backend that is down never breaks the page: the missing parts arrive as `null` and the
// storefront shows its "could not load" state with a retry button.

import { getCatalog, getProducts, type Catalog, type Product, type ProductPage } from "./api";
import { toProductQuery, type Filters } from "./filters";

export type StorefrontData = {
  initialPage: ProductPage | null;
  catalog: Catalog | null;
  featured: Product | null;
};

export async function loadStorefront(filters: Filters, scopeCategory: string | null): Promise<StorefrontData> {
  const [page, catalog, drops] = await Promise.allSettled([
    getProducts(toProductQuery(filters, scopeCategory)),
    getCatalog(),
    getProducts({ category: scopeCategory, sortBy: "discount_desc", pageSize: 1 }),
  ]);
  return {
    initialPage: page.status === "fulfilled" ? page.value : null,
    catalog: catalog.status === "fulfilled" ? catalog.value : null,
    featured: drops.status === "fulfilled" ? (drops.value.items[0] ?? null) : null,
  };
}

export type PageParams<T> = { params: Promise<T>; searchParams: Promise<Record<string, string | string[] | undefined>> };
