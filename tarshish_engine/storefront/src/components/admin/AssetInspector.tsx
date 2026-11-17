"use client";

// Generated FindHub content: every asset with its branded image, destination, copy, links and the free
// model that wrote it. Generate a batch from real products, then approve and publish (one or many).

import {
  Bot,
  Camera,
  Check,
  CheckCheck,
  CircleDashed,
  Clapperboard,
  Copy,
  Download,
  ExternalLink,
  ImageOff,
  LoaderCircle,
  Maximize2,
  Music2,
  RefreshCw,
  Send,
  Smartphone,
  Trash2,
  Undo2,
  WandSparkles,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { adminApi, timeAgo } from "@/lib/admin/client";
import type { AssetContentType, AssetsResponse, AssetStatus, Category, FreeLLMLink, GeneratedAsset } from "@/lib/admin/types";

import { useJobs } from "./jobs";
import { Badge, ErrorNote, Field, Loading, Modal, Panel, StatusBadge, useAction, useResource, useToast } from "./ui";

const CONTENT_TYPES: { value: AssetContentType; label: string; frame: string; icon: typeof Send }[] = [
  { value: "telegram_post", label: "Telegram", frame: "text post", icon: Send },
  { value: "instagram_post", label: "Instagram Post", frame: "1080×1350 · 4:5", icon: Camera },
  { value: "instagram_story", label: "Instagram Story", frame: "1080×1920 · 9:16", icon: CircleDashed },
  { value: "instagram_reel", label: "Instagram Reel", frame: "1080×1920 · 9:16", icon: Clapperboard },
  { value: "tiktok_post", label: "TikTok", frame: "1080×1920 · 9:16", icon: Music2 },
];
const TYPE = Object.fromEntries(CONTENT_TYPES.map((t) => [t.value, t])) as Record<AssetContentType, (typeof CONTENT_TYPES)[number]>;

const STATUS_TABS: { value: string; label: string }[] = [
  { value: "", label: "All" },
  { value: "QUEUED", label: "Queued" },
  { value: "DRAFT", label: "Draft" },
  { value: "FAILED", label: "Failed" },
  { value: "PUBLISHED", label: "Published" },
];

/** Same-origin path for /media images (proxied to the API by next.config), full URL for bucket images. */
function displayUrl(url: string | null): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.pathname.startsWith("/media/") ? parsed.pathname : url;
  } catch {
    return url;
  }
}

function money(amount: number, currency: string): string {
  try {
    return new Intl.NumberFormat("en", { style: "currency", currency }).format(amount);
  } catch {
    return `${amount.toFixed(2)} ${currency}`;
  }
}

function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const toast = useToast();
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="rounded p-1 text-adm-faint hover:bg-adm-raised hover:text-adm-ink"
      title={label}
      aria-label={label}
      onClick={() =>
        void navigator.clipboard.writeText(text).then(
          () => {
            setDone(true);
            window.setTimeout(() => setDone(false), 1500);
          },
          () => toast("error", "Clipboard is not available in this browser."),
        )
      }
    >
      {done ? <Check className="h-3.5 w-3.5 text-adm-ok" /> : <Copy className="h-3.5 w-3.5" />}
    </button>
  );
}

function LinkRow({ label, url }: { label: string; url: string }) {
  return (
    <div className="flex min-w-0 items-center gap-1.5 text-xs">
      <span className="shrink-0 text-adm-faint">{label}</span>
      <a href={url} target="_blank" rel="noreferrer noopener" className="min-w-0 truncate text-adm-accent hover:underline" title={url}>
        {url}
      </a>
      <CopyButton text={url} label={`Copy ${label}`} />
    </div>
  );
}

function LinksPreview({ asset }: { asset: GeneratedAsset }) {
  const { links } = asset;
  const cta = links.cta;
  let body: ReactNode;
  if (cta?.kind === "telegram_links") {
    body = (
      <>
        <LinkRow label="Affiliate" url={links.affiliate_url} />
        <LinkRow label="FindHub" url={links.storefront_product_url} />
      </>
    );
  } else if (cta?.kind === "link_in_bio") {
    body = (
      <>
        <p className="text-xs" dir="auto">
          <span className="font-medium">Bio CTA:</span> {cta.text}
        </p>
        <LinkRow label="Bio link" url={cta.bio_link} />
      </>
    );
  } else {
    body = (
      <>
        {(links.story_links ?? []).map((sticker) => (
          <LinkRow key={sticker.kind} label={`Sticker · ${sticker.label}`} url={sticker.url} />
        ))}
        <p className="text-[11px] text-adm-faint">
          {links.story_links_note ?? "Meta's API cannot attach link stickers: add them in the Instagram app when posting by hand."}
        </p>
      </>
    );
  }
  return <div className="space-y-1 rounded-lg border border-adm-line bg-adm-bg p-2">{body}</div>;
}

