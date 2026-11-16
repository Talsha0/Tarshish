// Product deep link: /he/p/1, /en/p/7.
// Shows the product's own category hub with the product's Quick View already open and its
// card highlighted at the top of the grid. Closing Quick View leaves the shopper browsing
// that category. This is also the address the storefront puts in the bar whenever a deal
// is opened, so sharing or reloading an open deal always works.

import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { cache } from "react";

import { Storefront } from "@/components/Storefront";
import { ApiError, getProduct, type Product } from "@/lib/api";
import { EMPTY_FILTERS } from "@/lib/filters";
import { money, titleOf } from "@/lib/format";
import { getDictionary, isLang } from "@/lib/i18n";
import { loadStorefront } from "@/lib/server-data";

export const dynamic = "force-dynamic";

type ProductParams = { lang: string; productId: string };

const PRODUCT_ID = /^[1-9]\d{0,11}$/;

/** One backend call per request, shared by the metadata and the page. */
const fetchProduct = cache((productId: number) =>
  getProduct(productId).then(
    (product) => ({ product, status: 200 }),
    (error: unknown) => ({ product: null, status: error instanceof ApiError ? error.status : 0 }),
  ),
);

/** The product, `null` when the backend is unreachable, or a 404 when it is not listed. */
async function findProduct(productId: string): Promise<Product | null> {
  if (!PRODUCT_ID.test(productId)) notFound();
  const { product, status } = await fetchProduct(Number(productId));
  if (status === 404 || status === 422) notFound();
  return product;
}

export async function generateMetadata({ params }: { params: Promise<ProductParams> }): Promise<Metadata> {
  const { lang, productId } = await params;
  if (!isLang(lang) || !PRODUCT_ID.test(productId)) return {};
  const { product } = await fetchProduct(Number(productId));
  if (!product) return {};
  const t = getDictionary(lang);
  const title = `${titleOf(product, lang)} | FindHub`;
  const description = [
    money(product.price, product.currency, lang).text,
    t.quickView.soldOn(product.store.name),
    t.quickView.ratingOutOf(product.rating.toFixed(1)),
  ].join(". ");
  return {
    title,
    description,
    alternates: {
      canonical: `/${lang}/p/${product.id}`,
      languages: { he: `/he/p/${product.id}`, en: `/en/p/${product.id}` },
    },
    openGraph: { title, description, images: product.image_urls.slice(0, 1) },
  };
}

export default async function ProductPage({ params }: { params: Promise<ProductParams> }) {
  const { lang, productId } = await params;
  if (!isLang(lang)) notFound();
  const product = await findProduct(productId);
  const data = await loadStorefront(EMPTY_FILTERS, product?.category ?? null);
  return (
    <Storefront
      lang={lang}
      scopeCategory={product?.category ?? null}
      initialFilters={EMPTY_FILTERS}
      initialProduct={product}
      {...data}
    />
  );
}
