"use client";

// Smart filters (store, rating, orders, price range) and the sort control.
// The same panel is the sidebar on wide screens and the content of the filter sheet on
// phones. Choices apply at once; a price applies when the field is left or Enter is pressed.

import { ChevronDown } from "lucide-react";
import { useEffect, useId, useState, type ReactNode } from "react";

import { SORT_OPTIONS, type SortBy, type StoreFacet } from "@/lib/api";
import { ORDER_STEPS, RATING_STEPS, countSidebarFilters, type Filters } from "@/lib/filters";
import { ILS_PER_USD, currencySymbol, formatNumber, formatRating, toCatalogPrice } from "@/lib/format";
import { getDictionary, type Lang } from "@/lib/i18n";

type FilterSidebarProps = {
  lang: Lang;
  filters: Filters;
  stores: StoreFacet[];
  onChange: (patch: Partial<Filters>) => void;
  onClear: () => void;
};

function Group({ legend, children }: { legend: string; children: ReactNode }) {
  return (
    <fieldset className="border-t border-line py-4 first:border-t-0 first:pt-0">
      <legend className="float-start mb-2.5 w-full text-sm font-bold text-ink">{legend}</legend>
      <div className="clear-both flex flex-col gap-0.5">{children}</div>
    </fieldset>
  );
}

function Choice({
  name,
  checked,
  onSelect,
  children,
  count,
}: {
  name: string;
  checked: boolean;
  onSelect: () => void;
  children: ReactNode;
  count?: number;
}) {
  return (
    <label className="-mx-2 flex cursor-pointer items-center gap-2.5 rounded-lg px-2 py-1.5 text-sm text-ink-soft transition-colors hover:bg-line/40 has-[:checked]:font-semibold has-[:checked]:text-ink">
      <input type="radio" name={name} checked={checked} onChange={onSelect} className="size-4 shrink-0 accent-spruce" />
      <span className="flex-1">{children}</span>
      {count !== undefined ? <span className="text-xs tabular-nums text-ink-faint">{count}</span> : null}
    </label>
  );
}

/** A price as the shopper types it (display currency) for a catalogue price (ILS). */
function displayPrice(value: number | null, lang: Lang): string {
  if (value === null) return "";
  return String(lang === "en" ? Math.round((value / ILS_PER_USD) * 100) / 100 : value);
}

function PriceField({
  id,
  label,
  value,
  lang,
  onCommit,
}: {
  id: string;
  label: string;
  value: number | null;
  lang: Lang;
  onCommit: (value: number | null) => void;
}) {
  const [text, setText] = useState(() => displayPrice(value, lang));
  useEffect(() => setText(displayPrice(value, lang)), [value, lang]);

  const commit = () => {
    const number = Number(text.replace(",", "."));
    const next = text.trim() !== "" && Number.isFinite(number) && number > 0 ? toCatalogPrice(number, lang) : null;
    if (next !== value) onCommit(next);
    else setText(displayPrice(value, lang));
  };

  return (
    <div className="flex-1">
      <label htmlFor={id} className="mb-1 block text-xs font-medium text-ink-soft">
        {label}
      </label>
      <input
        id={id}
        type="number"
        inputMode="decimal"
        min={0}
        dir="ltr"
        value={text}
        onChange={(event) => setText(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            commit();
          }
        }}
        className="h-10 w-full rounded-[10px] border border-line-strong bg-surface px-3 text-sm tabular-nums text-ink transition-colors hover:border-ink-faint focus:border-spruce-bright focus:outline-none focus:ring-2 focus:ring-spruce-bright/25"
      />
    </div>
  );
}