function Lightbox({ asset, onClose }: { asset: GeneratedAsset; onClose: () => void }) {
  const [size, setSize] = useState<string | null>(null);
  const src = displayUrl(asset.generated_image_url);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-[70] flex flex-col items-center justify-center gap-3 bg-black/85 p-4" role="dialog" aria-modal="true" onClick={onClose}>
      <button type="button" className="absolute right-4 top-4 rounded-full bg-white/10 p-2 text-white hover:bg-white/20" aria-label="Close" onClick={onClose}>
        <X className="h-5 w-5" />
      </button>
      {src && (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={src}
          alt={`${TYPE[asset.content_type].label} for ${asset.product_title}`}
          className="max-h-[85vh] max-w-full rounded-lg object-contain shadow-2xl"
          onClick={(event) => event.stopPropagation()}
          onLoad={(event) => setSize(`${event.currentTarget.naturalWidth}×${event.currentTarget.naturalHeight}`)}
        />
      )}
      <p className="text-center text-xs text-white/80">
        #{asset.id} · {TYPE[asset.content_type].label} · rendered {size ?? "…"} (expected {TYPE[asset.content_type].frame})
      </p>
    </div>
  );
}

function ChainStrip({ chain }: { chain: FreeLLMLink[] }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
      <span className="font-semibold uppercase tracking-wide text-adm-faint">Free LLM chain</span>
      {chain.map((link, index) => {
        const tone = !link.configured ? "neutral" : link.cooling_down_seconds ? "warn" : "ok";
        const state = !link.configured
          ? `${link.env_var} missing`
          : link.cooling_down_seconds
            ? `${link.last_problem ?? "cooling down"} · ${Math.ceil(link.cooling_down_seconds / 60)} min`
            : "ready";
        return (
          <Badge key={`${link.provider}:${link.model}`} tone={tone} title={`${link.label} · ${link.model}`}>
            {index + 1}. {link.model.replace(/^meta-llama\//, "")} · {state}
          </Badge>
        );
      })}
    </div>
  );
}

function AssetCard({
  asset,
  selected,
  onSelect,
  onZoom,
  onPreview,
  onChanged,
}: {
  asset: GeneratedAsset;
  selected: boolean;
  onSelect: (value: boolean) => void;
  onZoom: () => void;
  onPreview?: () => void;
  onChanged: () => void;
}) {
  const { start } = useJobs();
  const { busy, run } = useAction();
  const [expanded, setExpanded] = useState(false);
  const type = TYPE[asset.content_type];
  const Icon = type.icon;
  const src = displayUrl(asset.generated_image_url);
  const vertical = asset.content_type !== "instagram_post";
  const manualOnly = asset.content_type === "instagram_story" || asset.content_type === "instagram_reel";
  const title = asset.language === "he" && asset.product_title_he ? asset.product_title_he : asset.product_title;
  // Unpublished assets open in the phone preview; published ones in the full-resolution lightbox.
  const reviewable = Boolean(onPreview) && asset.status !== "PUBLISHED";

  return (
    <article className={`adm-card flex flex-col overflow-hidden ${selected ? "ring-2 ring-adm-accent" : ""}`}>
      {/* Destination routing */}
      <header className="flex flex-wrap items-center gap-1.5 border-b border-adm-line px-3 py-2">
        {asset.publishable && (
          <input
            type="checkbox"
            className="h-4 w-4 accent-[rgb(var(--adm-accent))]"
            checked={selected}
            onChange={(event) => onSelect(event.target.checked)}
            aria-label={`Select asset #${asset.id}`}
          />
        )}
        <Badge tone="accent">
          <Icon className="h-3 w-3" /> {type.label}
        </Badge>
        <Badge tone={asset.target_account_handle ? "neutral" : "warn"} title={asset.target_account_id ?? "No account linked"}>
          {asset.target_account_handle ?? "no account"}
        </Badge>
        <Badge>{asset.category_id}</Badge>
        <Badge>{asset.language.toUpperCase()}</Badge>
        <span className="ml-auto flex items-center gap-1.5">
          <StatusBadge status={asset.publishing ? "RUNNING" : asset.status} />
          <span className="text-[11px] text-adm-faint">#{asset.id}</span>
        </span>
      </header>

      <div className="flex gap-3 p-3">
        {/* Visual */}
        <button
          type="button"
          onClick={reviewable ? onPreview : src ? onZoom : undefined}
          className={`group relative shrink-0 overflow-hidden rounded-lg border border-adm-line bg-adm-bg ${vertical ? "aspect-[9/16] w-28" : "aspect-[4/5] w-32"}`}
          aria-label={reviewable ? "Open in the phone preview" : src ? "Open the full-resolution image" : "Text post (no image)"}
          disabled={!src && !reviewable}
        >
          {src ? (
            <>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={src} alt="" loading="lazy" className="h-full w-full object-cover" />
              <span className="absolute inset-0 flex items-center justify-center bg-black/0 text-white opacity-0 transition group-hover:bg-black/40 group-hover:opacity-100">
                {reviewable ? <Smartphone className="h-5 w-5" /> : <Maximize2 className="h-5 w-5" />}
              </span>
            </>
          ) : asset.content_type === "telegram_post" ? (
            <span className="flex h-full flex-col items-center justify-center gap-1 p-2 text-center text-[11px] text-adm-faint">
              {reviewable ? <Smartphone className="h-5 w-5" /> : <Send className="h-5 w-5" />} Text post · link preview
            </span>
          ) : (
            <span className="flex h-full items-center justify-center text-adm-faint">
              <ImageOff className="h-5 w-5" />
            </span>
          )}
        </button>

        {/* Facts */}
        <div className="min-w-0 flex-1 space-y-1">
          <p className="line-clamp-2 text-sm font-medium" dir="auto" title={asset.product_title}>
            {title}
          </p>
          <p className="text-xs text-adm-soft">
            {money(asset.product_price, asset.product_currency)}
            {asset.product_original_price && asset.product_original_price > asset.product_price && (
              <span className="ml-1 text-adm-faint line-through">{money(asset.product_original_price, asset.product_currency)}</span>
            )}
            {asset.product_rating ? ` · ★ ${asset.product_rating.toFixed(1)}` : ""}
            {asset.product_orders_count ? ` · ${asset.product_orders_count.toLocaleString()} sold` : ""}
            {` · ${asset.product_source}`}
          </p>
          <p className="flex items-center gap-1 text-[11px] text-adm-faint" title={(asset.links.llm_failover ?? []).join("\n") || undefined}>
            <Bot className="h-3 w-3" /> {asset.llm_model_used}
            {asset.links.llm_failover?.length ? <Badge tone="warn">failover ×{asset.links.llm_failover.length}</Badge> : null}
          </p>
          <p className="text-[11px] text-adm-faint">
            {type.frame} · created {timeAgo(asset.created_at)}
            {asset.published_at ? ` · published ${timeAgo(asset.published_at)}` : ""}
            {asset.platform_post_id ? ` · post ${asset.platform_post_id}` : ""}
          </p>
        </div>
      </div>

      {/* Copy */}
      <div className="space-y-2 px-3 pb-3">
        <div className="relative">
          <pre
            dir="auto"
            style={{ unicodeBidi: "plaintext" }}
            className={`whitespace-pre-wrap break-words rounded-lg bg-adm-bg p-2.5 font-sans text-xs leading-relaxed ${expanded ? "" : "line-clamp-6"}`}
          >
            {asset.generated_text}
          </pre>
          <div className="mt-1 flex items-center gap-2">
            <button type="button" className="text-[11px] text-adm-accent hover:underline" onClick={() => setExpanded(!expanded)}>
              {expanded ? "Collapse" : "Full caption"}
            </button>
            <CopyButton text={asset.generated_text} label="Copy caption" />
            {asset.links.reel_caption && <CopyButton text={asset.links.reel_caption} label="Copy Reel caption" />}
          </div>
        </div>
        <LinksPreview asset={asset} />
        {asset.error_message && <p className="rounded-lg bg-adm-bad/10 p-2 text-xs text-adm-bad">{asset.error_message}</p>}
        {!asset.publishable && asset.not_publishable_reason && asset.status !== "PUBLISHED" && (
          <p className="text-[11px] text-adm-warn">{asset.not_publishable_reason}</p>
        )}
      </div>

      {/* Actions */}
      <footer className="mt-auto flex flex-wrap items-center gap-1.5 border-t border-adm-line px-3 py-2">
        <button
          type="button"
          className="adm-btn-primary px-2.5 py-1 text-xs"
          disabled={!asset.publishable}
          title={asset.not_publishable_reason ?? "Publish through the platform API now"}
          onClick={() => void start(`assets/${asset.id}/publish`, {}, `Publish asset #${asset.id}`).then(() => onChanged())}
        >
          <Send className="h-3.5 w-3.5" /> Approve &amp; Publish Now
        </button>
        {asset.status === "DRAFT" && asset.target_account_id && (
          <button
            type="button"
            className="adm-btn px-2 py-1 text-xs"
            disabled={busy !== null}
            onClick={() => void run("queue", () => adminApi(`assets/${asset.id}/status`, { method: "POST", body: { status: "QUEUED" } }), `#${asset.id} queued`).then((ok) => ok && onChanged())}
          >
            <CheckCheck className="h-3.5 w-3.5" /> Queue
          </button>
        )}
        {(asset.status === "QUEUED" || asset.status === "FAILED") && !asset.publishing && (
          <button
            type="button"
            className="adm-btn px-2 py-1 text-xs"
            disabled={busy !== null}
            title="Keep it out of bulk publishing"
            onClick={() => void run("draft", () => adminApi(`assets/${asset.id}/status`, { method: "POST", body: { status: "DRAFT" } }), `#${asset.id} moved to drafts`).then((ok) => ok && onChanged())}
          >
            <Undo2 className="h-3.5 w-3.5" /> Draft
          </button>
        )}
        {manualOnly && asset.status !== "PUBLISHED" && !asset.publishing && (
          <button
            type="button"
            className="adm-btn px-2 py-1 text-xs"
            disabled={busy !== null}
            title="Posted from the Instagram app (with link stickers)"
            onClick={() => void run("manual", () => adminApi(`assets/${asset.id}/mark-published`, { method: "POST" }), `#${asset.id} marked as published`).then((ok) => ok && onChanged())}
          >
            <Check className="h-3.5 w-3.5" /> Posted manually
          </button>
        )}
        {reviewable && (
          <button type="button" className="adm-btn px-2 py-1 text-xs" onClick={onPreview} title="Phone preview" aria-label={`Preview asset #${asset.id} on a phone`}>
            <Smartphone className="h-3.5 w-3.5" />
          </button>
        )}
        {src && (
          <a className="adm-btn px-2 py-1 text-xs" href={src} download target="_blank" rel="noreferrer noopener" title="Download the image">
            <Download className="h-3.5 w-3.5" />
          </a>
        )}
        {asset.generated_image_url && (
          <a className="adm-btn px-2 py-1 text-xs" href={asset.generated_image_url} target="_blank" rel="noreferrer noopener" title="Public URL the platform downloads">
            <ExternalLink className="h-3.5 w-3.5" />
          </a>
        )}
        {!asset.publishing && (
          <button
            type="button"
            className="adm-btn-danger ml-auto px-2 py-1 text-xs"
            disabled={busy !== null}
            aria-label={`Delete asset #${asset.id}`}
            onClick={() => {
              if (window.confirm(`Delete asset #${asset.id} and its image?`))
                void run("delete", () => adminApi(`assets/${asset.id}`, { method: "DELETE" }), `#${asset.id} deleted`).then((ok) => ok && onChanged());
            }}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
      </footer>
    </article>
  );
}

function GenerateModal({ categories, onClose, onStarted }: { categories: Category[]; onClose: () => void; onStarted: () => void }) {
  const { start } = useJobs();
  const [category, setCategory] = useState(categories.find((c) => c.enabled)?.category_id ?? categories[0]?.category_id ?? "");
  const [language, setLanguage] = useState<"he" | "en">("he");
  const [types, setTypes] = useState<AssetContentType[]>(CONTENT_TYPES.map((t) => t.value));
  const [limit, setLimit] = useState(3);
  const [force, setForce] = useState(false);
  const toggle = (value: AssetContentType) => setTypes((list) => (list.includes(value) ? list.filter((t) => t !== value) : [...list, value]));

  return (
    <Modal title="Generate batch from real products" onClose={onClose}>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          onClose();
          void start(
            "assets/generate",
            { category_id: category, language, content_types: types, limit, force },
            `Generate ${category}/${language}`,
          ).then(() => onStarted());
        }}
      >
        <p className="text-xs text-adm-soft">
          Only harvested Temu and AliExpress products are used (never the mock catalogue). Each product costs one call on the free LLM chain, whatever the
          number of content types.
        </p>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Category">
            <select className="adm-input" value={category} onChange={(e) => setCategory(e.target.value)} required>
              {categories.map((c) => (
                <option key={c.category_id} value={c.category_id}>
                  {c.display_name_en} ({(c.products.temu ?? 0) + (c.products.aliexpress ?? 0)} real)
                </option>
              ))}
            </select>
          </Field>
          <Field label="Language">
            <div className="flex rounded-lg border border-adm-line bg-adm-bg p-0.5">
              {(["he", "en"] as const).map((code) => (
                <button
                  key={code}
                  type="button"
                  onClick={() => setLanguage(code)}
                  className={`flex-1 rounded-md px-3 py-1 text-sm ${language === code ? "bg-adm-panel font-semibold shadow-sm" : "text-adm-soft"}`}
                >
                  {code === "he" ? "HE · עברית" : "EN · English"}
                </button>
              ))}
            </div>
          </Field>
        </div>
        <fieldset>
          <legend className="adm-label">Content types</legend>
          <div className="grid gap-1.5 sm:grid-cols-2">
            {CONTENT_TYPES.map((t) => (
              <label key={t.value} className="flex cursor-pointer items-center gap-2 rounded-lg border border-adm-line px-2.5 py-1.5 text-sm hover:bg-adm-raised">
                <input type="checkbox" checked={types.includes(t.value)} onChange={() => toggle(t.value)} className="accent-[rgb(var(--adm-accent))]" />
                <t.icon className="h-3.5 w-3.5 text-adm-soft" />
                <span>{t.label}</span>
                <span className="ml-auto text-[11px] text-adm-faint">{t.frame}</span>
              </label>
            ))}
          </div>
        </fieldset>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Products per content type" hint="Best sellers first; products that already have this asset are skipped.">
            <input type="number" min={1} max={20} className="adm-input" value={limit} onChange={(e) => setLimit(Math.min(20, Math.max(1, Number(e.target.value) || 1)))} />
          </Field>
          <label className="flex items-center gap-2 self-center text-sm">
            <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} className="accent-[rgb(var(--adm-accent))]" />
            Regenerate products that already have assets
          </label>
        </div>
        <div className="flex justify-end gap-2">
          <button type="button" className="adm-btn" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="adm-btn-primary" disabled={!category || types.length === 0}>
            <WandSparkles className="h-4 w-4" /> Generate into queue
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function AssetInspector({ onPreview, refreshKey = 0 }: { onPreview?: (assetId: number) => void; refreshKey?: number } = {}) {
  const { start, finishedCount } = useJobs();
  const [status, setStatus] = useState("");
  const [contentType, setContentType] = useState("");
  const [category, setCategory] = useState("");
  const [language, setLanguage] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [zoom, setZoom] = useState<GeneratedAsset | null>(null);
  const [generating, setGenerating] = useState(false);
  const categories = useResource<{ categories: Category[] }>("categories");

  const params = new URLSearchParams({ limit: "120" });
  if (status) params.set("status", status);
  if (contentType) params.set("content_type", contentType);
  if (category) params.set("category", category);
  if (language) params.set("language", language);
  const assets = useResource<AssetsResponse>(`assets?${params.toString()}`, 15_000);
  useEffect(() => {
    if (finishedCount || refreshKey) void assets.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finishedCount, refreshKey]);

  const items = useMemo(() => assets.data?.items ?? [], [assets.data]);
  const publishable = items.filter((item) => item.publishable);
  const chosen = [...selected].filter((id) => publishable.some((item) => item.id === id));
  useEffect(() => {
    setSelected((current) => new Set([...current].filter((id) => items.some((item) => item.id === id && item.publishable))));
  }, [items]);

  const counts = assets.data?.counts;
  const publishSelected = () =>
    void start("assets/publish", { ids: chosen }, `Publish ${chosen.length} asset(s)`).then(() => {
      setSelected(new Set());
      void assets.reload();
    });

  return (
    <Panel
      className="mb-5"
      title="FindHub content inspector"
      subtitle="Branded images, captions and links generated from real products — check the framing, destination and links, then approve."
      actions={
        <>
          <button type="button" className="adm-btn" onClick={() => void assets.reload()}>
            <RefreshCw className="h-4 w-4" /> Refresh
          </button>
          <button type="button" className="adm-btn" disabled={chosen.length === 0} onClick={publishSelected}>
            <Send className="h-4 w-4" /> Publish selected ({chosen.length})
          </button>
          <button type="button" className="adm-btn-primary" onClick={() => setGenerating(true)} disabled={!categories.data}>
            {categories.data ? <WandSparkles className="h-4 w-4" /> : <LoaderCircle className="h-4 w-4 animate-spin" />} Generate Batch Now
          </button>
        </>
      }
    >
      {assets.data && <ChainStrip chain={assets.data.llm_chain} />}

      <div className="my-3 flex flex-wrap items-center gap-2">
        <div className="flex rounded-lg border border-adm-line bg-adm-panel p-0.5">
          {STATUS_TABS.map((tab) => (
            <button
              key={tab.label}
              type="button"
              onClick={() => setStatus(tab.value)}
              className={`rounded-md px-3 py-1 text-xs ${status === tab.value ? "bg-adm-raised font-semibold" : "text-adm-soft hover:text-adm-ink"}`}
            >
              {tab.label}
              {tab.value && counts && <span className="ml-1 text-adm-faint">{counts[tab.value as AssetStatus] ?? 0}</span>}
            </button>
          ))}
        </div>
        <select className="adm-input w-auto" value={contentType} onChange={(e) => setContentType(e.target.value)} aria-label="Content type">
          <option value="">All content types</option>
          {CONTENT_TYPES.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label}
            </option>
          ))}
        </select>
        <select className="adm-input w-auto" value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Category">
          <option value="">All categories</option>
          {(categories.data?.categories ?? []).map((c) => (
            <option key={c.category_id} value={c.category_id}>
              {c.display_name_en}
            </option>
          ))}
        </select>
        <select className="adm-input w-auto" value={language} onChange={(e) => setLanguage(e.target.value)} aria-label="Language">
          <option value="">HE + EN</option>
          <option value="he">HE</option>
          <option value="en">EN</option>
        </select>
        {publishable.length > 0 && (
          <button
            type="button"
            className="text-xs text-adm-accent hover:underline"
            onClick={() => setSelected(chosen.length === publishable.length ? new Set() : new Set(publishable.map((item) => item.id)))}
          >
            {chosen.length === publishable.length ? "Clear selection" : `Select all publishable (${publishable.length})`}
          </button>
        )}
      </div>

      {assets.error && <ErrorNote error={assets.error} onRetry={() => void assets.reload()} />}
      {!assets.data && !assets.error && <Loading />}
      {assets.data && items.length === 0 && (
        <p className="py-6 text-center text-sm text-adm-soft">No generated content for these filters yet. Use “Generate Batch Now”.</p>
      )}
      {items.length > 0 && (
        <div className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3">
          {items.map((asset) => (
            <AssetCard
              key={asset.id}
              asset={asset}
              selected={selected.has(asset.id)}
              onSelect={(value) =>
                setSelected((current) => {
                  const next = new Set(current);
                  if (value) next.add(asset.id);
                  else next.delete(asset.id);
                  return next;
                })
              }
              onZoom={() => setZoom(asset)}
              onPreview={onPreview ? () => onPreview(asset.id) : undefined}
              onChanged={() => void assets.reload()}
            />
          ))}
        </div>
      )}

      {zoom && <Lightbox asset={zoom} onClose={() => setZoom(null)} />}
      {generating && categories.data && (
        <GenerateModal categories={categories.data.categories} onClose={() => setGenerating(false)} onStarted={() => void assets.reload()} />
      )}
    </Panel>
  );
}
