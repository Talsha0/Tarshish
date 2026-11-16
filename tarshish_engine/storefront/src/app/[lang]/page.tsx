// Main storefront: /he, /en. Every listed deal, with the category pill bar on top.

import { notFound } from "next/navigation";

import { Storefront } from "@/components/Storefront";
import { parseFilters } from "@/lib/filters";
import { isLang } from "@/lib/i18n";
import { loadStorefront, type PageParams } from "@/lib/server-data";

// Prices, stock labels and "high demand" badges change by the minute: render per request.
export const dynamic = "force-dynamic";

export default async function StorefrontPage({ params, searchParams }: PageParams<{ lang: string }>) {
  const { lang } = await params;
  if (!isLang(lang)) notFound();
  const filters = parseFilters(await searchParams);
  const data = await loadStorefront(filters, null);
  return <Storefront lang={lang} scopeCategory={null} initialFilters={filters} {...data} />;
}
