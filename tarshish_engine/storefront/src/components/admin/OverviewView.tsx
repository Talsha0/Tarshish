"use client";

import {
  CircleAlert,
  Download,
  MousePointerClick,
  Package,
  PlugZap,
  Radio,
  RefreshCw,
  Send,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";

import { PLATFORM_LABELS, adminApi, timeAgo } from "@/lib/admin/client";
import type { Overview, SourceChain } from "@/lib/admin/types";

import { useJobs } from "./jobs";
import { Badge, EnvChip, ErrorNote, Loading, PageHeader, Panel, StatusBadge, Toggle, statusTone } from "./ui";

function Kpi({ label, value, hint, icon }: { label: string; value: ReactNode; hint?: ReactNode; icon: ReactNode }) {
  return (
    <div className="adm-card flex items-start gap-3 p-4">
      <div className="rounded-lg bg-adm-accent/10 p-2 text-adm-accent">{icon}</div>
      <div className="min-w-0">
        <p className="text-xs text-adm-soft">{label}</p>
        <p className="mt-0.5 text-2xl font-semibold tabular-nums tracking-tight">{value}</p>
        {hint && <p className="mt-0.5 text-[11px] text-adm-faint">{hint}</p>}
      </div>
    </div>
  );
}

function BudgetCard({ budget }: { budget: Overview["budget"] }) {
  const pct = Math.min(100, Math.round(budget.ratio * 100));
  const bar = budget.state === "ok" ? "bg-adm-ok" : budget.state === "warning" ? "bg-adm-warn" : "bg-adm-bad";
  return (
    <div className="adm-card p-4">
      <div className="flex items-center justify-between">
        <p className="text-xs text-adm-soft">AI budget · this month</p>
        <Badge tone={statusTone(budget.state === "ok" ? "ok" : budget.state === "warning" ? "warning" : "bad")}>{budget.state}</Badge>
      </div>
      <p className="mt-1 text-2xl font-semibold tabular-nums tracking-tight">
        ${budget.spent_usd.toFixed(2)} <span className="text-sm font-normal text-adm-faint">of ${budget.limit_usd.toFixed(2)}</span>
      </p>
      <div className="mt-2 h-2 overflow-hidden rounded-full bg-adm-raised" role="meter" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label="AI budget used">
        <div className={`h-full rounded-full ${bar}`} style={{ width: `${pct}%` }} />
      </div>
      <p className="mt-1 text-[11px] text-adm-faint">{pct}% used · MAX_MONTHLY_AI_BUDGET_USD</p>
    </div>
  );
}

function ChainCard({ title, chain }: { title: string; chain: SourceChain }) {
  return (
    <Panel
      title={title}
      subtitle={chain.from_file ? `Order from config/${chain.config_path}` : "Built-in provider order"}
      actions={<Badge tone={chain.ready ? "ok" : "bad"}>{chain.ready ? "Ready" : "No key"}</Badge>}
    >
      {chain.error && <ErrorNote error={chain.error} />}
      <ol className="space-y-1.5">
        {chain.providers.map((provider) => (
          <li key={provider.name} className="flex items-start gap-3 rounded-lg border border-adm-line bg-adm-bg/60 px-3 py-2">
            <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-adm-raised text-[11px] font-semibold">
              {provider.order}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium">{provider.name}</span>
                {!provider.enabled && <Badge tone="warn">disabled</Badge>}
              </div>
              <p className="adm-mono truncate text-[11px] text-adm-faint">{provider.host}</p>
              {provider.last_outcome && (
                <p className="mt-0.5 text-[11px] text-adm-soft">
                  Last run: <span className={provider.last_outcome === "ok" ? "text-adm-ok" : "text-adm-warn"}>{provider.last_outcome}</span>
                  {provider.last_detail ? ` · ${provider.last_detail}` : ""}
                </p>
              )}
            </div>
            <div className="flex shrink-0 flex-col items-end gap-1">
              <EnvChip name={provider.api_key_env} status={provider.key_status === "Ready" ? "Configured" : "Missing"} />
              {provider.key_status === "Ready" && provider.key_detail.includes("via") && (
                <span className="text-[10px] text-adm-faint">{provider.key_detail.replace("Configured ", "")}</span>
              )}
            </div>
          </li>
        ))}
      </ol>
      {chain.last_run && (
        <p className="mt-2 text-[11px] text-adm-faint">
          Last harvest {timeAgo(chain.last_run.finished_at)} · {chain.last_run.category ?? "—"} · “{chain.last_run.keyword ?? ""}” ·{" "}
          {chain.last_run.succeeded ? `${chain.last_run.usable ?? 0} usable from ${chain.last_run.succeeded}` : "no provider delivered"}
        </p>
      )}
    </Panel>
  );
}

export function OverviewView() {
  const { start, follow, finishedCount } = useJobs();
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dryRun, setDryRun] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);

  const load = async () => {
    try {
      setData(await adminApi<Overview>("overview"));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 30_000);
    return () => window.clearInterval(timer);
  }, [finishedCount]);

  const quick = async (key: string, path: string, body: unknown, label: string) => {
    setBusy(key);
    await start(path, body, label);
    setBusy(null);
  };

  return (
    <>
      <PageHeader
        title="Command center"
        subtitle="Everything the CLI does, one click away. Jobs run on the backend; their output streams into the console at the bottom."
        actions={
          <button type="button" className="adm-btn" onClick={() => void load()}>
            <RefreshCw className="h-4 w-4" /> Refresh
          </button>
        }
      />

      <div className="adm-card mb-5 flex flex-wrap items-center gap-2 p-3">
        <span className="mr-1 text-xs font-semibold uppercase tracking-wide text-adm-faint">Quick actions</span>
        <button
          type="button"
          className="adm-btn-primary"
          disabled={busy !== null}
          onClick={() => void quick("harvest", "actions/harvest", { source: "all", category_id: "all" }, "Harvest all sources")}
        >
          <Download className="h-4 w-4" /> Harvest all sources
        </button>
        <button
          type="button"
          className="adm-btn-primary"
          disabled={busy !== null}
          onClick={() => void quick("publish", "actions/publish", { category_id: "all", language: "all", dry_run: dryRun }, "Publishing pipelines")}
        >
          <Send className="h-4 w-4" /> Run publishing pipelines
        </button>
        <label className="flex items-center gap-2 rounded-lg border border-adm-line px-2 py-1 text-xs text-adm-soft">
          <Toggle checked={dryRun} onChange={setDryRun} label="Dry run" /> Dry run
        </label>
        <button
          type="button"
          className="adm-btn"
          disabled={busy !== null}
          onClick={() => void quick("verify", "actions/verify", {}, "Verify all connections")}
        >
          <ShieldCheck className="h-4 w-4" /> Verify all API connections
        </button>
        <button
          type="button"
          className="adm-btn"
          disabled={busy !== null}
          onClick={() => void quick("worker", "actions/worker", {}, "Queue worker pass")}
        >
          <Radio className="h-4 w-4" /> Run worker pass
        </button>
      </div>

      {error && (
        <div className="mb-4">
          <ErrorNote error={error} onRetry={() => void load()} />
        </div>
      )}
      {!data && !error && <Loading />}

      {data && (
        <div className="space-y-5">
          {!data.runtime.background_jobs && (
            <div className="flex items-start gap-2 rounded-lg border border-adm-warn/40 bg-adm-warn/10 px-3 py-2 text-sm">
              <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-adm-warn" />
              <span>
                This backend runs as a serverless function, which cannot keep background jobs alive. Configuration works, but
                harvests and publishing need <code className="adm-mono">BACKEND_API_URL</code> to point at the long-running
                FastAPI server.
              </span>
            </div>
          )}

          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Kpi
              label="Storefront clicks"
              value={data.kpis.storefront_clicks.toLocaleString()}
              hint={`${data.kpis.storefront_clicks_7d.toLocaleString()} in the last 7 days`}
              icon={<MousePointerClick className="h-4 w-4" />}
            />
            <Kpi
              label="Published posts"
              value={data.kpis.published_posts.toLocaleString()}
              hint={`${data.queue.QUEUED} queued · ${data.queue.FAILED} failed`}
              icon={<Send className="h-4 w-4" />}
            />
            <Kpi
              label="Active products"
              value={data.kpis.active_products.toLocaleString()}
              hint={`${data.kpis.harvested_products.toLocaleString()} harvested · ${data.categories.active}/${data.categories.total} categories active`}
              icon={<Package className="h-4 w-4" />}
            />
            <BudgetCard budget={data.budget} />
          </div>

          <div className="grid gap-5 lg:grid-cols-2">
            <ChainCard title="Temu RapidAPI failover chain" chain={data.sources.temu} />
            <ChainCard title="AliExpress RapidAPI failover chain" chain={data.sources.aliexpress} />
          </div>

          <div className="grid gap-5 lg:grid-cols-3">
            <Panel
              title="Publication queue"
              actions={
                <Link href="/admin/queue" className="text-xs text-adm-accent hover:underline">
                  Open queue →
                </Link>
              }
            >
              <dl className="grid grid-cols-2 gap-2">
                {(["QUEUED", "PENDING", "PUBLISHED", "FAILED"] as const).map((status) => (
                  <div key={status} className="rounded-lg border border-adm-line bg-adm-bg/60 p-3">
                    <dt className="text-[11px] text-adm-soft">{status}</dt>
                    <dd className="text-lg font-semibold tabular-nums">{data.queue[status]}</dd>
                  </div>
                ))}
              </dl>
            </Panel>

            <Panel
              title="Social accounts"
              subtitle={`${data.social_accounts.ready} of ${data.social_accounts.total} ready to publish`}
              actions={
                <Link href="/admin/accounts" className="text-xs text-adm-accent hover:underline">
                  Manage →
                </Link>
              }
            >
              <ul className="space-y-2">
                {data.platform_credentials.map((row) => (
                  <li key={row.platform}>
                    <div className="mb-1 flex items-center justify-between text-xs">
                      <span className="font-medium">{PLATFORM_LABELS[row.platform] ?? row.platform}</span>
                      <span className="text-adm-faint">{data.social_accounts.by_platform[row.platform] ?? 0} page(s)</span>
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {row.env.map((env) => (
                        <EnvChip key={env.env_var} name={env.env_var} status={env.status} required={env.required} />
                      ))}
                    </div>
                  </li>
                ))}
              </ul>
              <p className="mt-2 text-[11px] text-adm-faint">Publisher defaults; pages can name their own variables.</p>
            </Panel>

            <Panel title="Environment" subtitle={data.runtime.database}>
              <ul className="space-y-2.5">
                {data.environment.map((group) => (
                  <li key={group.group}>
                    <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-adm-faint">{group.group}</p>
                    <div className="space-y-1">
                      {group.vars.map((item) => (
                        <div key={item.name} className="flex items-center justify-between gap-2 text-xs">
                          <span className="text-adm-soft">{item.purpose}</span>
                          <EnvChip name={item.name} status={item.status} required={false} />
                        </div>
                      ))}
                    </div>
                  </li>
                ))}
              </ul>
              {data.runtime.plugin_load_errors.length > 0 && (
                <p className="mt-3 flex items-start gap-1 text-[11px] text-adm-warn">
                  <CircleAlert className="mt-0.5 h-3 w-3 shrink-0" /> Plugins not loaded: {data.runtime.plugin_load_errors.join(", ")}
                </p>
              )}
            </Panel>
          </div>

          <Panel
            title="Recent jobs"
            actions={
              <Link href="/admin/queue#jobs" className="text-xs text-adm-accent hover:underline">
                Job history →
              </Link>
            }
          >
            {data.recent_jobs.length === 0 ? (
              <p className="text-sm text-adm-soft">
                <PlugZap className="mr-1 inline h-4 w-4" /> No jobs yet — try “Harvest all sources”.
              </p>
            ) : (
              <ul className="divide-y divide-adm-line">
                {data.recent_jobs.map((job) => (
                  <li key={job.id} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                    <StatusBadge status={job.status} />
                    <span className="font-medium">{job.kind}</span>
                    <span className="adm-mono min-w-0 flex-1 truncate text-xs text-adm-faint">{job.command}</span>
                    <span className="text-xs text-adm-faint">{timeAgo(job.created_at)}</span>
                    <button type="button" className="adm-btn px-2 py-1 text-xs" onClick={() => follow(job)}>
                      View log
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </div>
      )}
    </>
  );
}
