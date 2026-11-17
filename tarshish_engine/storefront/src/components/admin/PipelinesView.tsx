"use client";

import { ArrowRight, Database, Download, FileUp, LoaderCircle, Play, Plus, RefreshCcw, Save, Sparkles, Wrench } from "lucide-react";
import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";

import { CONTENT_TYPE_LABELS, LANGUAGE_LABELS, PLATFORM_LABELS, adminApi } from "@/lib/admin/client";
import type { Category, ContentProfile, ContentProfiles, Overview, SocialAccount } from "@/lib/admin/types";

import { useJobs } from "./jobs";
import { Badge, EnvChip, ErrorNote, Field, Loading, Modal, PageHeader, Panel, StatusBadge, Toggle, useAction, useResource } from "./ui";

type Rules = Record<string, unknown> | null;
const STORES = [
  { key: "temu_search", label: "Temu", source: "temu_rapidapi", mock: "temu_mock" },
  { key: "aliexpress_search", label: "AliExpress", source: "aliexpress_rapidapi", mock: "aliexpress_mock" },
] as const;

function num(value: unknown): string {
  return value === undefined || value === null ? "" : String(value);
}

/** Keyword + price filters of one store, keeping any other keys (l2Category …) untouched. */
function RulesEditor({ label, rules, onChange }: { label: string; rules: Rules; onChange: (rules: Rules) => void }) {
  const current = rules ?? {};
  const update = (key: string, value: string, numeric = false) => {
    const next: Record<string, unknown> = { ...current };
    if (value.trim() === "") delete next[key];
    else next[key] = numeric ? Number(value) : value;
    onChange(Object.keys(next).length ? next : null);
  };
  const extra = Object.entries(current).filter(([key]) => !["keyword", "priceMin", "priceMax"].includes(key));
  return (
    <div className="rounded-lg border border-adm-line p-2.5">
      <p className="mb-1.5 text-xs font-semibold">{label}</p>
      <input
        className="adm-input mb-1.5"
        placeholder="search keywords (empty = not harvested)"
        value={num(current.keyword)}
        onChange={(e) => update("keyword", e.target.value)}
      />
      <div className="grid grid-cols-2 gap-1.5">
        <input className="adm-input" inputMode="decimal" placeholder="min price" value={num(current.priceMin)} onChange={(e) => update("priceMin", e.target.value, true)} />
        <input className="adm-input" inputMode="decimal" placeholder="max price" value={num(current.priceMax)} onChange={(e) => update("priceMax", e.target.value, true)} />
      </div>
      {extra.length > 0 && (
        <p className="adm-mono mt-1 truncate text-[10px] text-adm-faint">{extra.map(([k, v]) => `${k}: ${String(v)}`).join(" · ")}</p>
      )}
    </div>
  );
}

