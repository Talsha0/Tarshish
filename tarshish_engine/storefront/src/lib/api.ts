// Typed client for the FindHub storefront API (api/server.py).
//
// In the browser every call is same-origin (`/api/...`, proxied by next.config.mjs).
// On the server (page rendering) the backend is called directly at BACKEND_API_URL (or API_ORIGIN).

export type Store = {
  /** Stable id used by the `source` filter, e.g. "aliexpress". */
  key: string;
  /** Display name, e.g. "AliExpress". */
  name: string;
};

export type Badge = "crowd_favorite" | "flash_deal" | "verified";
export type SocialProof = "high_demand" | "top_rated" | "most_ordered";

export type Product = {
  id: number;
  title: string;
  title_he: string | null;
  category: string;
  source: string;
  store: Store;
  /** Current price in `currency` (ILS in the catalogue). */
  price: number;
  original_price: number | null;
  discount_percent: number | null;
  currency: string;
  rating: number;
  orders_count: number;
  image_urls: string[];
  coupon_code: string | null;
  is_flash_sale: boolean;
  stock_urgency_label: string | null;
  badges: Badge[];
  social_proof: SocialProof | null;
};

export type StoreFacet = Store & { products: number };

export type ProductPage = {
  items: Product[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
  sort_by: string;
  facets: { stores: StoreFacet[]; price_min: number | null; price_max: number | null };
};

export type Category = { key: string; products: number };

export type Catalog = {
  categories: Category[];
  total: number;
  /** A product is listed only when it meets this standard. */
  standards: { min_rating: number; min_orders: number };
};

export const SORT_OPTIONS = ["rating_desc", "price_asc", "price_desc", "orders_desc", "discount_desc"] as const;
export type SortBy = (typeof SORT_OPTIONS)[number];

export type ProductQuery = {
  q?: string;
  category?: string | null;
  source?: string | null;
  minRating?: number | null;
  minOrders?: number | null;
  /** Price bounds in the catalogue currency (ILS). */
  minPrice?: number | null;
  maxPrice?: number | null;
  sortBy?: SortBy | null;
  page?: number;
  pageSize?: number;
};

export type ClickReceipt = { product_id: number; affiliate_url: string; store: Store };

export const PAGE_SIZE = 24;

export class ApiError extends Error {
  constructor(
    message: string,
    /** HTTP status, or 0 when the backend could not be reached at all. */
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function origin(): string {
  if (typeof window !== "undefined") return "";
  const origin = process.env.BACKEND_API_URL?.trim() || process.env.API_ORIGIN?.trim() || "http://127.0.0.1:8000";
  return origin.replace(/\/+$/, "");
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${origin()}${path}`, {
      cache: "no-store",
      ...init,
      headers: { Accept: "application/json", ...init.headers },
      signal: init.signal ?? AbortSignal.timeout(10_000),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError("The catalogue server is not answering.", 0);
  }
  if (!response.ok) {
    throw new ApiError(`The catalogue server answered ${response.status}.`, response.status);
  }
  return (await response.json()) as T;
}

export function productQueryString(query: ProductQuery): string {
  const params = new URLSearchParams();
  const set = (name: string, value: string | number | null | undefined) => {
    if (value !== null && value !== undefined && value !== "") params.set(name, String(value));
  };
  set("q", query.q?.trim());
  set("category", query.category);
  set("source", query.source);
  set("min_rating", query.minRating);
  set("min_orders", query.minOrders);
  set("min_price", query.minPrice);
  set("max_price", query.maxPrice);
  set("sort_by", query.sortBy);
  set("page", query.page && query.page > 1 ? query.page : null);
  set("page_size", query.pageSize ?? PAGE_SIZE);
  return params.toString();
}

export function getProducts(query: ProductQuery = {}, signal?: AbortSignal): Promise<ProductPage> {
  return request<ProductPage>(`/api/products?${productQueryString(query)}`, { signal });
}

export function getProduct(productId: number, signal?: AbortSignal): Promise<Product> {
  return request<Product>(`/api/products/${productId}`, { signal });
}

export function getCatalog(signal?: AbortSignal): Promise<Catalog> {
  return request<Catalog>("/api/categories", { signal });
}

/** Logs the confirmed outbound click and returns the link to open. */
export function trackClick(
  productId: number,
  context: { language: string; referrerPath: string },
): Promise<ClickReceipt> {
  return request<ClickReceipt>(`/api/track-click/${productId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ language: context.language, referrer_path: context.referrerPath }),
  });
}
