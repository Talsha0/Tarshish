"use client";

// Phone Preview Mode: every unpublished post (generated FindHub assets in DRAFT / QUEUED and the
// pipeline's queued publications) inside a smartphone, next to an inspector panel. Step through
// them with ← / →, edit the caption and watch the phone update as you type, then approve and
// publish, save the edit, or discard the post.

import { AnimatePresence, motion } from "framer-motion";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  CircleAlert,
  Info,
  Keyboard,
  Link2,
  LoaderCircle,
  Moon,
  RefreshCw,
  RotateCcw,
  Save,
  Send,
  Smartphone,
  Sun,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { PLATFORM_LABELS, adminApi, errorText, timeAgo } from "@/lib/admin/client";
import type { AssetsResponse, GeneratedAsset, Job, Publication, PubStatus } from "@/lib/admin/types";

import { useJobs } from "../jobs";
import { Badge, StatusBadge, useToast } from "../ui";
import { MobileDeviceSimulator, type SimulatorBusy } from "./MobileDeviceSimulator";
import { fromAsset, fromPublication, money, patchBody, type PreviewItem } from "./model";
import type { Appearance } from "./parts";

type QueueResponse = { items: Publication[]; counts: Record<PubStatus, number> };
type SourceFilter = "all" | "asset" | "publication";

const PLATFORM_FILTERS = ["instagram", "tiktok", "youtube", "telegram"] as const;
/** Caption limits per platform (UTF-16 units, as the publishers count them). */
const LIMITS: Record<string, number> = { instagram: 2200, tiktok: 2200, youtube: 5000, telegram: 4096 };
const PUBLISH_VERB: Record<string, string> = {
  instagram: "Publishing to Instagram…",
  tiktok: "Publishing to TikTok…",
  youtube: "Uploading to YouTube Shorts…",
  telegram: "Posting to Telegram…",
};

function utf16(text: string): number {
  return text.length;
}

/** The ✓ / ✗ line a publish job prints last (post id or the platform's error), without its "#id" prefix. */
function receiptOf(job: Job, id: number): string | null {
  const lines = job.log.filter((line) => /^\s*[✓✗]/.test(line));
  const last = lines.length ? lines[lines.length - 1]! : job.error;
  return last
    ? last
        .replace(/^\s*[✓✗]\s*/, "")
        .replace(new RegExp(`^#${id}\\s+(not published:|failed:)?\\s*`), "")
        .replace(/^Failed:\s*/, "")
    : null;
}

function postIdOf(receipt: string | null): string | null {
  return receipt?.match(/post id\s+([^\s)]+?)[).]*$/i)?.[1] ?? null;
}