function CategoryCard({ category, onSaved }: { category: Category; onSaved: () => void }) {
  const { start } = useJobs();
  const { busy, run } = useAction();
  const [draft, setDraft] = useState(category);
  const [source, setSource] = useState("all");
  const [limit, setLimit] = useState("");
  const [dryRun, setDryRun] = useState(false);
  useEffect(() => setDraft(category), [category]);
  const dirty = JSON.stringify(draft) !== JSON.stringify(category);

  const save = () =>
    run(
      "save",
      () =>
        adminApi(`categories/${encodeURIComponent(category.category_id)}`, {
          method: "PUT",
          body: {
            display_name_en: draft.display_name_en,
            display_name_he: draft.display_name_he,
            temu_search: draft.temu_search,
            aliexpress_search: draft.aliexpress_search,
            min_rating: Number(draft.min_rating),
            min_orders: Number(draft.min_orders),
            enabled: draft.enabled,
          },
        }),
      `Category ${category.category_id} saved`,
    ).then((ok) => ok && onSaved());

  const harvest = () =>
    start(
      "actions/harvest",
      { source, category_id: category.category_id, limit: limit ? Number(limit) : undefined, dry_run: dryRun },
      `Harvest ${category.category_id}`,
    );

  const total = Object.values(category.products).reduce((a, b) => a + b, 0);
  return (
    <div className="adm-card p-3">
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="font-semibold">{category.display_name_en}</p>
          <p className="text-[11px] text-adm-faint">
            <code className="adm-mono">{category.category_id}</code> · {total} product(s) · {category.accounts} page(s)
          </p>
        </div>
        <Toggle checked={draft.enabled} onChange={(enabled) => setDraft({ ...draft, enabled })} label="Enabled" />
      </div>
      <div className="mb-2 grid grid-cols-2 gap-1.5">
        <input className="adm-input" value={draft.display_name_en} onChange={(e) => setDraft({ ...draft, display_name_en: e.target.value })} aria-label="English name" />
        <input className="adm-input" dir="rtl" value={draft.display_name_he} onChange={(e) => setDraft({ ...draft, display_name_he: e.target.value })} aria-label="Hebrew name" />
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        {STORES.map((store) => (
          <RulesEditor
            key={store.key}
            label={`${store.label}${category.products[store.source] ? ` · ${category.products[store.source]} stored` : ""}`}
            rules={draft[store.key]}
            onChange={(rules) => setDraft({ ...draft, [store.key]: rules })}
          />
        ))}
      </div>
      <div className="mt-2 grid grid-cols-2 gap-1.5">
        <Field label="Min rating">
          <input className="adm-input" inputMode="decimal" value={draft.min_rating} onChange={(e) => setDraft({ ...draft, min_rating: e.target.value as unknown as number })} />
        </Field>
        <Field label="Min orders">
          <input className="adm-input" inputMode="numeric" value={draft.min_orders} onChange={(e) => setDraft({ ...draft, min_orders: e.target.value as unknown as number })} />
        </Field>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-adm-line pt-3">
        <button type="button" className="adm-btn-primary px-2 py-1 text-xs" disabled={!dirty || busy !== null} onClick={() => void save()}>
          {busy === "save" ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />} Save
        </button>
        <span className="mx-1 h-4 w-px bg-adm-line" />
        <select className="adm-input w-auto py-1 text-xs" value={source} onChange={(e) => setSource(e.target.value)} aria-label="Source">
          <option value="all">All live sources</option>
          {STORES.map((store) => (
            <option key={store.source} value={store.source}>
              {store.label} RapidAPI
            </option>
          ))}
          {STORES.map((store) => (
            <option key={store.mock} value={store.mock}>
              {store.label} mock (offline)
            </option>
          ))}
        </select>
        <input className="adm-input w-16 py-1 text-xs" inputMode="numeric" placeholder="limit" value={limit} onChange={(e) => setLimit(e.target.value.replace(/\D/g, ""))} />
        <label className="flex items-center gap-1 text-xs text-adm-soft">
          <Toggle checked={dryRun} onChange={setDryRun} label="Dry run" /> dry run
        </label>
        <button type="button" className="adm-btn px-2 py-1 text-xs" onClick={() => void harvest()}>
          <Download className="h-3.5 w-3.5" /> Harvest
        </button>
      </div>
    </div>
  );
}