export function FilterSidebar({ lang, filters, stores, onChange, onClear }: FilterSidebarProps) {
  const t = getDictionary(lang);
  const id = useId();
  const active = countSidebarFilters(filters);

  return (
    <form onSubmit={(event) => event.preventDefault()} aria-label={t.filters.title}>
      {stores.length > 1 ? (
        <Group legend={t.filters.store}>
          <Choice name={`${id}-store`} checked={filters.source === null} onSelect={() => onChange({ source: null })}>
            {t.filters.anyStore}
          </Choice>
          {stores.map((store) => (
            <Choice
              key={store.key}
              name={`${id}-store`}
              checked={filters.source === store.key}
              onSelect={() => onChange({ source: store.key })}
              count={store.products}
            >
              <span dir="ltr">{store.name}</span>
            </Choice>
          ))}
        </Group>
      ) : null}

      <Group legend={t.filters.rating}>
        <Choice name={`${id}-rating`} checked={filters.minRating === null} onSelect={() => onChange({ minRating: null })}>
          {t.filters.anyRating}
        </Choice>
        {RATING_STEPS.map((step) => (
          <Choice
            key={step}
            name={`${id}-rating`}
            checked={filters.minRating === step}
            onSelect={() => onChange({ minRating: step })}
          >
            {t.filters.ratingFrom(formatRating(step))}
          </Choice>
        ))}
      </Group>

      <Group legend={t.filters.orders}>
        <Choice name={`${id}-orders`} checked={filters.minOrders === null} onSelect={() => onChange({ minOrders: null })}>
          {t.filters.anyOrders}
        </Choice>
        {ORDER_STEPS.map((step) => (
          <Choice
            key={step}
            name={`${id}-orders`}
            checked={filters.minOrders === step}
            onSelect={() => onChange({ minOrders: step })}
          >
            {t.filters.ordersFrom(formatNumber(step, lang))}
          </Choice>
        ))}
      </Group>

      <Group legend={t.filters.price(currencySymbol(lang))}>
        <div className="flex items-end gap-2.5">
          <PriceField
            id={`${id}-min`}
            label={t.filters.priceMin}
            value={filters.minPrice}
            lang={lang}
            onCommit={(value) => onChange({ minPrice: value })}
          />
          <PriceField
            id={`${id}-max`}
            label={t.filters.priceMax}
            value={filters.maxPrice}
            lang={lang}
            onCommit={(value) => onChange({ maxPrice: value })}
          />
        </div>
      </Group>

      {active > 0 ? (
        <button
          type="button"
          onClick={onClear}
          className="mt-1 text-sm font-semibold text-spruce underline decoration-spruce/30 underline-offset-4 hover:decoration-spruce"
        >
          {t.filters.clear}
        </button>
      ) : null}
    </form>
  );
}

/** The sort control. "Best match" replaces "Recommended" as the default while a search is active. */
export function SortSelect({
  lang,
  value,
  searching,
  onChange,
}: {
  lang: Lang;
  value: SortBy | null;
  searching: boolean;
  onChange: (value: SortBy | null) => void;
}) {
  const t = getDictionary(lang);
  const id = useId();
  return (
    <div className="relative shrink-0">
      <label htmlFor={id} className="sr-only">
        {t.sort.label}
      </label>
      <select
        id={id}
        value={value ?? ""}
        onChange={(event) => onChange((event.target.value || null) as SortBy | null)}
        className="h-10 cursor-pointer appearance-none rounded-[10px] border border-line-strong bg-surface pe-9 ps-3 text-sm font-semibold text-ink transition-colors hover:border-ink-faint focus:border-spruce-bright focus:outline-none focus:ring-2 focus:ring-spruce-bright/25"
      >
        <option value="">{searching ? t.sort.options.relevance : t.sort.options.recommended}</option>
        {SORT_OPTIONS.map((option) => (
          <option key={option} value={option}>
            {t.sort.options[option]}
          </option>
        ))}
      </select>
      <ChevronDown aria-hidden="true" size={16} className="pointer-events-none absolute end-3 top-1/2 -translate-y-1/2 text-ink-soft" />
    </div>
  );
}