export function PhonePreviewInspector({
  initialKey,
  onClose,
  onChanged,
}: {
  initialKey?: string | null;
  onClose: () => void;
  /** Something was saved, published or deleted: the page behind should reload. */
  onChanged: () => void;
}) {
  const toast = useToast();
  const { start } = useJobs();
  const [assets, setAssets] = useState<GeneratedAsset[] | null>(null);
  const [publications, setPublications] = useState<Publication[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [source, setSource] = useState<SourceFilter>("all");
  const [platform, setPlatform] = useState<string>("");
  const [withFailed, setWithFailed] = useState(true);
  const [currentKey, setCurrentKey] = useState<string | null>(initialKey ?? null);
  const [direction, setDirection] = useState<1 | -1>(1);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<(SimulatorBusy & { key: string }) | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [appearance, setAppearance] = useState<Appearance>("light");
  const [expanded, setExpanded] = useState(true);
  const editor = useRef<HTMLTextAreaElement>(null);

  // -- data ---------------------------------------------------------------------------- //
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [a, q] = await Promise.all([
        adminApi<AssetsResponse>("assets?status=DRAFT,QUEUED,FAILED&limit=300"),
        adminApi<QueueResponse>("queue?status=QUEUED,FAILED,PENDING&limit=300"),
      ]);
      setAssets(a.items);
      setPublications(q.items);
      setLoadError(null);
    } catch (error) {
      setLoadError(errorText(error));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const all = useMemo(() => {
    const items = [...(assets ?? []).map(fromAsset), ...(publications ?? []).map(fromPublication)];
    return items.sort((a, b) => b.createdAt.localeCompare(a.createdAt) || b.id - a.id);
  }, [assets, publications]);

  const items = useMemo(
    () =>
      all.filter(
        (item) =>
          (source === "all" || item.source === source) &&
          (!platform || item.platform === platform) &&
          (withFailed || item.status !== "FAILED" || item.key === currentKey),
      ),
    [all, source, platform, withFailed, currentKey],
  );

  const index = Math.max(0, items.findIndex((item) => item.key === currentKey));
  const item: PreviewItem | undefined = items[index];
  useEffect(() => {
    if (item && item.key !== currentKey) setCurrentKey(item.key);
  }, [item, currentKey]);
  useEffect(() => setConfirmDelete(false), [item?.key]);

  const draft = item ? (drafts[item.key] ?? item.text) : "";
  const dirty = Boolean(item && drafts[item.key] !== undefined && drafts[item.key] !== item.text);
  const displayText = item ? `${draft}${item.suffix}` : "";
  const isBusy = busy !== null;

  // -- navigation ---------------------------------------------------------------------- //
  const go = useCallback(
    (step: 1 | -1) => {
      if (!items.length) return;
      const next = (index + step + items.length) % items.length;
      setDirection(step);
      setCurrentKey(items[next]!.key);
    },
    [index, items],
  );

  /** Take an item out of the list (published or deleted) and show the one that slides into its place. */
  const dropAndAdvance = useCallback(
    (gone: PreviewItem) => {
      const position = items.findIndex((entry) => entry.key === gone.key);
      const rest = items.filter((entry) => entry.key !== gone.key);
      setDirection(1);
      setCurrentKey(rest.length ? rest[Math.min(position, rest.length - 1)]!.key : null);
      if (gone.source === "asset") setAssets((list) => (list ?? []).filter((row) => row.id !== gone.id));
      else setPublications((list) => (list ?? []).filter((row) => row.id !== gone.id));
      setDrafts(({ [gone.key]: _unused, ...rest_ }) => rest_);
    },
    [items],
  );

  // -- actions ------------------------------------------------------------------------- //
  const save = useCallback(
    async (target: PreviewItem, quiet = false): Promise<boolean> => {
      const text = drafts[target.key];
      if (text === undefined || text === target.text) return true;
      if (!text.trim()) {
        toast("error", "The caption cannot be empty.");
        return false;
      }
      const result = await adminApi<GeneratedAsset | { id: number; caption: string }>(target.routes.patch, {
        method: "PATCH",
        body: patchBody(target, text),
      });
      if (target.source === "asset") {
        const updated = result as GeneratedAsset;
        setAssets((list) => (list ?? []).map((row) => (row.id === updated.id ? updated : row)));
      } else {
        const caption = (result as { caption: string }).caption;
        setPublications((list) =>
          (list ?? []).map((row) =>
            row.id === target.id
              ? { ...row, caption, preview: row.preview ? { ...row.preview, final_text: row.preview.final_text === null ? null : caption } : row.preview }
              : row,
          ),
        );
      }
      setDrafts(({ [target.key]: _unused, ...rest }) => rest);
      if (!quiet) toast("ok", `Saved #${target.id}`);
      onChanged();
      return true;
    },
    [drafts, onChanged, toast],
  );

  const runSave = useCallback(async () => {
    if (!item || !dirty || isBusy) return;
    setBusy({ key: item.key, kind: "saving", label: "Saving…" });
    try {
      await save(item);
    } catch (error) {
      toast("error", errorText(error));
    } finally {
      setBusy(null);
    }
  }, [dirty, isBusy, item, save, toast]);

  const publish = useCallback(async () => {
    if (!item || !item.canPublish || isBusy) return;
    const target = item;
    setBusy({ key: target.key, kind: "publishing", label: PUBLISH_VERB[target.platform] ?? "Publishing…" });
    try {
      if (!(await save(target, true))) return;
      if (target.routes.approve) await adminApi(target.routes.approve, { method: "POST", body: { status: "QUEUED" } });
      const job = await start(target.routes.publish, {}, `Publish ${target.typeLabel} #${target.id}`, { openConsole: false, quiet: true });
      if (!job) {
        await load(); // could not start (toast already shown); the item may now be QUEUED
        return;
      }
      const full = await adminApi<Job>(`jobs/${job.id}`).catch(() => job);
      const receipt = receiptOf(full, target.id);
      if (full.status === "SUCCEEDED") {
        const postId = postIdOf(receipt);
        toast("ok", `#${target.id} published to ${target.handle} (${PLATFORM_LABELS[target.platform] ?? target.platform})${postId ? ` · post id ${postId}` : ""}`);
        dropAndAdvance(target);
      } else {
        toast("error", `#${target.id} was not published: ${receipt ?? "see the job log"}`);
        await load();
      }
      onChanged();
    } catch (error) {
      toast("error", errorText(error));
      await load();
    } finally {
      setBusy(null);
    }
  }, [dropAndAdvance, isBusy, item, load, onChanged, save, start, toast]);

  const remove = useCallback(async () => {
    if (!item || isBusy) return;
    const target = item;
    setBusy({ key: target.key, kind: "deleting", label: "Discarding…" });
    try {
      const result = await adminApi<{ deleted: number; media_removed?: boolean }>(target.routes.remove, { method: "DELETE" });
      toast("ok", `#${target.id} discarded${result.media_removed ? " · its media file was removed" : ""}`);
      dropAndAdvance(target);
      onChanged();
    } catch (error) {
      toast("error", errorText(error));
    } finally {
      setBusy(null);
      setConfirmDelete(false);
    }
  }, [dropAndAdvance, isBusy, item, onChanged, toast]);

  // -- keyboard ------------------------------------------------------------------------ //
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const typing = event.target instanceof HTMLElement && /^(TEXTAREA|INPUT|SELECT)$/.test(event.target.tagName);
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        void runSave();
        return;
      }
      if (event.key === "Escape") {
        if (confirmDelete) setConfirmDelete(false);
        else if (typing) (event.target as HTMLElement).blur();
        else if (!isBusy) onClose();
        return;
      }
      if (typing || isBusy) return;
      if (event.key === "ArrowRight") {
        event.preventDefault();
        go(1);
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        go(-1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirmDelete, go, isBusy, onClose, runSave]);

  // Keep the page behind from scrolling while the inspector is open.
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => void (document.body.style.overflow = previous);
  }, []);

  const unsavedCount = Object.entries(drafts).filter(([key, text]) => all.find((entry) => entry.key === key)?.text !== text).length;
  const limit = item ? LIMITS[item.platform] : undefined;
  const length = utf16(displayText);

  // -- render -------------------------------------------------------------------------- //
  return (
    <div className="fixed inset-0 z-[55] flex flex-col bg-adm-bg" role="dialog" aria-modal="true" aria-label="Phone preview">
      {/* Top bar */}
      <header className="flex flex-wrap items-center gap-2 border-b border-adm-line bg-adm-panel px-3 py-2">
        <Smartphone className="h-4 w-4 text-adm-accent" />
        <h2 className="text-sm font-semibold">Phone preview</h2>
        <span className="text-xs text-adm-faint">{items.length ? `${index + 1} / ${items.length} unpublished` : "nothing to review"}</span>
        {unsavedCount > 0 && <Badge tone="warn">{unsavedCount} unsaved</Badge>}

        <div className="ms-2 flex rounded-lg border border-adm-line bg-adm-bg p-0.5">
          {(
            [
              ["all", "All"],
              ["asset", "FindHub assets"],
              ["publication", "Pipeline queue"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              onClick={() => setSource(value)}
              className={`rounded-md px-2.5 py-0.5 text-xs ${source === value ? "bg-adm-panel font-semibold shadow-sm" : "text-adm-soft hover:text-adm-ink"}`}
            >
              {label}
            </button>
          ))}
        </div>
        <select className="adm-input w-auto py-1 text-xs" value={platform} onChange={(e) => setPlatform(e.target.value)} aria-label="Platform">
          <option value="">All platforms</option>
          {PLATFORM_FILTERS.map((p) => (
            <option key={p} value={p}>
              {PLATFORM_LABELS[p]}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-1.5 text-xs text-adm-soft">
          <input type="checkbox" checked={withFailed} onChange={(e) => setWithFailed(e.target.checked)} className="accent-[rgb(var(--adm-accent))]" />
          Include failed
        </label>

        <div className="ms-auto flex items-center gap-1.5">
          <span className="hidden items-center gap-1 text-[11px] text-adm-faint md:flex">
            <Keyboard className="h-3.5 w-3.5" /> ← → browse · Ctrl+S save · Esc close
          </span>
          <button
            type="button"
            className="adm-btn px-2 py-1"
            onClick={() => setAppearance(appearance === "light" ? "dark" : "light")}
            title={`App theme: ${appearance}`}
            aria-label="Toggle the app theme inside the phone"
          >
            {appearance === "light" ? <Moon className="h-4 w-4" /> : <Sun className="h-4 w-4" />}
          </button>
          <button type="button" className="adm-btn px-2 py-1" onClick={() => void load()} disabled={loading || isBusy} aria-label="Reload">
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          </button>
          <button type="button" className="adm-btn px-2 py-1" onClick={onClose} disabled={isBusy} aria-label="Close the phone preview">
            <X className="h-4 w-4" />
          </button>
        </div>
      </header>

      {/* Filmstrip */}
      {items.length > 1 && (
        <nav className="no-scrollbar flex gap-1.5 overflow-x-auto border-b border-adm-line bg-adm-panel px-3 py-1.5" aria-label="Unpublished posts">
          {items.map((entry, position) => (
            <button
              key={entry.key}
              type="button"
              disabled={isBusy}
              onClick={() => {
                setDirection(position >= index ? 1 : -1);
                setCurrentKey(entry.key);
              }}
              className={`relative h-12 w-9 shrink-0 overflow-hidden rounded-md border-2 bg-adm-raised ${
                entry.key === item?.key ? "border-adm-accent" : "border-transparent opacity-70 hover:opacity-100"
              }`}
              title={`#${entry.id} · ${entry.typeLabel} · ${entry.handle} · ${entry.status}`}
            >
              {entry.imageUrl ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={entry.imageUrl} alt="" loading="lazy" className="h-full w-full object-cover" />
              ) : (
                <span className="flex h-full items-center justify-center text-[9px] text-adm-faint">{entry.platform.slice(0, 2).toUpperCase()}</span>
              )}
              {drafts[entry.key] !== undefined && drafts[entry.key] !== entry.text && (
                <span className="absolute right-0.5 top-0.5 h-2 w-2 rounded-full bg-adm-warn" />
              )}
              {entry.status === "FAILED" && <span className="absolute inset-x-0 bottom-0 h-1 bg-adm-bad" />}
            </button>
          ))}
        </nav>
      )}

      {/* Body */}
      {loadError && !item ? (
        <div className="m-6 flex items-center gap-2 rounded-lg border border-adm-bad/30 bg-adm-bad/10 p-3 text-sm text-adm-bad">
          <CircleAlert className="h-4 w-4" /> {loadError}
        </div>
      ) : !item ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 text-sm text-adm-soft">
          {assets === null ? (
            <>
              <LoaderCircle className="h-5 w-5 animate-spin" /> Loading unpublished posts…
            </>
          ) : (
            <>
              <Check className="h-6 w-6 text-adm-ok" />
              Nothing left to review{source !== "all" || platform ? " for these filters" : ""}.
            </>
          )}
        </div>
      ) : (
        <div className="grid min-h-0 flex-1 grid-rows-[minmax(420px,1fr)_auto] overflow-y-auto lg:grid-cols-[minmax(0,1fr)_440px] lg:grid-rows-1 lg:overflow-hidden">
          {/* Phone + arrows */}
          <section className="relative flex min-h-0 items-center gap-2 px-2 py-4 sm:px-6">
            <button
              type="button"
              className="adm-btn h-11 w-11 shrink-0 rounded-full p-0 shadow-sm"
              onClick={() => go(-1)}
              disabled={items.length < 2 || isBusy}
              aria-label="Previous post"
              title="Previous (←)"
            >
              <ArrowLeft className="h-5 w-5" />
            </button>
            <MobileDeviceSimulator
              item={item}
              text={displayText}
              appearance={appearance}
              expanded={expanded}
              onToggleCaption={() => setExpanded(!expanded)}
              busy={busy && busy.key === item.key && busy.kind !== "saving" ? busy : null}
              direction={direction}
              className="min-h-0 min-w-0 flex-1 self-stretch"
            />
            <button
              type="button"
              className="adm-btn h-11 w-11 shrink-0 rounded-full p-0 shadow-sm"
              onClick={() => go(1)}
              disabled={items.length < 2 || isBusy}
              aria-label="Next post"
              title="Next (→)"
            >
              <ArrowRight className="h-5 w-5" />
            </button>
          </section>

          {/* Inspector panel */}
          <aside className="flex min-h-0 flex-col border-t border-adm-line bg-adm-panel lg:border-l lg:border-t-0" data-key={item.key}>
            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge tone="accent">{item.typeLabel}</Badge>
                <StatusBadge status={item.status} />
                <Badge tone={item.hasAccount ? "neutral" : "warn"}>{item.hasAccount ? item.handle : "no account linked"}</Badge>
                <Badge>{item.categoryId}</Badge>
                <Badge>
                  {item.language.toUpperCase()} · {item.dir.toUpperCase()}
                </Badge>
                <span className="ms-auto text-[11px] text-adm-faint">
                  {item.source === "asset" ? "asset" : "queue"} #{item.id}
                </span>
              </div>

              <div>
                <p className="line-clamp-2 text-sm font-medium" dir="auto">
                  {item.productTitle}
                </p>
                <p className="text-xs text-adm-soft">
                  {money(item.price, item.currency)}
                  {item.originalPrice && item.price !== null && item.originalPrice > item.price && (
                    <span className="ms-1 text-adm-faint line-through">{money(item.originalPrice, item.currency)}</span>
                  )}
                  {` · ${item.model} · created ${timeAgo(item.createdAt)}`}
                </p>
              </div>

              {item.errorMessage && (
                <p className="flex gap-1.5 rounded-lg bg-adm-bad/10 p-2 text-xs text-adm-bad">
                  <CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {item.errorMessage}
                </p>
              )}

              {/* Caption editor */}
              <div>
                <div className="mb-1 flex items-center gap-2">
                  <label htmlFor="sim-caption" className="adm-label mb-0">
                    {item.textField === "reel_caption"
                      ? "Reel caption"
                      : item.layout === "instagram_story"
                        ? "Story overlay text"
                        : item.textField === "caption"
                          ? "Caption"
                          : "Post text"}
                  </label>
                  {dirty && <Badge tone="warn">unsaved</Badge>}
                  <span className={`ms-auto text-[11px] ${limit && length > limit ? "font-semibold text-adm-bad" : "text-adm-faint"}`}>
                    {length.toLocaleString()}
                    {limit ? ` / ${limit.toLocaleString()}` : ""}
                  </span>
                  {dirty && (
                    <button
                      type="button"
                      className="rounded p-0.5 text-adm-faint hover:bg-adm-raised hover:text-adm-ink"
                      title="Discard this edit"
                      aria-label="Discard this edit"
                      onClick={() => setDrafts(({ [item.key]: _unused, ...rest }) => rest)}
                    >
                      <RotateCcw className="h-3.5 w-3.5" />
                    </button>
                  )}
                </div>
                <textarea
                  id="sim-caption"
                  ref={editor}
                  dir={item.dir}
                  value={draft}
                  readOnly={!item.canEdit || isBusy}
                  onChange={(event) => setDrafts((all_) => ({ ...all_, [item.key]: event.target.value }))}
                  rows={item.layout === "instagram_story" ? 3 : 10}
                  className="adm-input resize-y font-sans text-[13px] leading-relaxed"
                  style={{ unicodeBidi: "plaintext" }}
                />
                {item.suffix && (
                  <p className="mt-1 text-[11px] text-adm-faint">
                    The publisher appends the link / call to action, hashtags and disclosure shown in the phone.
                  </p>
                )}
              </div>

              <Notes item={item} />

              {/* Links */}
              {(item.links.affiliateUrl || item.links.productUrl || item.links.stickers.length > 0) && (
                <div className="space-y-1 rounded-lg border border-adm-line bg-adm-bg p-2">
                  {item.links.stickers.map((sticker) => (
                    <LinkLine key={`s:${sticker.kind}`} label={`Sticker · ${sticker.label}`} url={sticker.url} />
                  ))}
                  {item.links.bioLink && item.layout !== "instagram_story" && <LinkLine label="Bio link" url={item.links.bioLink} />}
                  {item.links.productUrl && item.layout !== "instagram_story" && <LinkLine label="FindHub" url={item.links.productUrl} />}
                  {item.links.affiliateUrl && item.layout !== "instagram_story" && <LinkLine label="Affiliate" url={item.links.affiliateUrl} />}
                </div>
              )}
            </div>

            {/* Action bar */}
            <div className="border-t border-adm-line p-3">
              {!item.canPublish && item.publishBlockedReason && (
                <p className="mb-2 flex gap-1.5 text-[11px] text-adm-warn">
                  <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  {item.publishBlockedReason}
                </p>
              )}
              <AnimatePresence mode="wait" initial={false}>
                {confirmDelete ? (
                  <motion.div
                    key="confirm"
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -6 }}
                    className="flex flex-wrap items-center gap-2 rounded-lg border border-adm-bad/40 bg-adm-bad/10 p-2"
                  >
                    <span className="flex-1 text-xs text-adm-bad">
                      Discard {item.source === "asset" ? "asset" : "publication"} #{item.id}
                      {item.source === "asset" ? " and its image (unless another post uses it)" : ""}?
                    </span>
                    <button type="button" className="adm-btn px-2.5 py-1 text-xs" onClick={() => setConfirmDelete(false)} disabled={isBusy}>
                      Keep
                    </button>
                    <button
                      type="button"
                      className="inline-flex items-center gap-1.5 rounded-lg bg-adm-bad px-2.5 py-1 text-xs font-semibold text-white hover:bg-adm-bad/90 disabled:opacity-50"
                      onClick={() => void remove()}
                      disabled={isBusy}
                      autoFocus
                    >
                      {busy?.kind === "deleting" ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />} Yes, discard
                    </button>
                  </motion.div>
                ) : (
                  <motion.div key="actions" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
                      onClick={() => void publish()}
                      disabled={!item.canPublish || isBusy || (limit !== undefined && length > limit)}
                      title={item.publishBlockedReason ?? (dirty ? "Saves your edit, then publishes" : "Publish through the platform API now")}
                    >
                      {busy?.kind === "publishing" ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                      {item.status === "DRAFT" ? "Approve & Publish Now" : "Publish Now"}
                    </button>
                    <button type="button" className="adm-btn" onClick={() => void runSave()} disabled={!dirty || isBusy || !item.canEdit} title="Save the edit (Ctrl+S)">
                      {busy?.kind === "saving" ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />} Save edits
                    </button>
                    <button
                      type="button"
                      className="adm-btn-danger"
                      onClick={() => setConfirmDelete(true)}
                      disabled={isBusy || item.publishing}
                      aria-label={`Discard #${item.id}`}
                      title="Delete / discard"
                    >
                      <Trash2 className="h-4 w-4" /> Discard
                    </button>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}

function LinkLine({ label, url }: { label: string; url: string }) {
  return (
    <a href={url} target="_blank" rel="noreferrer noopener" className="flex min-w-0 items-center gap-1.5 text-xs hover:underline" title={url}>
      <Link2 className="h-3 w-3 shrink-0 text-adm-faint" />
      <span className="shrink-0 text-adm-faint">{label}</span>
      <span className="min-w-0 truncate text-adm-accent">{url}</span>
    </a>
  );
}

/** What the phone shows that the platform API will (or will not) reproduce exactly. */
function Notes({ item }: { item: PreviewItem }) {
  const notes: string[] = [];
  if (item.layout === "instagram_story") {
    notes.push("Stories carry no caption: this text is drawn into the 9:16 image, so editing it here only updates the stored text.");
    if (item.links.stickers.length)
      notes.push("Meta's API cannot attach link stickers: when the Story is published by the API they are left out; add them in the app if you post it by hand.");
  }
  if (item.layout === "telegram")
    notes.push("Inline buttons are a preview of the post's links: the Telegram publisher currently sends them inside the message text.");
  if (item.layout === "vertical_video" && !item.videoUrl) notes.push("No video rendered yet: the phone shows the 9:16 cover image.");
  if (!notes.length) return null;
  return (
    <ul className="space-y-1 rounded-lg border border-adm-line bg-adm-bg p-2 text-[11px] text-adm-soft">
      {notes.map((note) => (
        <li key={note} className="flex gap-1.5">
          <Info className="mt-0.5 h-3 w-3 shrink-0 text-adm-faint" />
          {note}
        </li>
      ))}
    </ul>
  );
}
