// What the shopper has narrowed the grid to, and how that state travels in the URL
// (so a filtered view can be shared, reloaded, or switched to the other language).

import { SORT_OPTIONS, type ProductQuery, type SortBy } from "./api";

export type Filters = {
  q: string;
  /** Category chosen on the main storefront. Category hubs fix it through their URL instead. */
  category: string | null;
  source: string | null;
  minRating: number | null;
  minOrders: number | null;
  /** Price bounds in the catalogue currency (ILS). */
  minPrice: number | null;
  maxPrice: number | null;
  sortBy: SortBy | null;
};

export const EMPTY_FILTERS: Filters = {
  q: "",
  category: null,
  source: null,
  minRating: null,
  minOrders: null,
  minPrice: null,
  maxPrice: null,
  sortBy: null,
};

export const RATING_STEPS = [4.7, 4.8, 4.9] as const;
export const ORDER_STEPS = [500, 1000, 3000] as const;

type RawParams = Record<string, string | string[] | undefined>;

function first(value: string | string[] | undefined): string {
  return (Array.isArray(value) ? value[0] : value)?.trim() ?? "";
}

function positive(value: string): number | null {
  const number = Number(value);
  return value !== "" && Number.isFinite(number) && number > 0 ? number : null;
}

const SLUG = /^[\p{L}\p{N}][\p{L}\p{N}_-]{0,63}$/u;

export function parseFilters(params: RawParams): Filters {
  const sort = first(params.sort);
  const rating = positive(first(params.rating));
  const category = first(params.category).toLowerCase();
  const source = first(params.source).toLowerCase();
  return {
    q: first(params.q).slice(0, 80),
    category: SLUG.test(category) ? category : null,
    source: SLUG.test(source) ? source : null,
    minRating: rating !== null && rating <= 5 ? rating : null,
    minOrders: positive(first(params.orders)),
    minPrice: positive(first(params.min)),
    maxPrice: positive(first(params.max)),
    sortBy: (SORT_OPTIONS as readonly string[]).includes(sort) ? (sort as SortBy) : null,
  };
}

/** `?q=...&rating=4.8`, or an empty string when nothing is narrowed. */
export function filtersToSearch(filters: Filters, includeCategory: boolean): string {
  const params = new URLSearchParams();
  const set = (name: string, value: string | number | null) => {
    if (value !== null && value !== "") params.set(name, String(value));
  };
  set("q", filters.q.trim());
  if (includeCategory) set("category", filters.category);
  set("source", filters.source);
  set("rating", filters.minRating);
  set("orders", filters.minOrders);
  set("min", filters.minPrice);
  set("max", filters.maxPrice);
  set("sort", filters.sortBy);
  const search = params.toString();
  return search ? `?${search}` : "";
}

/** Filters set in the sidebar (search text, category and sorting are shown elsewhere). */
export function countSidebarFilters(filters: Filters): number {
  return [filters.source, filters.minRating, filters.minOrders, filters.minPrice ?? filters.maxPrice].filter(
    (value) => value !== null,
  ).length;
}

export function isNarrowed(filters: Filters): boolean {
  return filters.q.trim() !== "" || filters.category !== null || countSidebarFilters(filters) > 0;
}

export function toProductQuery(filters: Filters, scopeCategory: string | null, page = 1): ProductQuery {
  return {
    q: filters.q,
    category: scopeCategory ?? filters.category,
    source: filters.source,
    minRating: filters.minRating,
    minOrders: filters.minOrders,
    minPrice: filters.minPrice,
    maxPrice: filters.maxPrice,
    sortBy: filters.sortBy,
    page,
  };
}
