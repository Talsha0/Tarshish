// One shape for everything the Mobile Social Preview Simulator shows: generated FindHub assets
// (GET /api/admin/assets) and pipeline publications (GET /api/admin/queue) both become a PreviewItem,
// and each item carries the admin API routes that edit, publish and delete it.

import { CONTENT_TYPE_LABELS } from "@/lib/admin/client";
import type { GeneratedAsset, Publication, StoryLink } from "@/lib/admin/types";

/** Which phone skin renders the item. */
export type SimLayout = "instagram_post" | "instagram_story" | "vertical_video" | "telegram";
/** Platform chrome of the shared 9:16 vertical-video skin. */
export type VerticalVariant = "instagram_reel" | "tiktok" | "youtube_short";
export type SimSource = "asset" | "publication";

export type PreviewLinks = {
  storefrontUrl: string | null;
  productUrl: string | null;
  affiliateUrl: string | null;
  bioLink: string | null;
  bioCta: string | null;
  stickers: StoryLink[];
};

export type PreviewItem = {
  key: string;
  source: SimSource;
  id: number;
  layout: SimLayout;
  variant: VerticalVariant | null;
  platform: string;
  contentType: string;
  typeLabel: string;
  status: string;
  language: string;
  dir: "rtl" | "ltr";
  handle: string;
  hasAccount: boolean;
  categoryId: string;
  /** The editable text as saved: generated_text, an Instagram Reel's reel_caption, or a publication caption. */
  text: string;
  textField: "generated_text" | "reel_caption" | "caption";
  /** Appended after the editable text on screen (a publication's link, hashtags and disclosure). */
  suffix: string;
  imageUrl: string | null;
  videoUrl: string | null;
  productTitle: string;
  price: number | null;
  originalPrice: number | null;
  currency: string;
  links: PreviewLinks;
  canEdit: boolean;
  canPublish: boolean;
  publishBlockedReason: string | null;
  publishing: boolean;
  model: string;
  createdAt: string;
  errorMessage: string | null;
  /** Admin API routes (relative to /api/admin). */
  routes: { patch: string; publish: string; remove: string; approve: string | null };
};

const RTL_LANGUAGES = new Set(["he", "ar", "fa", "ur", "yi"]);

export function textDirection(language: string | null | undefined): "rtl" | "ltr" {
  return RTL_LANGUAGES.has(((language ?? "").split("-")[0] ?? "").toLowerCase()) ? "rtl" : "ltr";
}

/** Same-origin path for /media images (proxied to the API by next.config), the full URL otherwise. */
export function displayUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.pathname.startsWith("/media/") ? parsed.pathname : url;
  } catch {
    return url;
  }
}

export function money(amount: number | null, currency: string): string {
  if (amount === null || Number.isNaN(amount)) return "";
  try {
    return new Intl.NumberFormat("en", { style: "currency", currency, maximumFractionDigits: 2 }).format(amount);
  } catch {
    return `${amount.toFixed(2)} ${currency}`;
  }
}

function atHandle(handle: string | null | undefined): string {
  const clean = (handle ?? "").trim();
  if (!clean || clean === "default") return "@findhub";
  return clean.startsWith("@") ? clean : `@${clean}`;
}

const ASSET_LAYOUT: Record<GeneratedAsset["content_type"], [SimLayout, VerticalVariant | null]> = {
  instagram_post: ["instagram_post", null],
  instagram_story: ["instagram_story", null],
  instagram_reel: ["vertical_video", "instagram_reel"],
  tiktok_post: ["vertical_video", "tiktok"],
  telegram_post: ["telegram", null],
};

