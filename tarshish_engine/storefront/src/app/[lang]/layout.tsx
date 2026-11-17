import type { Metadata, Viewport } from "next";
import { Assistant, Frank_Ruhl_Libre } from "next/font/google";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { THEME_SCRIPT } from "@/components/ThemeToggle";
import { Providers } from "@/components/Toaster";
import { LANGS, dirOf, getDictionary, isLang } from "@/lib/i18n";

import "../globals.css";

// Two families, both drawn for Hebrew and Latin. Assistant carries the interface and the
// prices; Frank Ruhl Libre, the classic Hebrew book face, is kept for the headlines.
const sans = Assistant({ subsets: ["hebrew", "latin"], variable: "--font-sans", display: "swap" });
const display = Frank_Ruhl_Libre({
  subsets: ["hebrew", "latin"],
  weight: ["700", "900"],
  variable: "--font-display",
  display: "swap",
});

type LayoutProps = { children: ReactNode; params: Promise<{ lang: string }> };

/** Public address for canonical links and social previews: NEXT_PUBLIC_SITE_URL, else the Vercel production domain. */
function siteUrl(): string {
  const configured = process.env.NEXT_PUBLIC_SITE_URL?.trim();
  if (configured) return configured;
  const vercel = process.env.VERCEL_PROJECT_PRODUCTION_URL?.trim();
  return vercel ? `https://${vercel}` : "http://localhost:3000";
}

export function generateStaticParams() {
  return LANGS.map((lang) => ({ lang }));
}

export async function generateMetadata({ params }: { params: Promise<{ lang: string }> }): Promise<Metadata> {
  const { lang } = await params;
  if (!isLang(lang)) return {};
  const t = getDictionary(lang);
  return {
    metadataBase: new URL(siteUrl()),
    title: t.meta.title,
    description: t.meta.description,
    applicationName: "FindHub",
    alternates: { canonical: `/${lang}`, languages: { he: "/he", en: "/en" } },
    openGraph: {
      type: "website",
      siteName: "FindHub",
      title: t.meta.title,
      description: t.meta.description,
      locale: lang === "he" ? "he_IL" : "en_US",
    },
  };
}

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#0F4C3A" },
    { media: "(prefers-color-scheme: dark)", color: "#0C1210" },
  ],
  colorScheme: "light dark",
};

export default async function LangLayout({ children, params }: LayoutProps) {
  const { lang } = await params;
  if (!isLang(lang)) notFound();
  return (
    // suppressHydrationWarning: THEME_SCRIPT may set data-theme on <html> before React hydrates.
    <html lang={lang} dir={dirOf(lang)} className={`${sans.variable} ${display.variable}`} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className="min-h-dvh">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