function NewCategory({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const { busy, run } = useAction();
  const [form, setForm] = useState({ en: "", he: "", temu: "", ali: "", min: "", max: "" });
  const submit = () =>
    run(
      "create",
      () =>
        adminApi<Category>("categories", {
          method: "POST",
          body: {
            display_name_en: form.en,
            display_name_he: form.he,
            temu_keyword: form.temu || undefined,
            aliexpress_keyword: form.ali || undefined,
            price_min: form.min ? Number(form.min) : undefined,
            price_max: form.max ? Number(form.max) : undefined,
          },
        }),
      (category) => `Category ${category.category_id} created`,
    ).then((ok) => ok && onSaved());
  return (
    <Modal title="New category" onClose={onClose}>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="English name">
          <input className="adm-input" value={form.en} onChange={(e) => setForm({ ...form, en: e.target.value })} />
        </Field>
        <Field label="Hebrew name">
          <input className="adm-input" dir="rtl" value={form.he} onChange={(e) => setForm({ ...form, he: e.target.value })} />
        </Field>
        <Field label="Temu keywords">
          <input className="adm-input" value={form.temu} onChange={(e) => setForm({ ...form, temu: e.target.value })} />
        </Field>
        <Field label="AliExpress keywords">
          <input className="adm-input" value={form.ali} onChange={(e) => setForm({ ...form, ali: e.target.value })} />
        </Field>
        <Field label="Min price">
          <input className="adm-input" inputMode="decimal" value={form.min} onChange={(e) => setForm({ ...form, min: e.target.value })} />
        </Field>
        <Field label="Max price">
          <input className="adm-input" inputMode="decimal" value={form.max} onChange={(e) => setForm({ ...form, max: e.target.value })} />
        </Field>
      </div>
      <div className="mt-4 flex justify-end gap-2">
        <button type="button" className="adm-btn" onClick={onClose}>
          Cancel
        </button>
        <button type="button" className="adm-btn-primary" disabled={!form.en.trim() || busy !== null} onClick={() => void submit()}>
          Create category
        </button>
      </div>
    </Modal>
  );
}

function ProfileRow({ profile, models, onSaved }: { profile: ContentProfile; models: ContentProfiles["models"]; onSaved: () => void }) {
  const { busy, run } = useAction();
  const [model, setModel] = useState(profile.llm_model);
  const [tone, setTone] = useState(profile.tone ?? "");
  const [enabled, setEnabled] = useState(profile.enabled);
  const dirty = model !== profile.llm_model || tone !== (profile.tone ?? "") || enabled !== profile.enabled;
  const groups = ["anthropic", "gemini", "offline", "other"];
  return (
    <tr className="border-t border-adm-line align-top">
      <td className="py-2 pr-3">
        <p className="font-medium">{CONTENT_TYPE_LABELS[profile.content_type] ?? profile.content_type}</p>
        <p className="adm-mono text-[11px] text-adm-faint">
          {profile.content_type} · {profile.generator}
          {profile.publish_format ? ` · ${profile.publish_format}` : ""}
        </p>
      </td>
      <td className="py-2 pr-3 text-xs text-adm-soft">
        {PLATFORM_LABELS[profile.platform] ?? profile.platform}
        <br />
        <span className="text-adm-faint">{profile.accounts} page(s)</span>
      </td>
      <td className="py-2 pr-3">
        <select className="adm-input min-w-[11rem]" value={model} onChange={(e) => setModel(e.target.value)} aria-label="AI model">
          {groups.map((group) => {
            const items = models.filter((m) => m.provider === group);
            return items.length ? (
              <optgroup key={group} label={group === "anthropic" ? "Claude" : group === "gemini" ? "Gemini" : group === "offline" ? "Offline" : "Other"}>
                {items.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </optgroup>
            ) : null;
          })}
        </select>
      </td>
      <td className="py-2 pr-3">
        <input className="adm-input min-w-[12rem]" value={tone} placeholder="tone of voice" onChange={(e) => setTone(e.target.value)} />
      </td>
      <td className="py-2 pr-3">
        <Toggle checked={enabled} onChange={setEnabled} label="Enabled" />
      </td>
      <td className="py-2">
        <button
          type="button"
          className="adm-btn-primary px-2 py-1 text-xs"
          disabled={!dirty || busy !== null}
          onClick={() =>
            void run(
              "save",
              () =>
                adminApi(`content-profiles/${encodeURIComponent(profile.content_type)}`, {
                  method: "PUT",
                  body: { llm_model: model, tone: tone || null, enabled },
                }),
              `${profile.content_type} saved`,
            ).then((ok) => ok && onSaved())
          }
        >
          <Save className="h-3.5 w-3.5" /> Save
        </button>
      </td>
    </tr>
  );
}

function RunPipeline({ categories, profiles }: { categories: Category[]; profiles: ContentProfile[] }) {
  const { start } = useJobs();
  const [category, setCategory] = useState("all");
  const [language, setLanguage] = useState("all");
  const [types, setTypes] = useState<string[]>([]);
  const [mode, setMode] = useState("");
  const [limit, setLimit] = useState("");
  const [dryRun, setDryRun] = useState(true);
  const [force, setForce] = useState(false);
  const toggle = (type: string) => setTypes((list) => (list.includes(type) ? list.filter((t) => t !== type) : [...list, type]));
  return (
    <Panel title="Run a publishing pipeline" subtitle="python main.py --publish … · every option of the CLI">
      <div className="grid gap-3 md:grid-cols-4">
        <Field label="Category">
          <select className="adm-input" value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="all">All enabled categories</option>
            {categories.map((c) => (
              <option key={c.category_id} value={c.category_id}>
                {c.display_name_en}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Language">
          <select className="adm-input" value={language} onChange={(e) => setLanguage(e.target.value)}>
            <option value="all">Every language with a page</option>
            {["he", "en"].map((l) => (
              <option key={l} value={l}>
                {LANGUAGE_LABELS[l]}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Mode">
          <select className="adm-input" value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="">Default (PUBLISHING_MODE)</option>
            <option value="queued">Queued — the worker paces posts</option>
            <option value="immediate">Immediate — publish now</option>
          </select>
        </Field>
        <Field label="Products per content type">
          <input className="adm-input" inputMode="numeric" placeholder="default" value={limit} onChange={(e) => setLimit(e.target.value.replace(/\D/g, ""))} />
        </Field>
      </div>
      <div className="mt-3">
        <span className="adm-label">Content types (none selected = every enabled type with a page)</span>
        <div className="flex flex-wrap gap-1.5">
          {profiles.map((p) => (
            <button
              key={p.content_type}
              type="button"
              onClick={() => toggle(p.content_type)}
              className={`adm-btn px-2 py-1 text-xs ${types.includes(p.content_type) ? "border-adm-accent bg-adm-accent/10 text-adm-accent" : ""}`}
            >
              {CONTENT_TYPE_LABELS[p.content_type] ?? p.content_type}
            </button>
          ))}
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-2 text-sm">
          <Toggle checked={dryRun} onChange={setDryRun} label="Dry run" /> Dry run (no posts, no paid AI, no writes)
        </label>
        <label className="flex items-center gap-2 text-sm">
          <Toggle checked={force} onChange={setForce} label="Force" /> Force (re-process handled products)
        </label>
        <button
          type="button"
          className="adm-btn-primary ml-auto"
          onClick={() =>
            void start(
              "actions/publish",
              {
                category_id: category,
                language,
                content_types: types,
                mode: mode || undefined,
                limit: limit ? Number(limit) : undefined,
                dry_run: dryRun,
                force,
              },
              dryRun ? "Publishing dry run" : "Publishing run",
            )
          }
        >
          <Play className="h-4 w-4" /> Run pipeline
        </button>
      </div>
    </Panel>
  );
}

function EngineTools({ categories }: { categories: Category[] }) {
  const { start } = useJobs();
  const [seed, setSeed] = useState({ store: "temu", category: categories[0]?.category_id ?? "", keyword: "", limit: "10", job: "" });
  const [importPath, setImportPath] = useState("tarshish_engine.db");
  return (
    <Panel title="Engine tools" subtitle="Seeding, configuration sync, reports and maintenance — the remaining CLI flags.">
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="space-y-2">
          <p className="text-xs font-semibold">Keyword seed (--seed-temu / --seed-aliexpress)</p>
          <div className="grid grid-cols-2 gap-1.5">
            <select className="adm-input" value={seed.store} onChange={(e) => setSeed({ ...seed, store: e.target.value })}>
              <option value="temu">Temu</option>
              <option value="aliexpress">AliExpress</option>
            </select>
            <select className="adm-input" value={seed.category} onChange={(e) => setSeed({ ...seed, category: e.target.value })}>
              {categories.map((c) => (
                <option key={c.category_id} value={c.category_id}>
                  {c.category_id}
                </option>
              ))}
            </select>
            <input className="adm-input" placeholder="keyword (default: category rules)" value={seed.keyword} onChange={(e) => setSeed({ ...seed, keyword: e.target.value })} />
            <input className="adm-input" inputMode="numeric" placeholder="limit" value={seed.limit} onChange={(e) => setSeed({ ...seed, limit: e.target.value.replace(/\D/g, "") })} />
            {seed.store === "temu" && (
              <input className="adm-input col-span-2" placeholder="Temu job id to collect (optional)" value={seed.job} onChange={(e) => setSeed({ ...seed, job: e.target.value })} />
            )}
          </div>
          <button
            type="button"
            className="adm-btn"
            disabled={!seed.category}
            onClick={() =>
              void start(
                "actions/seed",
                {
                  store: seed.store,
                  category_id: seed.category,
                  keyword: seed.keyword || undefined,
                  limit: Number(seed.limit || 10),
                  job: seed.store === "temu" && seed.job ? seed.job : undefined,
                },
                "Keyword seed",
              )
            }
          >
            <Sparkles className="h-4 w-4" /> Run seed
          </button>
        </div>
        <div className="space-y-2">
          <p className="text-xs font-semibold">Maintenance</p>
          <div className="flex flex-wrap gap-1.5">
            <button type="button" className="adm-btn" onClick={() => void start("actions/seed-storefront", {}, "Seed storefront demo")}>
              <Database className="h-4 w-4" /> Load demo catalogue
            </button>
            <button
              type="button"
              className="adm-btn"
              onClick={() => {
                if (window.confirm("Push config/*.yaml into the database? This overwrites edits made here for the rows in those files."))
                  void start("actions/sync-config", {}, "Sync config");
              }}
            >
              <RefreshCcw className="h-4 w-4" /> Sync config from YAML
            </button>
            <button type="button" className="adm-btn" onClick={() => void start("actions/report", { report: "stats" }, "Stats")}>
              <Wrench className="h-4 w-4" /> Stats
            </button>
            <button type="button" className="adm-btn" onClick={() => void start("actions/report", { report: "list-config" }, "List config")}>
              <Wrench className="h-4 w-4" /> List config
            </button>
          </div>
          <p className="pt-2 text-xs font-semibold">Import a local SQLite file into DATABASE_URL (--import-sqlite)</p>
          <div className="flex gap-1.5">
            <input className="adm-input adm-mono" value={importPath} onChange={(e) => setImportPath(e.target.value)} aria-label="SQLite file path on the backend" />
            <button
              type="button"
              className="adm-btn shrink-0"
              onClick={() => {
                if (window.confirm(`Copy ${importPath} (a path on the backend server) into the PostgreSQL database?`))
                  void start("actions/import-sqlite", { path: importPath }, "Import SQLite");
              }}
            >
              <FileUp className="h-4 w-4" /> Import
            </button>
          </div>
        </div>
      </div>
    </Panel>
  );
}

function FlowColumn({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="min-w-0 flex-1 rounded-xl border border-adm-line bg-adm-panel p-3">
      <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-adm-faint">{title}</p>
      <div className="space-y-1.5">{children}</div>
    </div>
  );
}

function Arrow() {
  return (
    <div className="flex items-center justify-center text-adm-faint lg:px-1">
      <ArrowRight className="h-5 w-5 rotate-90 lg:rotate-0" />
    </div>
  );
}

export function PipelinesView() {
  const { finishedCount } = useJobs();
  const categories = useResource<{ categories: Category[] }>("categories");
  const profiles = useResource<ContentProfiles>("content-profiles");
  const accounts = useResource<{ accounts: SocialAccount[] }>("social-accounts");
  const overview = useResource<Overview>("overview");
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    if (finishedCount) {
      void categories.reload();
      void overview.reload();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finishedCount]);

  const cats = categories.data?.categories ?? [];
  const profs = profiles.data?.profiles ?? [];
  const pages = accounts.data?.accounts ?? [];
  const error = categories.error ?? profiles.error ?? accounts.error;

  return (
    <>
      <PageHeader
        title="Pipelines, categories & content models"
        subtitle="Stage 1 harvests each category once per store; Stage 2 writes each content type with its own model and publishes it to the page of that category and language."
        actions={
          <button type="button" className="adm-btn-primary" onClick={() => setCreating(true)}>
            <Plus className="h-4 w-4" /> New category
          </button>
        }
      />
      {error && <ErrorNote error={error} onRetry={() => void categories.reload()} />}
      {!categories.data && !error && <Loading />}

      {categories.data && (
        <div className="space-y-5">
          <div className="flex flex-col gap-2 lg:flex-row lg:items-stretch">
            <FlowColumn title="Sources">
              {(["temu", "aliexpress"] as const).map((store) => {
                const chain = overview.data?.sources[store];
                return (
                  <div key={store} className="flex items-center justify-between gap-2 rounded-lg bg-adm-bg/60 px-2 py-1.5 text-sm">
                    <span>{store === "temu" ? "Temu" : "AliExpress"} RapidAPI</span>
                    {chain ? <Badge tone={chain.ready ? "ok" : "bad"}>{chain.ready ? `${chain.providers.length} providers` : "no key"}</Badge> : <Badge>…</Badge>}
                  </div>
                );
              })}
            </FlowColumn>
            <Arrow />
            <FlowColumn title="Category catalog">
              {cats.map((c) => (
                <div key={c.category_id} className="flex items-center justify-between gap-2 rounded-lg bg-adm-bg/60 px-2 py-1.5 text-sm">
                  <span className="truncate">{c.display_name_en}</span>
                  <Badge tone={c.enabled ? "ok" : "warn"}>{Object.values(c.products).reduce((a, b) => a + b, 0)} items</Badge>
                </div>
              ))}
            </FlowColumn>
            <Arrow />
            <FlowColumn title="Content types & AI models">
              {profs
                .filter((p) => p.platform !== "console")
                .map((p) => (
                  <div key={p.content_type} className="rounded-lg bg-adm-bg/60 px-2 py-1.5 text-sm">
                    <span className="block truncate">{CONTENT_TYPE_LABELS[p.content_type] ?? p.content_type}</span>
                    <span className="block truncate text-[11px] text-adm-faint">{p.model_label}</span>
                  </div>
                ))}
            </FlowColumn>
            <Arrow />
            <FlowColumn title="Target social pages">
              {pages.map((a) => (
                <div key={a.id} className="flex items-center justify-between gap-2 rounded-lg bg-adm-bg/60 px-2 py-1.5 text-sm">
                  <span className="min-w-0 truncate">
                    {a.page_handle} <span className="text-[11px] text-adm-faint">{a.language.toUpperCase()}</span>
                  </span>
                  <StatusBadge status={a.status} />
                </div>
              ))}
              <Link href="/admin/accounts" className="block pt-1 text-xs text-adm-accent hover:underline">
                Manage pages →
              </Link>
            </FlowColumn>
          </div>

          <RunPipeline categories={cats} profiles={profs} />

          <div>
            <h2 className="mb-2 text-sm font-semibold">Categories & search rules</h2>
            <div className="grid gap-3 xl:grid-cols-2">
              {cats.map((category) => (
                <CategoryCard key={category.category_id} category={category} onSaved={() => void categories.reload()} />
              ))}
            </div>
          </div>

          <Panel
            title="Content types & AI model routing"
            subtitle="One model per content type, shared by every category and language."
            actions={
              profiles.data && (
                <span className="flex gap-1">
                  <EnvChip name="ANTHROPIC_API_KEY" status={profiles.data.provider_keys.anthropic} required={false} />
                  <EnvChip name="GEMINI_API_KEY" status={profiles.data.provider_keys.gemini} required={false} />
                </span>
              )
            }
          >
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-[11px] uppercase tracking-wide text-adm-faint">
                    <th className="pb-2 pr-3 font-semibold">Content type</th>
                    <th className="pb-2 pr-3 font-semibold">Platform</th>
                    <th className="pb-2 pr-3 font-semibold">AI model</th>
                    <th className="pb-2 pr-3 font-semibold">Tone</th>
                    <th className="pb-2 pr-3 font-semibold">On</th>
                    <th className="pb-2" />
                  </tr>
                </thead>
                <tbody>
                  {profs.map((profile) => (
                    <ProfileRow key={profile.content_type} profile={profile} models={profiles.data?.models ?? []} onSaved={() => void profiles.reload()} />
                  ))}
                </tbody>
              </table>
            </div>
            <p className="mt-2 text-[11px] text-adm-faint">
              Without the provider&apos;s API key a model falls back to the free offline templates (“Mock”), so a run never fails for a missing key.
            </p>
          </Panel>

          <EngineTools categories={cats} />
        </div>
      )}

      {creating && (
        <NewCategory
          onClose={() => setCreating(false)}
          onSaved={() => {
            setCreating(false);
            void categories.reload();
          }}
        />
      )}
    </>
  );
}
