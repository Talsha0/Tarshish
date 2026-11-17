"use client";

import { ChevronLeft, ChevronRight, LoaderCircle, Pencil, Plus, PlugZap, Power, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { LANGUAGE_LABELS, PLATFORM_LABELS, adminApi, errorText, timeAgo } from "@/lib/admin/client";
import type { Category, PlatformFields, SocialAccount, VerifyResult } from "@/lib/admin/types";

import {
  Badge,
  EnvChip,
  ErrorNote,
  Field,
  Loading,
  Modal,
  PageHeader,
  Panel,
  StatusBadge,
  Toggle,
  useAction,
  useResource,
  useToast,
} from "./ui";

type AccountsResponse = { accounts: SocialAccount[]; platforms: Record<string, PlatformFields> };
type CategoriesResponse = { categories: Category[] };

const PLATFORM_ORDER = ["instagram", "tiktok", "youtube", "telegram"] as const;
const PLATFORM_DOT: Record<string, string> = {
  instagram: "bg-pink-500",
  tiktok: "bg-adm-ink",
  youtube: "bg-red-600",
  telegram: "bg-sky-500",
};
const ENV_NAME = /^[A-Z_][A-Z0-9_]{1,127}$/;

// --------------------------------------------------------------------------- //
// Live "is this variable set on the backend?" indicator for env-var NAME inputs
// --------------------------------------------------------------------------- //
function useEnvStatus(names: string[]) {
  const [status, setStatus] = useState<Record<string, string>>({});
  const key = names.filter((name) => ENV_NAME.test(name)).join(",");
  useEffect(() => {
    if (!key) return;
    const timer = window.setTimeout(() => {
      adminApi<{ vars: Record<string, string> }>(`env-check?names=${encodeURIComponent(key)}`)
        .then((body) => setStatus((old) => ({ ...old, ...body.vars })))
        .catch(() => undefined);
    }, 350);
    return () => window.clearTimeout(timer);
  }, [key]);
  return status;
}

function EnvNameInput({
  value,
  onChange,
  placeholder,
  status,
  required,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  status?: string;
  required?: boolean;
}) {
  const invalid = value !== "" && !ENV_NAME.test(value);
  return (
    <div>
      <div className="relative">
        <input
          className={`adm-input adm-mono pr-24 ${invalid ? "border-adm-bad" : ""}`}
          value={value}
          placeholder={placeholder}
          required={required}
          spellCheck={false}
          autoComplete="off"
          onChange={(event) => onChange(event.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, "_"))}
        />
        {value && !invalid && status && (
          <span className={`absolute right-2 top-1/2 -translate-y-1/2 text-[11px] font-semibold ${status === "Configured" ? "text-adm-ok" : "text-adm-bad"}`}>
            {status === "Configured" ? "✓ Configured" : "✗ Missing"}
          </span>
        )}
      </div>
      {invalid && <p className="mt-1 text-[11px] text-adm-bad">Enter the variable NAME (UPPER_SNAKE_CASE), never the secret itself.</p>}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Connect New Social Page wizard
// --------------------------------------------------------------------------- //
type WizardState = {
  platform: string;
  handle: string;
  metadata: Record<string, string>;
  token: string;
  accountIdEnv: string;
  language: string;
  categoryId: string;
  newCategory: boolean;
  catEn: string;
  catHe: string;
  temuKeyword: string;
  aliKeyword: string;
  priceMin: string;
  priceMax: string;
};

function ConnectWizard({
  platforms,
  categories,
  onClose,
  onCreated,
}: {
  platforms: Record<string, PlatformFields>;
  categories: Category[];
  onClose: () => void;
  onCreated: (account: SocialAccount) => void;
}) {
  const toast = useToast();
  const [step, setStep] = useState(1);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<WizardState>({
    platform: "",
    handle: "",
    metadata: {},
    token: "",
    accountIdEnv: "",
    language: "he",
    categoryId: categories[0]?.category_id ?? "",
    newCategory: categories.length === 0,
    catEn: "",
    catHe: "",
    temuKeyword: "",
    aliKeyword: "",
    priceMin: "",
    priceMax: "",
  });
  const set = <K extends keyof WizardState>(key: K, value: WizardState[K]) => setForm((old) => ({ ...old, [key]: value }));
  const fields = form.platform ? platforms[form.platform] : undefined;
  const envStatus = useEnvStatus([form.token, form.accountIdEnv]);

  const step2Ready =
    Boolean(fields) &&
    form.handle.trim() !== "" &&
    ENV_NAME.test(form.token) &&
    (form.accountIdEnv === "" || ENV_NAME.test(form.accountIdEnv)) &&
    (fields?.metadata ?? []).every((item) => !item.required || (form.metadata[item.key] ?? "").trim() !== "");
  const step3Ready = form.newCategory ? form.catEn.trim() !== "" : form.categoryId !== "";

  const submit = async () => {
    setSaving(true);
    setError(null);
    const number = (value: string) => (value.trim() === "" ? undefined : Number(value));
    try {
      const result = await adminApi<{ account: SocialAccount; created_category: string | null }>("social-accounts", {
        method: "POST",
        body: {
          page_handle: form.handle.trim(),
          platform: form.platform,
          language: form.language,
          category_id: form.newCategory ? undefined : form.categoryId,
          new_category: form.newCategory
            ? {
                display_name_en: form.catEn.trim(),
                display_name_he: form.catHe.trim(),
                temu_keyword: form.temuKeyword.trim() || undefined,
                aliexpress_keyword: form.aliKeyword.trim() || undefined,
                price_min: number(form.priceMin),
                price_max: number(form.priceMax),
              }
            : undefined,
          token_env_var: form.token,
          account_id_env_var: form.accountIdEnv || undefined,
          platform_metadata: form.metadata,
        },
      });
      toast(
        "ok",
        `${PLATFORM_LABELS[form.platform]} page ${result.account.page_handle} connected` +
          (result.created_category ? ` · category “${result.created_category}” created` : ""),
      );
      onCreated(result.account);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal title="Connect a new social page" onClose={onClose} wide>
      <ol className="mb-4 flex items-center gap-2 text-xs">
        {["Platform", "Page & credentials", "Category & language"].map((label, index) => (
          <li key={label} className="flex items-center gap-2">
            <span
              className={`flex h-5 w-5 items-center justify-center rounded-full text-[11px] font-semibold ${
                step > index ? "bg-adm-accent text-adm-accent-ink" : "bg-adm-raised text-adm-soft"
              }`}
            >
              {index + 1}
            </span>
            <span className={step === index + 1 ? "font-semibold" : "text-adm-soft"}>{label}</span>
            {index < 2 && <span className="text-adm-faint">›</span>}
          </li>
        ))}
      </ol>

      {step === 1 && (
        <div className="grid gap-3 sm:grid-cols-2">
          {PLATFORM_ORDER.map((platform) => (
            <button
              key={platform}
              type="button"
              onClick={() => {
                setForm((old) => ({ ...old, platform, metadata: {} }));
                setStep(2);
              }}
              className={`adm-card flex items-center gap-3 p-4 text-left transition hover:border-adm-accent ${
                form.platform === platform ? "border-adm-accent" : ""
              }`}
            >
              <span className={`h-3 w-3 rounded-full ${PLATFORM_DOT[platform]}`} />
              <span>
                <span className="block font-semibold">{platforms[platform]?.label ?? PLATFORM_LABELS[platform]}</span>
                <span className="block text-xs text-adm-soft">
                  {(platforms[platform]?.metadata ?? []).map((item) => item.label).join(" · ")}
                </span>
              </span>
            </button>
          ))}
        </div>
      )}

      {step === 2 && fields && (
        <div className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={fields.handle_label}>
              <input className="adm-input" value={form.handle} placeholder={fields.handle_placeholder} onChange={(e) => set("handle", e.target.value)} />
            </Field>
            {fields.metadata.map((item) => (
              <Field key={item.key} label={item.label} hint={`platform_metadata.${item.key} · an identifier, not a secret`}>
                <input
                  className="adm-input adm-mono"
                  value={form.metadata[item.key] ?? ""}
                  onChange={(e) => set("metadata", { ...form.metadata, [item.key]: e.target.value.trim() })}
                />
              </Field>
            ))}
            <Field label={fields.token_label} hint="The NAME of a variable in the backend environment.">
              <EnvNameInput value={form.token} onChange={(v) => set("token", v)} placeholder={fields.token_placeholder} status={envStatus[form.token]} required />
            </Field>
            {fields.account_id_label && (
              <Field label={fields.account_id_label}>
                <EnvNameInput
                  value={form.accountIdEnv}
                  onChange={(v) => set("accountIdEnv", v)}
                  placeholder={fields.account_id_placeholder}
                  status={envStatus[form.accountIdEnv]}
                />
              </Field>
            )}
          </div>
          <p className="rounded-lg bg-adm-raised px-3 py-2 text-xs text-adm-soft">
            🔒 Tokens never pass through this page. Add the secret to the backend&apos;s environment (server <code className="adm-mono">.env</code>,
            Vercel / VPS settings) under the name you enter here; the status shows whether the backend can see it.
          </p>
        </div>
      )}

      {step === 3 && (
        <div className="space-y-4">
          <div className="flex flex-wrap gap-4">
            <Field label="Language">
              <div className="flex gap-1">
                {["he", "en"].map((language) => (
                  <button
                    key={language}
                    type="button"
                    onClick={() => set("language", language)}
                    className={`adm-btn ${form.language === language ? "border-adm-accent bg-adm-accent/10 text-adm-accent" : ""}`}
                  >
                    {LANGUAGE_LABELS[language]}
                  </button>
                ))}
              </div>
            </Field>
          </div>
          <div className="flex items-center gap-2 text-sm">
            <Toggle checked={form.newCategory} onChange={(value) => set("newCategory", value)} label="Create a new category inline" />
            <span>+ Create new category inline</span>
          </div>
          {!form.newCategory ? (
            <Field label="Category">
              <select className="adm-input" value={form.categoryId} onChange={(e) => set("categoryId", e.target.value)}>
                {categories.map((category) => (
                  <option key={category.category_id} value={category.category_id}>
                    {category.display_name_en} {category.display_name_he ? `· ${category.display_name_he}` : ""} ({category.category_id})
                  </option>
                ))}
              </select>
            </Field>
          ) : (
            <div className="grid gap-3 rounded-lg border border-adm-line p-3 sm:grid-cols-2">
              <Field label="English name" hint="The category id (URL slug) is derived from it.">
                <input className="adm-input" value={form.catEn} placeholder="Smart Gadgets" onChange={(e) => set("catEn", e.target.value)} />
              </Field>
              <Field label="Hebrew name">
                <input className="adm-input" dir="rtl" value={form.catHe} placeholder="גאדג'טים חכמים" onChange={(e) => set("catHe", e.target.value)} />
              </Field>
              <Field label="Temu search keywords">
                <input className="adm-input" value={form.temuKeyword} placeholder="smart tech gadgets" onChange={(e) => set("temuKeyword", e.target.value)} />
              </Field>
              <Field label="AliExpress search keywords">
                <input className="adm-input" value={form.aliKeyword} placeholder="smart gadgets" onChange={(e) => set("aliKeyword", e.target.value)} />
              </Field>
              <Field label="Min price">
                <input className="adm-input" inputMode="decimal" value={form.priceMin} onChange={(e) => set("priceMin", e.target.value)} />
              </Field>
              <Field label="Max price">
                <input className="adm-input" inputMode="decimal" value={form.priceMax} onChange={(e) => set("priceMax", e.target.value)} />
              </Field>
            </div>
          )}
        </div>
      )}

      {error && (
        <div className="mt-3">
          <ErrorNote error={error} />
        </div>
      )}

      <div className="mt-5 flex items-center justify-between">
        <button type="button" className="adm-btn" disabled={step === 1} onClick={() => setStep(step - 1)}>
          <ChevronLeft className="h-4 w-4" /> Back
        </button>
        {step < 3 ? (
          <button type="button" className="adm-btn-primary" disabled={step === 1 ? !form.platform : !step2Ready} onClick={() => setStep(step + 1)}>
            Next <ChevronRight className="h-4 w-4" />
          </button>
        ) : (
          <button type="button" className="adm-btn-primary" disabled={!step3Ready || saving} onClick={() => void submit()}>
            {saving && <LoaderCircle className="h-4 w-4 animate-spin" />} Connect page
          </button>
        )}
      </div>
    </Modal>
  );
}

// --------------------------------------------------------------------------- //
// Edit an existing page
// --------------------------------------------------------------------------- //
function EditAccount({
  account,
  fields,
  onClose,
  onSaved,
}: {
  account: SocialAccount;
  fields: PlatformFields | undefined;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { busy, run } = useAction();
  const [handle, setHandle] = useState(account.page_handle);
  const [token, setToken] = useState(account.token_env_var ?? "");
  const [accountIdEnv, setAccountIdEnv] = useState(account.account_id_env_var ?? "");
  const [metadata, setMetadata] = useState<Record<string, string>>(account.platform_metadata);
  const envStatus = useEnvStatus([token, accountIdEnv]);
  const keys = Array.from(new Set([...(fields?.metadata.map((item) => item.key) ?? []), ...Object.keys(metadata)]));

  const save = async () => {
    const result = await run(
      "save",
      () =>
        adminApi(`social-accounts/${encodeURIComponent(account.id)}`, {
          method: "PUT",
          body: {
            page_handle: handle,
            token_env_var: token || null,
            account_id_env_var: accountIdEnv || null,
            platform_metadata: metadata,
          },
        }),
      "Page saved",
    );
    if (result) onSaved();
  };

  return (
    <Modal title={`Edit ${PLATFORM_LABELS[account.platform] ?? account.platform} · ${account.category_id}/${account.language}`} onClose={onClose}>
      <div className="space-y-3">
        <Field label={fields?.handle_label ?? "Page handle"}>
          <input className="adm-input" value={handle} onChange={(e) => setHandle(e.target.value)} />
        </Field>
        {keys.map((key) => (
          <Field key={key} label={fields?.metadata.find((item) => item.key === key)?.label ?? key}>
            <input className="adm-input adm-mono" value={metadata[key] ?? ""} onChange={(e) => setMetadata({ ...metadata, [key]: e.target.value.trim() })} />
          </Field>
        ))}
        <Field label={fields?.token_label ?? "Token env var"}>
          <EnvNameInput value={token} onChange={setToken} status={envStatus[token]} />
        </Field>
        <Field label={fields?.account_id_label ?? "Account id env var (optional)"}>
          <EnvNameInput value={accountIdEnv} onChange={setAccountIdEnv} status={envStatus[accountIdEnv]} />
        </Field>
      </div>
      <div className="mt-5 flex justify-end gap-2">
        <button type="button" className="adm-btn" onClick={onClose}>
          Cancel
        </button>
        <button type="button" className="adm-btn-primary" disabled={busy !== null || !handle.trim()} onClick={() => void save()}>
          {busy && <LoaderCircle className="h-4 w-4 animate-spin" />} Save
        </button>
      </div>
    </Modal>
  );
}

// --------------------------------------------------------------------------- //
// One page card
// --------------------------------------------------------------------------- //
function AccountCard({
  account,
  onEdit,
  onChanged,
}: {
  account: SocialAccount;
  onEdit: () => void;
  onChanged: () => void;
}) {
  const { busy, run } = useAction();
  const [check, setCheck] = useState<VerifyResult | null>(null);

  const test = async () => {
    const result = await run("test", () =>
      adminApi<VerifyResult>(`actions/verify-account/${encodeURIComponent(account.id)}`, { method: "POST" }),
    );
    if (result) setCheck(result);
  };

  return (
    <div className="rounded-lg border border-adm-line bg-adm-bg/60 p-3">
      <div className="flex items-start gap-2">
        <span className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${PLATFORM_DOT[account.platform] ?? "bg-adm-faint"}`} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-semibold">{account.page_handle}</span>
            <span className="text-xs text-adm-soft">{PLATFORM_LABELS[account.platform] ?? account.platform}</span>
          </div>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {account.env
              .filter((env) => env.required || env.explicit || env.inline_from)
              .map((env) =>
                env.inline_from ? (
                  <Badge key={env.credential} tone="ok" title={`From platform metadata (${env.inline_from})`}>
                    ✓ {env.credential} inline
                  </Badge>
                ) : (
                  <EnvChip key={env.credential} name={env.env_var} status={env.status} required={env.required} />
                ),
              )}
          </div>
          {Object.keys(account.platform_metadata).length > 0 && (
            <p className="adm-mono mt-1 truncate text-[11px] text-adm-faint">
              {Object.entries(account.platform_metadata)
                .map(([key, value]) => `${key}=${value}`)
                .join(" · ")}
            </p>
          )}
        </div>
        <StatusBadge status={account.status} title={account.reason ?? undefined} />
      </div>

      {check && (
        <div className={`mt-2 rounded-md border px-2.5 py-2 text-xs ${check.ok ? "border-adm-ok/30 bg-adm-ok/10" : "border-adm-bad/30 bg-adm-bad/10"}`}>
          <p className="font-medium">{check.ok ? "✓ Connected" : "✗ Not ready"} · {check.summary}</p>
          {check.lines.length > 0 && (
            <pre className="adm-mono mt-1 whitespace-pre-wrap text-[11px] text-adm-soft">{check.lines.join("\n")}</pre>
          )}
          {check.checked_at && <p className="mt-1 text-[10px] text-adm-faint">checked {timeAgo(check.checked_at)}</p>}
        </div>
      )}

      <div className="mt-2.5 flex flex-wrap gap-1.5">
        <button type="button" className="adm-btn px-2 py-1 text-xs" disabled={busy !== null} onClick={() => void test()}>
          {busy === "test" ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> : <PlugZap className="h-3.5 w-3.5" />}
          Test connection
        </button>
        <button type="button" className="adm-btn px-2 py-1 text-xs" onClick={onEdit}>
          <Pencil className="h-3.5 w-3.5" /> Edit
        </button>
        <button
          type="button"
          className="adm-btn px-2 py-1 text-xs"
          disabled={busy !== null}
          onClick={() =>
            void run(
              "toggle",
              () => adminApi(`social-accounts/${encodeURIComponent(account.id)}`, { method: "PUT", body: { enabled: !account.enabled } }),
              account.enabled ? "Page paused" : "Page enabled",
            ).then((ok) => ok && onChanged())
          }
        >
          <Power className="h-3.5 w-3.5" /> {account.enabled ? "Disable" : "Enable"}
        </button>
        <button
          type="button"
          className="adm-btn-danger px-2 py-1 text-xs"
          disabled={busy !== null}
          onClick={() => {
            if (!window.confirm(`Unlink ${account.page_handle}? Its publication history is kept.`)) return;
            void run("delete", () => adminApi(`social-accounts/${encodeURIComponent(account.id)}`, { method: "DELETE" }), "Page unlinked").then(
              (ok) => ok && onChanged(),
            );
          }}
        >
          <Trash2 className="h-3.5 w-3.5" /> Unlink
        </button>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //
export function AccountsView() {
  const accounts = useResource<AccountsResponse>("social-accounts");
  const categories = useResource<CategoriesResponse>("categories");
  const [wizard, setWizard] = useState(false);
  const [editing, setEditing] = useState<SocialAccount | null>(null);
  const [platformFilter, setPlatformFilter] = useState("all");
  const { busy, run } = useAction();

  const reload = () => {
    void accounts.reload();
    void categories.reload();
  };

  const grouped = useMemo(() => {
    const list = (accounts.data?.accounts ?? []).filter((a) => platformFilter === "all" || a.platform === platformFilter);
    const cats = categories.data?.categories ?? [];
    const ids = Array.from(new Set([...cats.map((c) => c.category_id), ...list.map((a) => a.category_id)]));
    const languages = Array.from(new Set(["he", "en", ...list.map((a) => a.language)]));
    return ids.map((id) => ({
      category: cats.find((c) => c.category_id === id),
      id,
      languages: languages.map((language) => ({
        language,
        accounts: list.filter((a) => a.category_id === id && a.language === language),
      })),
    }));
  }, [accounts.data, categories.data, platformFilter]);

  const all = accounts.data?.accounts ?? [];
  const testAll = async () => {
    const targets = all.filter((account) => account.enabled);
    const results = await run("all", () =>
      Promise.all(
        targets.map((account) =>
          adminApi<VerifyResult>(`actions/verify-account/${encodeURIComponent(account.id)}`, { method: "POST" }).catch(
            (error: unknown) => ({ ok: false, summary: errorText(error) }) as VerifyResult,
          ),
        ),
      ),
    );
    if (results) {
      const ok = results.filter((result) => result.ok).length;
      window.alert(`${ok} of ${results.length} enabled page(s) passed the live connection check.\nOpen a card's “Test connection” for details.`);
    }
  };

  return (
    <>
      <PageHeader
        title="Social accounts"
        subtitle="One page per category × language × platform. Credentials are referenced by environment-variable NAME only; the backend reports whether each one is set."
        actions={
          <>
            <button type="button" className="adm-btn" disabled={busy !== null || all.length === 0} onClick={() => void testAll()}>
              {busy === "all" ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <PlugZap className="h-4 w-4" />} Test all
            </button>
            <button type="button" className="adm-btn-primary" onClick={() => setWizard(true)} disabled={!accounts.data}>
              <Plus className="h-4 w-4" /> Connect new social page
            </button>
          </>
        }
      />

      <div className="mb-4 flex flex-wrap gap-1.5">
        {["all", ...PLATFORM_ORDER].map((platform) => (
          <button
            key={platform}
            type="button"
            onClick={() => setPlatformFilter(platform)}
            className={`adm-btn px-2.5 py-1 text-xs ${platformFilter === platform ? "border-adm-accent bg-adm-accent/10 text-adm-accent" : ""}`}
          >
            {platform === "all" ? `All platforms (${all.length})` : `${PLATFORM_LABELS[platform]} (${all.filter((a) => a.platform === platform).length})`}
          </button>
        ))}
      </div>

      {(accounts.error || categories.error) && <ErrorNote error={(accounts.error ?? categories.error) as string} onRetry={reload} />}
      {(accounts.loading || categories.loading) && !accounts.data && <Loading />}

      <div className="space-y-4">
        {grouped.map((group) => (
          <Panel
            key={group.id}
            title={
              <span className="flex flex-wrap items-center gap-2">
                {group.category?.display_name_en ?? group.id}
                {group.category?.display_name_he && <span className="font-normal text-adm-soft" dir="rtl">{group.category.display_name_he}</span>}
                <code className="adm-mono text-[11px] font-normal text-adm-faint">{group.id}</code>
                {group.category && !group.category.enabled && <Badge tone="warn">category disabled</Badge>}
              </span>
            }
          >
            <div className="grid gap-3 md:grid-cols-2">
              {group.languages.map(({ language, accounts: cell }) => (
                <div key={language} className="min-w-0">
                  <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-adm-faint">{LANGUAGE_LABELS[language] ?? language}</p>
                  {cell.length === 0 ? (
                    <p className="rounded-lg border border-dashed border-adm-line px-3 py-4 text-center text-xs text-adm-faint">No page linked</p>
                  ) : (
                    <div className="space-y-2">
                      {cell.map((account) => (
                        <AccountCard key={account.id} account={account} onEdit={() => setEditing(account)} onChanged={reload} />
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </Panel>
        ))}
      </div>

      {wizard && accounts.data && (
        <ConnectWizard
          platforms={accounts.data.platforms}
          categories={categories.data?.categories ?? []}
          onClose={() => setWizard(false)}
          onCreated={() => {
            setWizard(false);
            reload();
          }}
        />
      )}
      {editing && (
        <EditAccount
          account={editing}
          fields={accounts.data?.platforms[editing.platform]}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
          }}
        />
      )}
    </>
  );
}
