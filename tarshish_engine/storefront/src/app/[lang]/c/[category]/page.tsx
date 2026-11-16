// Category hub: /he/c/sports, /en/c/gadgets.
// A page of its own for one category, made to be linked from a social bio: its own
// headline, a grid that only holds that category, and a search scoped to it.

import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { Storefront } from "@/components/Storefront";
import { parseFilters } from "@/lib/filters";
import { categoryName, getDictionary, hubCopy, isLang } from "@/lib/i18n";
import { loadStorefront, type PageParams } from "@/lib/server-data";

export const dynamic = "force-dynamic";

type HubParams = { lang: string; category: string };

function categoryKey(raw: string): string {
  try {
    return decodeURIComponent(raw).toLowerCase();
  } catch {
    return raw.toLowerCase();
  }
}

export async function generateMetadata({ params }: { params: Promise<HubParams> }): Promise<Metadata> {
  const { lang, category } = await params;
  if (!isLang(lang)) return {};
  const key = categoryKey(category);
  const t = getDictionary(lang);
  const title = t.meta.hubTitle(categoryName(lang, key));
  const description = hubCopy(lang, key).blurb;
  const path = (code: string) => `/${code}/c/${encodeURIComponent(key)}`;
  return {
    title,
    description,
    alternates: { canonical: path(lang), languages: { he: path("he"), en: path("en") } },
    openGraph: { title, description },
  };
}

export default async function CategoryHubPage({ params, searchParams }: PageParams<HubParams>) {
  const { lang, category } = await params;
  if (!isLang(lang)) notFound();
  const key = categoryKey(category);
  const filters = { ...parseFilters(await searchParams), category: null };
  const data = await loadStorefront(filters, key);
  // An address for a category that does not exist is a 404; a backend that is down is not.
  if (data.catalog && !data.catalog.categories.some((entry) => entry.key === key)) notFound();
  return <Storefront lang={lang} scopeCategory={key} initialFilters={filters} {...data} />;
}