export function fromAsset(asset: GeneratedAsset): PreviewItem {
  const [layout, variant] = ASSET_LAYOUT[asset.content_type];
  const reel = asset.content_type === "instagram_reel";
  const links = asset.links ?? ({} as GeneratedAsset["links"]);
  const cta = links.cta;
  const status = asset.publishing ? "PENDING" : asset.status;
  const unpublished = asset.status === "DRAFT" || asset.status === "QUEUED" || asset.status === "FAILED";
  let blocked: string | null = null;
  if (reel) blocked = asset.not_publishable_reason ?? "Instagram publishes Reels from video only.";
  else if (!asset.target_account_id) blocked = asset.not_publishable_reason ?? `No ${asset.platform} account linked.`;
  else if (asset.publishing) blocked = "A publish is already in flight.";
  else if (!unpublished) blocked = `Status is ${asset.status}.`;
  return {
    key: `asset:${asset.id}`,
    source: "asset",
    id: asset.id,
    layout,
    variant,
    platform: asset.platform,
    contentType: asset.content_type,
    typeLabel: CONTENT_TYPE_LABELS[asset.content_type] ?? asset.content_type,
    status,
    language: asset.language,
    dir: textDirection(asset.language),
    handle: atHandle(asset.target_account_handle),
    hasAccount: Boolean(asset.target_account_id),
    categoryId: asset.category_id,
    text: reel ? (links.reel_caption ?? asset.generated_text) : asset.generated_text,
    textField: reel ? "reel_caption" : "generated_text",
    suffix: "",
    imageUrl: displayUrl(asset.generated_image_url) ?? displayUrl(asset.product_image_url),
    videoUrl: null,
    productTitle: asset.language === "he" && asset.product_title_he ? asset.product_title_he : asset.product_title,
    price: asset.product_price,
    originalPrice: asset.product_original_price,
    currency: asset.product_currency,
    links: {
      storefrontUrl: links.storefront_category_url ?? null,
      productUrl: links.storefront_product_url ?? null,
      affiliateUrl: links.affiliate_url ?? null,
      bioLink: cta?.kind === "link_in_bio" ? cta.bio_link : (links.storefront_category_url ?? null),
      bioCta: cta?.kind === "link_in_bio" ? cta.text : null,
      stickers: links.story_links ?? [],
    },
    canEdit: unpublished && !asset.publishing,
    canPublish: blocked === null,
    publishBlockedReason: blocked,
    publishing: asset.publishing,
    model: asset.llm_model_used,
    createdAt: asset.created_at,
    errorMessage: asset.error_message,
    routes: {
      patch: `assets/${asset.id}`,
      publish: `assets/${asset.id}/publish`,
      remove: `assets/${asset.id}`,
      approve: asset.status === "DRAFT" ? `assets/${asset.id}/status` : null,
    },
  };
}

const FORMAT_LAYOUT: Record<string, [SimLayout, VerticalVariant | null]> = {
  instagram_post: ["instagram_post", null],
  instagram_story: ["instagram_story", null],
  instagram_reel: ["vertical_video", "instagram_reel"],
  tiktok_video: ["vertical_video", "tiktok"],
  tiktok_post: ["vertical_video", "tiktok"],
  youtube_short: ["vertical_video", "youtube_short"],
  telegram_post: ["telegram", null],
};
const PLATFORM_LAYOUT: Record<string, [SimLayout, VerticalVariant | null]> = {
  instagram: ["instagram_post", null],
  tiktok: ["vertical_video", "tiktok"],
  youtube: ["vertical_video", "youtube_short"],
  telegram: ["telegram", null],
};

