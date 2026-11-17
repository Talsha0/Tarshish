"use client";

import { ChevronDown, ChevronRight, Eraser, LoaderCircle, Radio, RefreshCw, RotateCcw, Send, Trash2 } from "lucide-react";
import { Fragment, useEffect, useState } from "react";

import { CONTENT_TYPE_LABELS, PLATFORM_LABELS, adminApi, timeAgo } from "@/lib/admin/client";
import type { Category, Job, Publication, PubStatus } from "@/lib/admin/types";

import { useJobs } from "./jobs";
import { ErrorNote, Loading, PageHeader, Panel, StatusBadge, useAction, useResource } from "./ui";

type QueueResponse = { items: Publication[]; counts: Record<PubStatus, number> };
const STATUSES: { value: string; label: string }[] = [
  { value: "", label: "All" },
  { value: "QUEUED,PENDING", label: "Queued" },
  { value: "PUBLISHED", label: "Published" },
  { value: "FAILED", label: "Failed" },
];

function Row({ item, onChanged }: { item: Publication; onChanged: () => void }) {
  const { start } = useJobs();
  const { busy, run } = useAction();
  const [open, setOpen] = useState(false);
  const canPublish = item.status === "QUEUED" || item.status === "FAILED";
  return (
    <Fragment>
      <tr className="border-t border-adm-line align-top">
        <td className="py-2 pl-1 pr-2">
          <button type="button" className="rounded p-0.5 text-adm-soft hover:bg-adm-raised" onClick={() => setOpen(!open)} aria-label="Show caption">
            {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          </button>
        </td>
        <td className="py-2 pr-3">
          <StatusBadge status={item.status} />
          <p className="mt-1 text-[11px] text-adm-faint">#{item.id}</p>
        </td>
        <td className="max-w-[22rem] py-2 pr-3">
          <p className="truncate font-medium">{item.product_title}</p>
          <p className="line-clamp-2 text-xs text-adm-soft">{item.caption}</p>
          {item.error_message && <p className="mt-1 line-clamp-2 text-xs text-adm-bad">{item.error_message}</p>}
        </td>
        <td className="py-2 pr-3 text-xs">
          <p>{CONTENT_TYPE_LABELS[item.content_format ?? ""] ?? item.content_format ?? item.content_type}</p>
          <p className="text-adm-faint">{item.model_used}</p>
        </td>
        <td className="py-2 pr-3 text-xs">
          <p className="font-medium">{item.target_page}</p>
          <p className="text-adm-faint">
            {PLATFORM_LABELS[item.account_platform ?? ""] ?? item.platform} · {item.category ?? "—"}/{item.language ?? "—"}
          </p>
        </td>
        <td className="whitespace-nowrap py-2 pr-3 text-xs text-adm-soft">
          {item.status === "PUBLISHED" ? `published ${timeAgo(item.published_at)}` : item.scheduled_for ? `slot ${timeAgo(item.scheduled_for)}` : timeAgo(item.created_at)}
          {item.attempts > 0 && <p className="text-adm-faint">{item.attempts} attempt(s)</p>}
        </td>
        <td className="py-2">
          <div className="flex flex-wrap justify-end gap-1">
            {item.status === "FAILED" && (
              <button
                type="button"
                className="adm-btn px-2 py-1 text-xs"
                disabled={busy !== null}
                onClick={() => void run("retry", () => adminApi(`queue/${item.id}/retry`, { method: "POST" }), `#${item.id} re-queued`).then((ok) => ok && onChanged())}
              >
                <RotateCcw className="h-3.5 w-3.5" /> Retry
              </button>
            )}
            {canPublish && (
              <button
                type="button"
                className="adm-btn px-2 py-1 text-xs"
                onClick={() => void start(`queue/${item.id}/publish-now`, {}, `Publish #${item.id}`).then(() => onChanged())}
              >
                <Send className="h-3.5 w-3.5" /> Publish now
              </button>
            )}
            {item.status !== "PENDING" && (
              <button
                type="button"
                className="adm-btn-danger px-2 py-1 text-xs"
                disabled={busy !== null}
                onClick={() => {
                  if (window.confirm(`Delete publication #${item.id}? AI spend already recorded is kept.`))
                    void run("delete", () => adminApi(`queue/${item.id}`, { method: "DELETE" }), `#${item.id} deleted`).then((ok) => ok && onChanged());
                }}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        </td>
      </tr>
      {open && (
        <tr>
          <td />
          <td colSpan={6} className="pb-3 pr-3">
            <pre className="whitespace-pre-wrap rounded-lg bg-adm-bg p-3 text-xs leading-relaxed">{item.caption}</pre>
            <p className="mt-1 text-[11px] text-adm-faint">
              pipeline {item.pipeline_id} · cost ${item.generation_cost_usd.toFixed(4)}
              {item.platform_post_id ? ` · post id ${item.platform_post_id}` : ""}
              {item.media_uri ? ` · media ${item.media_uri}` : ""}
            </p>
          </td>
        </tr>
      )}
    </Fragment>
  );
}

function JobHistory() {
  const { follow, finishedCount } = useJobs();
  const jobs = useResource<{ jobs: Job[] }>("jobs?limit=50", 15_000);
  useEffect(() => {
    if (finishedCount) void jobs.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finishedCount]);
  return (
    <Panel title="Job history" subtitle="Every harvest, publish, check and seed started from this portal, with its full console output.">
      <div id="jobs" />
      {jobs.error && <ErrorNote error={jobs.error} onRetry={() => void jobs.reload()} />}
      {!jobs.data && !jobs.error && <Loading />}
      {jobs.data && jobs.data.jobs.length === 0 && <p className="text-sm text-adm-soft">No jobs yet.</p>}
      {jobs.data && jobs.data.jobs.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wide text-adm-faint">
                <th className="pb-2 pr-3 font-semibold">Status</th>
                <th className="pb-2 pr-3 font-semibold">Job</th>
                <th className="pb-2 pr-3 font-semibold">Command</th>
                <th className="pb-2 pr-3 font-semibold">Started</th>
                <th className="pb-2 pr-3 font-semibold">Duration</th>
                <th className="pb-2" />
              </tr>
            </thead>
            <tbody>
              {jobs.data.jobs.map((job) => {
                const seconds =
                  job.started_at && job.finished_at ? Math.max(0, (new Date(job.finished_at).getTime() - new Date(job.started_at).getTime()) / 1000) : null;
                return (
                  <tr key={job.id} className="border-t border-adm-line">
                    <td className="py-2 pr-3">
                      <StatusBadge status={job.status} />
                    </td>
                    <td className="py-2 pr-3 font-medium">{job.kind}</td>
                    <td className="adm-mono max-w-[28rem] truncate py-2 pr-3 text-xs text-adm-soft">{job.command}</td>
                    <td className="whitespace-nowrap py-2 pr-3 text-xs text-adm-soft">{timeAgo(job.created_at)}</td>
                    <td className="py-2 pr-3 text-xs text-adm-soft">{seconds === null ? "—" : `${seconds.toFixed(1)} s`}</td>
                    <td className="py-2 text-right">
                      <button type="button" className="adm-btn px-2 py-1 text-xs" onClick={() => follow(job)}>
                        View log
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

export function QueueView() {
  const { start, finishedCount } = useJobs();
  const { busy, run } = useAction();
  const [status, setStatus] = useState("");
  const [category, setCategory] = useState("");
  const [language, setLanguage] = useState("");
  const [platform, setPlatform] = useState("");
  const categories = useResource<{ categories: Category[] }>("categories");
  const params = new URLSearchParams({ limit: "200" });
  if (status) params.set("status", status);
  if (category) params.set("category", category);
  if (language) params.set("language", language);
  if (platform) params.set("platform", platform);
  const queue = useResource<QueueResponse>(`queue?${params.toString()}`, 20_000);
  useEffect(() => {
    if (finishedCount) void queue.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finishedCount]);

  const counts = queue.data?.counts;
  const scope = category && language ? { category_id: category, language } : {};
  const scopeLabel = category && language ? `${category}/${language}` : "every pipeline";

  return (
    <>
      <PageHeader
        title="Publication queue & job history"
        subtitle="Posts, Stories and videos waiting for the worker, published, or failed — with their generated captions."
        actions={
          <>
            <button type="button" className="adm-btn" onClick={() => void queue.reload()}>
              <RefreshCw className="h-4 w-4" /> Refresh
            </button>
            <button type="button" className="adm-btn" onClick={() => void start("actions/worker", {}, "Queue worker pass").then(() => queue.reload())}>
              <Radio className="h-4 w-4" /> Run worker pass
            </button>
            <button
              type="button"
              className="adm-btn"
              disabled={busy !== null || !counts?.FAILED}
              onClick={() =>
                void run("retry-all", () => adminApi<{ requeued: number }>("queue/retry-failed", { method: "POST", body: scope }), (r) => `${r.requeued} failed item(s) re-queued`).then(
                  () => queue.reload(),
                )
              }
            >
              <RotateCcw className="h-4 w-4" /> Retry failed ({scopeLabel})
            </button>
            <button
              type="button"
              className="adm-btn-danger"
              disabled={busy !== null || !(counts && counts.QUEUED + counts.PENDING)}
              onClick={() => {
                if (window.confirm(`Delete every queued item of ${scopeLabel}? AI spend already recorded is kept.`))
                  void run("flush", () => adminApi<{ flushed: number }>("queue/flush", { method: "POST", body: scope }), (r) => `${r.flushed} item(s) flushed`).then(() =>
                    queue.reload(),
                  );
              }}
            >
              {busy === "flush" ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Eraser className="h-4 w-4" />} Flush queue
            </button>
          </>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="flex rounded-lg border border-adm-line bg-adm-panel p-0.5">
          {STATUSES.map((option) => {
            const count =
              option.value === "" || !counts
                ? undefined
                : option.value.split(",").reduce((sum, key) => sum + (counts[key as PubStatus] ?? 0), 0);
            return (
              <button
                key={option.label}
                type="button"
                onClick={() => setStatus(option.value)}
                className={`rounded-md px-3 py-1 text-xs ${status === option.value ? "bg-adm-raised font-semibold" : "text-adm-soft hover:text-adm-ink"}`}
              >
                {option.label}
                {count !== undefined && <span className="ml-1 text-adm-faint">{count}</span>}
              </button>
            );
          })}
        </div>
        <select className="adm-input w-auto" value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Category">
          <option value="">All categories</option>
          {(categories.data?.categories ?? []).map((c) => (
            <option key={c.category_id} value={c.category_id}>
              {c.display_name_en}
            </option>
          ))}
        </select>
        <select className="adm-input w-auto" value={language} onChange={(e) => setLanguage(e.target.value)} aria-label="Language">
          <option value="">All languages</option>
          <option value="he">HE</option>
          <option value="en">EN</option>
        </select>
        <select className="adm-input w-auto" value={platform} onChange={(e) => setPlatform(e.target.value)} aria-label="Platform">
          <option value="">All platforms</option>
          {["instagram", "tiktok", "youtube", "telegram"].map((p) => (
            <option key={p} value={p}>
              {PLATFORM_LABELS[p]}
            </option>
          ))}
        </select>
      </div>

      <Panel className="mb-5">
        {queue.error && <ErrorNote error={queue.error} onRetry={() => void queue.reload()} />}
        {!queue.data && !queue.error && <Loading />}
        {queue.data && queue.data.items.length === 0 && <p className="py-6 text-center text-sm text-adm-soft">Nothing here for these filters.</p>}
        {queue.data && queue.data.items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-wide text-adm-faint">
                  <th className="w-6" />
                  <th className="pb-2 pr-3 font-semibold">Status</th>
                  <th className="pb-2 pr-3 font-semibold">Product & caption</th>
                  <th className="pb-2 pr-3 font-semibold">Type</th>
                  <th className="pb-2 pr-3 font-semibold">Target</th>
                  <th className="pb-2 pr-3 font-semibold">When</th>
                  <th className="pb-2 text-right font-semibold">Actions</th>
                </tr>
              </thead>
              <tbody>
                {queue.data.items.map((item) => (
                  <Row key={item.id} item={item} onChanged={() => void queue.reload()} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <JobHistory />
    </>
  );
}