export function fromPublication(pub: Publication): PreviewItem {
  const platform = pub.account_platform ?? pub.platform.replace(/_pub$/, "");
  const [layout, variant] = FORMAT_LAYOUT[pub.content_format ?? ""] ?? PLATFORM_LAYOUT[platform] ?? ["telegram", null];
  const preview = pub.preview;
  const language = pub.language ?? "en";
  const editable = pub.status === "QUEUED" || pub.status === "FAILED";
  const category = pub.category ?? pub.pipeline_id.replace(/_[a-z]{2}$/, "");
  // Without a reliable suffix (caption shortened to fit), fall back to caption + hashtags.
  const suffix =
    preview?.post_suffix ?? ((preview?.hashtags ?? []).length ? `\n\n${preview!.hashtags.map((tag) => `#${tag}`).join(" ")}` : "");
  return {
    key: `pub:${pub.id}`,
    source: "publication",
    id: pub.id,
    layout,
    variant,
    platform,
    contentType: pub.content_format ?? pub.content_type,
    typeLabel: CONTENT_TYPE_LABELS[pub.content_format ?? ""] ?? pub.content_format ?? pub.content_type,
    status: pub.status,
    language,
    dir: textDirection(language),
    handle: atHandle(pub.target_page),
    hasAccount: true,
    categoryId: category,
    text: preview?.final_text ?? pub.caption,
    textField: "caption",
    suffix,
    imageUrl: displayUrl(preview?.image_url),
    videoUrl: preview?.video_url ?? null,
    productTitle: pub.product_title,
    price: preview?.price ?? null,
    originalPrice: preview?.original_price ?? null,
    currency: preview?.currency ?? "USD",
    links: {
      storefrontUrl: null,
      productUrl: null,
      affiliateUrl: preview?.affiliate_url ?? null,
      bioLink: null,
      bioCta: null,
      stickers: [],
    },
    canEdit: editable,
    canPublish: editable,
    publishBlockedReason: editable ? null : `Status is ${pub.status}.`,
    publishing: pub.status === "PENDING",
    model: pub.model_used,
    createdAt: pub.created_at,
    errorMessage: pub.error_message,
    routes: { patch: `queue/${pub.id}`, publish: `queue/${pub.id}/publish-now`, remove: `queue/${pub.id}`, approve: null },
  };
}

/** The PATCH body that saves `text` into the item's editable field. */
export function patchBody(item: PreviewItem, text: string): Record<string, string> {
  return { [item.textField]: text };
}

/** Localized app chrome (the audience sees the apps in the post's language). */
const CHROME = {
  he: {
    likes: (n: string) => `${n} סימוני לייק`,
    viewComments: (n: number) => `הצגת כל ${n} התגובות`,
    more: "עוד",
    less: "פחות",
    sendMessage: "שליחת הודעה",
    linkInBio: "הקישור בביו",
    sponsored: "ממומן",
    viewOnFindHub: "צפייה ב־FindHub",
    directDeal: "לדיל ישיר",
    subscribers: (n: string) => `${n} מנויים`,
    channel: "ערוץ",
    mute: "השתקה",
    follow: "מעקב",
    subscribe: "הרשמה",
    originalAudio: "אודיו מקורי",
    addComment: "הוספת תגובה...",
    shopNow: "לקנייה",
    hoursAgo: "לפני 2 שע׳",
    posts: "פוסטים",
    today: "היום",
    views: "צפיות",
  },
  en: {
    likes: (n: string) => `${n} likes`,
    viewComments: (n: number) => `View all ${n} comments`,
    more: "more",
    less: "less",
    sendMessage: "Send message",
    linkInBio: "Link in bio",
    sponsored: "Sponsored",
    viewOnFindHub: "View on FindHub",
    directDeal: "Direct Deal",
    subscribers: (n: string) => `${n} subscribers`,
    channel: "channel",
    mute: "Mute",
    follow: "Follow",
    subscribe: "Subscribe",
    originalAudio: "Original audio",
    addComment: "Add comment...",
    shopNow: "Shop now",
    hoursAgo: "2 hours ago",
    posts: "Posts",
    today: "Today",
    views: "views",
  },
};
export type Chrome = (typeof CHROME)["en"];

export function chromeFor(language: string): Chrome {
  return language.split("-")[0] === "he" ? CHROME.he : CHROME.en;
}

/** Stable pseudo-random engagement numbers per item (a preview should not flicker between renders). */
export function engagement(seed: number) {
  const rand = (salt: number) => {
    const x = Math.sin(seed * 9301 + salt * 49297) * 233280;
    return x - Math.floor(x);
  };
  return {
    likes: Math.round(800 + rand(1) * 14000),
    comments: Math.round(12 + rand(2) * 380),
    shares: Math.round(20 + rand(3) * 900),
    saves: Math.round(30 + rand(4) * 1200),
    views: Math.round(2500 + rand(5) * 60000),
    subscribers: Math.round(1200 + rand(6) * 40000),
  };
}

export function compact(n: number): string {
  return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(n);
}
