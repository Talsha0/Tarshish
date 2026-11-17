"""Tarshish Engine — admin & analytics dashboard.

Run from the project root:

    python -m streamlit run dashboard/app.py

In production it runs on Streamlit Community Cloud (entrypoint ``tarshish_engine/dashboard/app.py``,
dependencies from ``dashboard/requirements.txt``) against the Supabase database, and asks for
``DASHBOARD_PASSWORD`` before showing anything.

Tabs
----
1. Categories & Search   — categories, their Temu / AliExpress search rules, "Harvest Now" (Stage 1)
2. Social Accounts       — the category × language × platform matrix: handles, .env variable names, on/off
3. Content Types & AI    — generator + LLM model + tone per content type (instagram_post, instagram_story…)
4. Run Pipelines & KPIs  — targeted publishing runs (Stage 2, dry-run by default), inventory, KPIs,
                           budget, queue and logs

The page talks to the same engine objects as the CLI (``core`` + ``MetricsService``),
and configuration is read from SQLite on every rerun, so edits apply immediately.

Only Streamlit's basic widgets are used, with small HTML snippets for tables and bars.
Streamlit's dataframe, data-editor and chart elements all load the native Apache Arrow
library, which Windows Smart App Control can refuse to load; avoiding them keeps the
dashboard working on locked-down machines.
"""

from __future__ import annotations

import contextlib
import hmac
import html
import io
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # `streamlit run` puts dashboard/ on sys.path, not the project root
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from core import Engine, bootstrap  # noqa: E402
from core.config import (  # noqa: E402
    PUBLISH_FORMATS,
    CategoryConfig,
    ContentProfile,
    SocialAccount,
    describe_validation_error,
)
from core.models import category_slug  # noqa: E402
from core.resilience import ConfigError, TarshishError, redact  # noqa: E402
from dashboard.metrics_service import MetricsService  # noqa: E402

TAB_CATEGORIES = "Categories & Search"
TAB_ACCOUNTS = "Social Accounts"
TAB_CONTENT = "Content Types & AI Models"
TAB_RUN = "Run Pipelines & KPIs"
TAB_KPIS, TAB_QUEUE = "KPIs & Budget", "Queue & Logs"   # inside Tab 4
_TAB_SLUGS = {"categories": TAB_CATEGORIES, "accounts": TAB_ACCOUNTS, "content": TAB_CONTENT, "run": TAB_RUN,
              "kpis": TAB_RUN, "queue": TAB_RUN}
NEW_CATEGORY = "➕ Add a new category"
SELECT_CATEGORY = "_select_category"  # category to show in the form on the next draw
NO_FORMAT = "(publisher decides)"
LANGUAGE_LABELS = {"he": "Hebrew (he)", "en": "English (en)", "ar": "Arabic (ar)", "ru": "Russian (ru)"}
RTL_LANGUAGES = {"he", "ar", "fa", "ur"}

# One series hue for every bar (categories are nominal), plus reserved status colors.
SERIES_BLUE = {"light": "#2a78d6", "dark": "#3987e5"}
STATUS = {
    "ok": {"color": None, "icon": "✅", "label": "Within budget"},
    "warning": {"color": "#fab219", "icon": "⚠️", "label": "Over 70% of the budget used"},
    "critical": {"color": "#ec835a", "icon": "🔶", "label": "Over 90% of the budget used"},
    "exceeded": {"color": "#d03b3b", "icon": "⛔", "label": "Budget exhausted — paid LLM calls are halted"},
}

st.set_page_config(page_title="Tarshish Engine", page_icon="⛵", layout="wide")

st.markdown(
    """
    <style>
      .tsh-flow { display: flex; flex-wrap: wrap; align-items: stretch; gap: 10px; margin: 4px 0 2px; }
      .tsh-node { flex: 1 1 230px; border: 1px solid rgba(128,128,128,.32); border-radius: 10px;
                  padding: 12px 14px; background: rgba(128,128,128,.06); }
      .tsh-arrow { display: flex; align-items: center; font-size: 1.3rem; opacity: .55; }
      .tsh-kicker { font-size: .68rem; letter-spacing: .08em; text-transform: uppercase; opacity: .62; }
      .tsh-main { font-weight: 600; font-size: 1.02rem; margin: 2px 0 4px; word-break: break-word; }
      .tsh-sub { font-size: .84rem; opacity: .82; line-height: 1.45; word-break: break-word; }
      .tsh-page { font-size: .88rem; line-height: 1.5; word-break: break-word; }
      .tsh-page small { opacity: .7; }
      .tsh-caption { white-space: pre-wrap; text-align: start; line-height: 1.5;
                     border-inline-start: 3px solid rgba(128,128,128,.4); padding: 4px 12px; margin: 6px 0; }
      .tsh-meter-track { height: 12px; border-radius: 999px; overflow: hidden; }
      .tsh-meter-fill { height: 100%; border-radius: 999px; }
      .tsh-meter-head { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px;
                        font-size: .9rem; margin-bottom: 6px; }
      .tsh-meter-value { font-size: 1.5rem; font-weight: 600; }

      .tsh-table-wrap { overflow: auto; border: 1px solid rgba(128,128,128,.3); border-radius: 8px; margin: 4px 0 10px; }
      .tsh-table { width: 100%; border-collapse: collapse; font-size: .86rem; }
      .tsh-table th { text-align: left; font-weight: 600; white-space: nowrap; padding: 8px 12px;
                      background: rgba(128,128,128,.10); border-bottom: 1px solid rgba(128,128,128,.3); }
      .tsh-table td { padding: 7px 12px; border-bottom: 1px solid rgba(128,128,128,.16); vertical-align: top;
                      white-space: nowrap; }
      .tsh-table tr:last-child td { border-bottom: 0; }
      .tsh-table .num { text-align: right; font-variant-numeric: tabular-nums; }
      .tsh-table td.clip { max-width: 360px; overflow: hidden; text-overflow: ellipsis; }

      .tsh-bars { margin: 2px 0 14px; }
      .tsh-bars-title { font-weight: 600; margin-bottom: 8px; }
      .tsh-bar-row { display: grid; grid-template-columns: minmax(70px, 34%) 1fr; align-items: center;
                     column-gap: 10px; padding: 5px 0; }
      .tsh-bar-name { font-size: .84rem; opacity: .85; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .tsh-bar-area { display: flex; align-items: center; gap: 7px; min-width: 0;
                      border-left: 1px solid rgba(128,128,128,.55); }
      .tsh-bar-fill { height: 16px; min-width: 2px; border-radius: 0 4px 4px 0; }
      .tsh-bar-value { font-size: .8rem; opacity: .78; font-variant-numeric: tabular-nums; white-space: nowrap; }
    </style>
    """,
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------- #
# Engine + small helpers
# --------------------------------------------------------------------------- #
@st.cache_resource(show_spinner=False)
def get_engine() -> Engine:
    return bootstrap()


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def theme_mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


def compact(value: float) -> str:
    """1,284 · 12.9K · 4.2M — for stat tiles."""
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if magnitude >= 10_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,.0f}"


def money(value: float, digits: int = 2) -> str:
    return f"-${abs(value):,.{digits}f}" if value < 0 else f"${value:,.{digits}f}"


def whole(value: float) -> str:
    return f"{value:,.0f}"


def local_time(engine: Engine, iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(engine.settings.tzinfo).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def flash(message: str, kind: str = "success") -> None:
    """Show a message after the next rerun."""
    st.session_state["_flash"] = (kind, message)


def show_flash() -> None:
    kind, message = st.session_state.pop("_flash", (None, None))
    if message:
        getattr(st, kind)(message)


def html_table(
    rows: list[dict[str, Any]],
    columns: dict[str, str],
    *,
    formats: dict[str, Callable[[Any], str]] | None = None,
    numeric: tuple[str, ...] = (),
    clipped: tuple[str, ...] = (),
) -> None:
    """Render rows as a plain HTML table (every value escaped; long cells clipped with a hover title)."""
    if not rows:
        st.caption("Nothing here.")
        return
    formats = formats or {}
    head = "".join(f'<th class="{"num" if key in numeric else ""}">{esc(label)}</th>' for key, label in columns.items())
    body = []
    for row in rows:
        cells = []
        for key in columns:
            value = row.get(key)
            text = "" if value is None else formats[key](value) if key in formats else str(value)
            classes = " ".join(name for name, on in (("num", key in numeric), ("clip", key in clipped)) if on)
            title = f' title="{esc(text)}" dir="auto"' if key in clipped else ""
            cells.append(f'<td class="{classes}"{title}>{esc(text)}</td>')
        body.append(f"<tr>{''.join(cells)}</tr>")
    st.markdown(
        f'<div class="tsh-table-wrap"><table class="tsh-table"><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
# Shared status helpers
# --------------------------------------------------------------------------- #
def account_status(engine: Engine, account: SocialAccount) -> tuple[str, str]:
    """``(icon, text)`` describing whether an account can publish right now."""
    if not account.enabled:
        return "⏸", "disabled"
    if not engine.plugins.has("publisher", account.publisher):
        return "🧩", f"no {account.publisher} plugin installed"
    _, reason = engine.registry.resolve_target(engine.registry.social_page(account))
    if reason:
        return "⚠️", reason
    return "✅", "ready"


def store_label(source: str) -> str:
    return {"temu_rapidapi": "Temu (RapidAPI)", "aliexpress_rapidapi": "AliExpress (RapidAPI)",
            "temu_mock": "Temu mock (offline)", "aliexpress_mock": "AliExpress mock (offline)"}.get(source, source)


# --------------------------------------------------------------------------- #
# Tab 1 — categories & source search mapping (Stage 1)
# --------------------------------------------------------------------------- #
def search_summary(search: dict[str, Any] | None) -> str:
    if not search:
        return "—"
    parts = [f"“{search['keyword']}”"] if search.get("keyword") else []
    for key in ("l1Category", "l2Category", "categoryId", "sort"):
        if search.get(key):
            parts.append(f"{key} {search[key]}")
    low, high = search.get("priceMin"), search.get("priceMax")
    if low is not None or high is not None:
        parts.append(f"price {low if low is not None else '…'}–{high if high is not None else '…'}")
    return " · ".join(parts)


def run_harvest(engine: Engine, category: CategoryConfig, source: str) -> None:
    with st.spinner(f"Harvesting {category.category_id} from {store_label(source)}…"):
        report = engine.harvester.harvest_one(category, source)
    st.session_state[f"harvest_result_{category.category_id}"] = report


def render_harvest_result(category_id: str) -> None:
    report = st.session_state.get(f"harvest_result_{category_id}")
    if report is None:
        return
    summary = (f"{store_label(report.source)}: received {report.received} · inserted {len(report.inserted)} · "
               f"already stored {len(report.duplicates)} · rejected {len(report.rejected)}")
    if report.error:
        st.error(f"{summary}\n\n{report.error}")
    elif report.provider is None:
        st.warning(f"{summary} — no provider delivered products (see the provider chain below).")
    else:
        st.success(summary)


def render_category_card(engine: Engine, category: CategoryConfig, inventory: dict[tuple[str, str], int]) -> None:
    cid = category.category_id
    with st.container(border=True):
        head = st.columns([4, 1.3, 1], vertical_alignment="center")
        head[0].markdown(f"#### {esc(category.display_name_en)} · {esc(category.display_name_he or '—')}  "
                         f"<small>`{esc(cid)}`</small>", unsafe_allow_html=True)
        enabled = head[1].toggle("Enabled", value=category.enabled, key=f"cat_enabled_{cid}")
        if enabled != category.enabled:
            engine.registry.save_category(category.model_copy(update={"enabled": enabled}))
            st.rerun()
        if head[2].button("Edit", key=f"cat_edit_{cid}", width="stretch"):
            st.session_state[SELECT_CATEGORY] = cid
        stored = {source: count for (cat, source), count in inventory.items() if cat == cid}
        html_table(
            [
                {"store": "Temu", "rules": search_summary(category.temu_search),
                 "stored": sum(n for src, n in stored.items() if src.startswith("temu"))},
                {"store": "AliExpress", "rules": search_summary(category.aliexpress_search),
                 "stored": sum(n for src, n in stored.items() if src.startswith("aliexpress"))},
            ],
            {"store": "Store", "rules": "Search rules (English, shared by every language)", "stored": "Harvested"},
            numeric=("stored",), clipped=("rules",),
        )
        st.caption(f"Quality bar: rating ≥ {category.min_rating:g} · orders ≥ {category.min_orders:,}")
        sources = [name for name in engine.plugins.names("source")]
        defaults = category.default_sources()
        row = st.columns([2.2, 1.2, 3], vertical_alignment="bottom")
        choice = row[0].selectbox(
            "Source", sources, index=sources.index(defaults[0]) if defaults and defaults[0] in sources else 0,
            format_func=store_label, key=f"harvest_source_{cid}",
            help="Live RapidAPI sources spend API quota; the mock sources serve an offline catalogue.",
        )
        if row[1].button("Harvest Now", key=f"harvest_{cid}", type="primary", width="stretch"):
            run_harvest(engine, category, choice)
        row[2].caption("Harvested once per store, stored in SQLite and shared by every language's pipeline.")
        render_harvest_result(cid)


def _price(value: Any) -> float | None:
    return None if value is None else float(value)


def store_inputs(
    title: str, store: str, current: dict[str, Any] | None, category_key: str, suffix: str
) -> tuple[bool, dict[str, Any]]:
    """Plain widgets for one store's search rules; extra keys of the stored rules are preserved."""
    current = dict(current or {})
    st.markdown(f"**{title}**")
    use = st.checkbox(f"Harvest from {title}", value=bool(current) or suffix == "new", key=f"cf_{store}_on_{suffix}")
    cols = st.columns([2, 1.6, 1, 1])
    keyword = cols[0].text_input("Keyword (English)", value=current.get("keyword", ""), key=f"cf_{store}_kw_{suffix}")
    scope = cols[1].text_input(category_key, value=str(current.get(category_key, "") or ""), key=f"cf_{store}_cat_{suffix}")
    low = cols[2].number_input("Min price", min_value=0.0, value=_price(current.get("priceMin")), key=f"cf_{store}_min_{suffix}")
    high = cols[3].number_input("Max price", min_value=0.0, value=_price(current.get("priceMax")), key=f"cf_{store}_max_{suffix}")
    for key, value in ((("keyword", keyword.strip())), (category_key, scope.strip()), ("priceMin", low), ("priceMax", high)):
        if value in (None, ""):
            current.pop(key, None)
        else:
            current[key] = value
    return use, current


def render_category_form(engine: Engine, categories: list[CategoryConfig]) -> None:
    st.subheader("Add / edit a category")
    by_id = {category.category_id: category for category in categories}
    requested = st.session_state.pop(SELECT_CATEGORY, None)
    if requested is not None:
        st.session_state["form_category"] = requested
    if st.session_state.get("form_category") not in (None, NEW_CATEGORY, *by_id):
        st.session_state["form_category"] = NEW_CATEGORY
    selected = st.selectbox("Category", [NEW_CATEGORY, *by_id], key="form_category")
    current = by_id.get(selected)
    suffix = current.category_id if current else "new"

    with st.form(f"category_form_{suffix}", border=True):
        cols = st.columns(3)
        category_id = cols[0].text_input(
            "Category ID", value=current.category_id if current else "", disabled=current is not None,
            placeholder="e.g. sports", key=f"cf_id_{suffix}",
            help="A URL slug (the storefront serves /c/<id>). Cannot be changed later.",
        )
        name_en = cols[1].text_input("Display name (English)", value=current.display_name_en if current else "",
                                     key=f"cf_en_{suffix}")
        name_he = cols[2].text_input("Display name (Hebrew)", value=current.display_name_he if current else "",
                                     key=f"cf_he_{suffix}")
        cols = st.columns(3)
        min_rating = cols[0].number_input("Minimum rating", min_value=0.0, max_value=5.0, step=0.1, format="%.1f",
                                          value=float(current.min_rating) if current else 4.5, key=f"cf_rating_{suffix}")
        min_orders = cols[1].number_input("Minimum orders", min_value=0, step=50,
                                          value=current.min_orders if current else 200, key=f"cf_orders_{suffix}")
        enabled = cols[2].checkbox("Enabled", value=current.enabled if current else True, key=f"cf_enabled_{suffix}")
        temu_on, temu = store_inputs("Temu", "temu", current.temu_search if current else None, "l2Category", suffix)
        ali_on, ali = store_inputs("AliExpress", "ali", current.aliexpress_search if current else None, "categoryId", suffix)
        submitted = st.form_submit_button("Save category", type="primary")

    if submitted:
        payload = {
            "category_id": current.category_id if current else category_id,
            "display_name_en": name_en, "display_name_he": name_he,
            "min_rating": float(min_rating), "min_orders": int(min_orders), "enabled": enabled,
            "temu_search": temu if temu_on else None, "aliexpress_search": ali if ali_on else None,
        }
        if current is None and engine.db.get_config("categories", category_slug(category_id or "")):
            st.error(f"A category with id '{category_slug(category_id)}' already exists — select it above to edit it.")
            return
        try:
            category = CategoryConfig.model_validate(payload)
            engine.registry.save_category(category)
        except ValidationError as exc:
            st.error("The category is not valid:\n\n" + "\n".join(f"- {line}" for line in describe_validation_error(exc)))
            return
        except TarshishError as exc:
            st.error(str(exc))
            return
        st.session_state[SELECT_CATEGORY] = category.category_id
        flash(f"Category '{category.category_id}' saved — the next harvest uses it.")
        st.rerun()


def render_categories_tab(engine: Engine) -> None:
    categories = engine.registry.list_categories()
    inventory = {(row["category"], row["source"]): int(row["products"]) for row in engine.db.count_products()}
    st.subheader("Categories & source search mapping")
    st.caption("Stage 1 — language-agnostic harvesting: each category is fetched once per store and stored in "
               "SQLite; every language's publishing pipeline reads the same products.")
    if not categories:
        st.info("No categories yet — add the first one below.")
    for category in categories:
        render_category_card(engine, category, inventory)
    render_temu_provider_chain(engine)
    render_aliexpress_provider_chain(engine)
    st.divider()
    render_category_form(engine, categories)


# --------------------------------------------------------------------------- #
# Tab 2 — social accounts matrix (category × language × platform)
# --------------------------------------------------------------------------- #
def platforms_of(engine: Engine) -> list[str]:
    found = {profile.platform for profile in engine.registry.list_content_profiles()}
    found |= {account.platform for account in engine.registry.list_social_accounts()}
    return sorted(found)


def render_matrix(engine: Engine, categories: list[CategoryConfig], languages: list[str], platforms: list[str]) -> None:
    accounts = {account.key: account for account in engine.registry.list_social_accounts()}
    for category in categories:
        rows = []
        for platform in platforms:
            row: dict[str, Any] = {"platform": platform}
            for language in languages:
                account = accounts.get((category.category_id, language, platform))
                if account is None:
                    row[language] = "— not linked —"
                    continue
                icon, text = account_status(engine, account)
                env = ", ".join(name for name in (account.token_env_var, account.account_id_env_var) if name)
                row[language] = f"{icon} {account.account_handle} · {text}" + (f" · env {env}" if env else "")
            rows.append(row)
        st.markdown(f"**{esc(category.display_name_en)}** <small>`{esc(category.category_id)}`</small>",
                    unsafe_allow_html=True)
        html_table(rows, {"platform": "Platform", **{lang: LANGUAGE_LABELS.get(lang, lang) for lang in languages}},
                   clipped=tuple(languages))


def render_account_form(engine: Engine, categories: list[CategoryConfig], languages: list[str], platforms: list[str]) -> None:
    st.subheader("Link / edit an account")
    if not categories:
        st.info("Add a category first (Categories & Search tab).")
        return
    cols = st.columns(3)
    category = cols[0].selectbox("Category", [c.category_id for c in categories], key="acc_category")
    language = cols[1].selectbox("Language", languages, accept_new_options=True, key="acc_language",
                                 format_func=lambda code: LANGUAGE_LABELS.get(code, code))
    platform = cols[2].selectbox("Platform", platforms or ["instagram"], accept_new_options=True, key="acc_platform",
                                 help="The platform picks the publisher plugin: instagram ➔ instagram_pub.")
    current = engine.registry.get_social_account(category, language, platform) if language and platform else None
    suffix = f"{category}_{language}_{platform}"
    st.caption(f"Editing **{category} × {language} × {platform}** — "
               + ("existing account." if current else "not linked yet; saving creates it."))
    with st.form(f"account_form_{suffix}", border=True):
        cols = st.columns([1.4, 1.6, 1.6, 0.8], vertical_alignment="bottom")
        handle = cols[0].text_input("Page / handle", value=current.account_handle if current else "",
                                    placeholder="e.g. @gadgets.il", key=f"af_handle_{suffix}")
        token = cols[1].text_input("Token env var", value=(current.token_env_var or "") if current else "",
                                   placeholder="e.g. INSTAGRAM_ACCESS_TOKEN", key=f"af_token_{suffix}")
        account_id = cols[2].text_input("Account / chat ID env var",
                                        value=(current.account_id_env_var or "") if current else "",
                                        placeholder="e.g. INSTAGRAM_BUSINESS_ACCOUNT_ID", key=f"af_id_{suffix}")
        enabled = cols[3].checkbox("Enabled", value=current.enabled if current else True, key=f"af_enabled_{suffix}")
        st.caption("The env-var fields hold the **names** of `.env` variables — never the token itself.")
        submitted = st.form_submit_button("Save account", type="primary")
    if submitted:
        try:
            account = SocialAccount.model_validate({
                "category_id": category, "language": language, "platform": platform, "account_handle": handle,
                "token_env_var": token, "account_id_env_var": account_id, "enabled": enabled,
            })
            engine.registry.save_social_account(account)
        except ValidationError as exc:
            st.error("The account is not valid:\n\n" + "\n".join(f"- {line}" for line in describe_validation_error(exc)))
            return
        except TarshishError as exc:
            st.error(str(exc))
            return
        flash(f"Account {account.label} saved — the next publishing run uses it.")
        st.rerun()
    if current is not None:
        with st.popover("Unlink this account"):
            st.write(f"Remove **{current.label}** from the matrix? Its publication history is kept.")
            if st.button("Yes, unlink", key=f"unlink_{suffix}", type="primary"):
                engine.db.delete_config("social_accounts", *current.key)
                flash(f"Account {current.label} unlinked.")
                st.rerun()


def render_accounts_tab(engine: Engine) -> None:
    categories = engine.registry.list_categories()
    languages = engine.registry.languages()
    platforms = platforms_of(engine)
    st.subheader("Social accounts matrix · category × language × platform")
    st.caption("Each publishing pipeline (category + language) posts to the account in its cell. Hebrew and "
               "English pages sit side by side; credentials are referenced by `.env` variable name only.")
    render_matrix(engine, categories, languages, platforms)
    if any(account.platform == "instagram" for account in engine.registry.list_social_accounts()):
        render_instagram_connection(engine)
    st.divider()
    render_account_form(engine, categories, languages, platforms)


# --------------------------------------------------------------------------- #
# Tab 3 — content types & AI model assignment
# --------------------------------------------------------------------------- #
def model_options(engine: Engine, current: str | None = None) -> list[str]:
    plugins = engine.plugins
    models = list(dict.fromkeys(
        m for name in plugins.names("generator") for m in plugins.get("generator", name).supported_models()
    ))
    if current and current not in models:
        models.append(current)
    return models


def render_profile_form(engine: Engine, profile: ContentProfile | None) -> None:
    suffix = profile.content_type if profile else "new"
    generators = engine.plugins.names("generator")
    models = model_options(engine, profile.llm_model if profile else None)
    formats = [NO_FORMAT, *PUBLISH_FORMATS]

    def index_of(options: list[str], value: str | None) -> int:
        return options.index(value) if value in options else 0

    with st.form(f"profile_form_{suffix}", border=True):
        cols = st.columns([1.4, 1, 1.3, 1.6, 1])
        content_type = cols[0].text_input(
            "Content type", value=profile.content_type if profile else "", disabled=profile is not None,
            placeholder="e.g. youtube_short", key=f"pf_type_{suffix}",
        )
        platform = cols[1].text_input("Platform", value=profile.platform if profile else "", placeholder="e.g. youtube",
                                      key=f"pf_platform_{suffix}")
        generator = cols[2].selectbox("Generator", generators,
                                      index=index_of(generators, profile.generator if profile else "llm_text_gen"),
                                      key=f"pf_generator_{suffix}")
        llm_model = cols[3].selectbox(
            "LLM model", models, index=index_of(models, profile.llm_model if profile else "claude-sonnet-5-5"),
            accept_new_options=True, key=f"pf_model_{suffix}",
            help="A model id, a provider alias (anthropic / gemini) or 'mock' for free offline templates. "
                 "Type to enter any other model id.",
        )
        publish_format = cols[4].selectbox("Publish format", formats,
                                           index=index_of(formats, profile.publish_format if profile else None),
                                           key=f"pf_format_{suffix}")
        cols = st.columns([3, 1])
        tone = cols[0].text_input("Tone", value=(profile.tone or "") if profile else "",
                                  placeholder="e.g. punchy and playful", key=f"pf_tone_{suffix}")
        enabled = cols[1].checkbox("Enabled", value=profile.enabled if profile else True, key=f"pf_enabled_{suffix}")
        prompt_template = st.text_area(
            "Custom prompt template (optional)", value=(profile.prompt_template or "") if profile else "", height=90,
            key=f"pf_prompt_{suffix}",
            help="Leave empty for the generator's default. Placeholders: {title} {price} {was_price} "
                 "{discount_percent} {rating} {orders_count} {category} {language_name} {tone}.",
        )
        submitted = st.form_submit_button("Save content type" if profile else "Add content type", type="primary")
    if submitted:
        try:
            saved = ContentProfile.model_validate({
                "content_type": profile.content_type if profile else content_type, "platform": platform,
                "generator": generator, "llm_model": llm_model,
                "publish_format": None if publish_format == NO_FORMAT else publish_format,
                "tone": tone, "prompt_template": prompt_template, "enabled": enabled,
            })
            if profile is None and engine.db.get_config("content_profiles", saved.content_type):
                st.error(f"Content type '{saved.content_type}' already exists — edit it above.")
                return
            engine.registry.save_content_profile(saved)
        except ValidationError as exc:
            st.error("The content type is not valid:\n\n"
                     + "\n".join(f"- {line}" for line in describe_validation_error(exc)))
            return
        except TarshishError as exc:
            st.error(str(exc))
            return
        flash(f"Content type '{saved.content_type}' now uses {saved.generator} with {saved.llm_model}.")
        st.rerun()


def render_content_tab(engine: Engine) -> None:
    profiles = engine.registry.list_content_profiles()
    st.subheader("Content types & AI model assignment")
    st.caption("One routing for every category and language: change a model here and every pipeline that "
               "publishes this content type uses it on its next run.")
    html_table(
        [{**p.model_dump(), "enabled": "yes" if p.enabled else "no", "publish_format": p.publish_format or "auto"}
         for p in profiles],
        {"content_type": "Content type", "platform": "Platform", "generator": "Generator", "llm_model": "LLM model",
         "publish_format": "Format", "tone": "Tone", "enabled": "Enabled"},
        clipped=("tone",),
    )
    for profile in profiles:
        with st.expander(f"{profile.content_type} · {profile.generator} · {profile.llm_model}"):
            render_profile_form(engine, profile)
    with st.expander("➕ Add a content type"):
        render_profile_form(engine, None)


# --------------------------------------------------------------------------- #
# Tab 4 — run pipelines & live KPIs
# --------------------------------------------------------------------------- #
def run_pipeline(engine: Engine, category: str, language: str, content_types: list[str], dry_run: bool,
                 mode: str, limit: int) -> None:
    buffer = io.StringIO()
    try:
        with st.spinner("Running the publishing pipeline…"), contextlib.redirect_stdout(buffer):
            report = engine.orchestrator.run(
                category, language, content_types or None, mode=mode, limit=limit, dry_run=dry_run,
            )
        st.session_state["run_result"] = {"report": report, "console": buffer.getvalue()}
    except TarshishError as exc:  # BudgetExceededError included
        st.session_state["run_result"] = {"error": str(exc)}


def render_run_result() -> None:
    result = st.session_state.get("run_result")
    if not result:
        return
    if "error" in result:
        st.error(result["error"])
        return
    report = result["report"]
    direction = "rtl" if report.language.split("-")[0] in RTL_LANGUAGES else "ltr"
    label = "Dry-run result — nothing was published, no AI cost" if report.dry_run else "Run result"
    with st.expander(f"{label} · {report.pipeline_id}", expanded=True):
        if report.error:
            st.error(report.error)
        html_table(
            [{**vars(route), "account": f"{route.publisher} → {route.account}" if route.account else "—",
              "status": "✅ ready" if route.ready else f"⚠️ {route.reason or 'not ready'}"} for route in report.routes],
            {"content_type": "Content type", "generator": "Generator", "llm_model": "Model", "account": "Account",
             "candidates": "Products", "status": "Status"},
            numeric=("candidates",), clipped=("status",),
        )
        st.caption(f"Published {report.count('published')} · queued {report.count('queued')} · previewed "
                   f"{report.count('preview')} · failed {report.count('failed')} · AI cost {money(report.cost_usd, 4)}")
        for warning in report.warnings:
            st.warning(warning, icon="⚠️")
        for item in report.items:
            with st.container(border=True):
                st.markdown(f"**{esc(item.title)}** · `{esc(item.content_type)}`")
                if item.status == "failed":
                    st.error(item.detail)
                    continue
                written = "" if item.model_used == item.configured_model else f" (written by {item.model_used})"
                st.caption(f"{item.target} · model {item.configured_model}{written} · {item.status}"
                           + (f" — {item.detail}" if item.detail and item.status != "preview" else ""))
                tags = " ".join(f"#{tag}" for tag in item.hashtags)
                st.markdown(
                    f'<div class="tsh-caption" dir="{direction}">{esc(item.caption)}<br><br>{esc(tags)}</div>',
                    unsafe_allow_html=True,
                )
        if result.get("console"):
            with st.popover("Console output"):
                st.code(result["console"], language=None)
        if st.button("Clear result", key="clear_run"):
            st.session_state.pop("run_result", None)
            st.rerun()


def render_run_form(engine: Engine) -> None:
    categories = [c.category_id for c in engine.registry.list_categories()]
    profiles = [p.content_type for p in engine.registry.list_content_profiles()]
    if not categories or not profiles:
        st.info("Add a category and a content type first.")
        return
    languages = engine.registry.languages()
    preferred = [name for name in ("instagram_post", "instagram_story") if name in profiles]
    with st.form("run_form", border=True):
        cols = st.columns([1.2, 1, 2.6])
        category = cols[0].selectbox("Category", categories, key="run_category")
        language = cols[1].selectbox("Language", languages, key="run_language",
                                     format_func=lambda code: LANGUAGE_LABELS.get(code, code))
        content_types = cols[2].multiselect("Content types", profiles, default=preferred, key="run_types",
                                            help="Empty = every enabled type that has an account for this pair.")
        cols = st.columns([1, 1.4, 1, 2])
        dry_run = cols[0].toggle("Dry-run", value=True, key="run_dry",
                                 help="On: preview only — no posts, no paid LLM calls, no database writes.")
        mode = cols[1].radio("Mode", ["queued", "immediate"], horizontal=True, key="run_mode")
        limit = cols[2].number_input("Products per type", min_value=1, max_value=50, value=3, key="run_limit")
        cols[3].caption("With dry-run off the content is generated with the paid models and queued or "
                        "published to the real accounts.")
        submitted = st.form_submit_button("▶ Run pipeline", type="primary")
    if submitted:
        run_pipeline(engine, category, language, content_types, dry_run, mode, int(limit))
    render_run_result()


def render_run_tab(engine: Engine, metrics: MetricsService) -> None:
    st.subheader("Run a publishing pipeline")
    st.caption("Stage 2 — pick a category, a language and content types; the products come from the last "
               "harvest, the models from Content Types, the accounts from the matrix.")
    render_run_form(engine)
    st.subheader("Harvested inventory")
    html_table(metrics.get_product_inventory(),
               {"category": "Category", "source": "Source", "products": "Products", "last_harvest": "Last harvest"},
               formats={"last_harvest": lambda value: local_time(engine, value)}, numeric=("products",))
    st.divider()
    kpis, queue = st.tabs([TAB_KPIS, TAB_QUEUE])
    with kpis:
        render_kpis_tab(engine, metrics)
    with queue:
        render_queue_tab(engine)


# Outcome of asking one provider of a RapidAPI chain (Temu, AliExpress), as the chain cards word it.
_PROVIDER_OUTCOMES = {
    "success": "✓ delivered", "missing_key": "✗ no API key", "quota_exhausted": "✗ quota used up (429)",
    "auth_failed": "✗ key rejected (401/403)", "unavailable": "✗ unreachable / 5xx", "empty": "✗ no items",
    "filtered_out": "✗ nothing passed the filter", "job_pending": "… search still running", "error": "✗ error",
}
_TEMU_OUTCOMES = _PROVIDER_OUTCOMES  # the original name


def render_provider_chain_card(engine: Engine, title: str, store: str, status: dict[str, Any], builtin: str) -> None:
    """One source's failover order, which API keys are present, and who answered last."""
    with st.container(border=True):
        st.markdown(f"**{title}**")
        if status["error"]:
            st.error(f"The provider chain is not valid; the {store} source cannot fetch until it is fixed.")
            st.code(redact(status["error"]), language=None)
            return

        last = status["last_run"] or {}
        if not last:
            st.caption("Tried top to bottom; the first provider with usable products wins. No live fetch has run yet.")
        elif last.get("succeeded"):
            st.caption(
                f"Last run {local_time(engine, last.get('finished_at'))}: **{last['succeeded']}** delivered "
                f"{last.get('usable', 0)} usable product(s) for “{last.get('keyword', '')}”."
            )
        else:
            st.caption(
                f"Last run {local_time(engine, last.get('finished_at'))}: no provider delivered products "
                f"for “{last.get('keyword', '')}”."
            )

        rows = []
        for provider in status["providers"]:
            if not provider["enabled"]:
                outcome = "switched off"
            elif provider["last_outcome"]:
                outcome = _PROVIDER_OUTCOMES.get(provider["last_outcome"], provider["last_outcome"])
                if provider["last_outcome"] != "success" and provider["last_detail"]:
                    outcome += f" — {provider['last_detail']}"
            else:
                outcome = "not reached" if last else ""
            rows.append({**provider, "last": outcome})
        html_table(
            rows,
            {"order": "#", "name": "Provider", "adapter": "Adapter", "host": "Host",
             "api_key_env": "API key variable", "key_state": "Key in .env", "last": "Last run"},
            numeric=("order",), clipped=("last",),
        )
        source = status["config_path"] if status["from_file"] else f"built-in default (no {builtin})"
        st.caption(f"Order and endpoints: `{source}`. Keys are read from `.env`; restart the dashboard after changing it.")


def render_temu_provider_chain(engine: Engine) -> None:
    """The Temu source's failover order, which API keys are present, and who answered last."""
    try:
        from plugins.sources.temu_providers import chain_status
    except ImportError:  # the Temu source was removed: nothing to show
        return
    render_provider_chain_card(
        engine, "Temu RapidAPI Provider Chain", "Temu", chain_status(engine.settings), "temu_providers.yaml"
    )


def aliexpress_chain_status(engine: Engine) -> dict[str, Any] | None:
    """The AliExpress chain status, or ``None`` when the AliExpress RapidAPI source is not installed."""
    try:
        from plugins.sources.aliexpress_providers import chain_status
    except ImportError:
        return None
    return chain_status(engine.settings)


def render_aliexpress_provider_chain(engine: Engine, status: dict[str, Any] | None = None) -> None:
    """The AliExpress source's failover order, which API keys are present, and who answered last."""
    status = status or aliexpress_chain_status(engine)
    if status is not None:
        render_provider_chain_card(
            engine, "AliExpress RapidAPI Provider Chain", "AliExpress", status, "aliexpress_providers.yaml"
        )


def render_instagram_connection(engine: Engine) -> None:
    """A read-only check of every Instagram account of the matrix (nothing is posted)."""
    try:
        from plugins.publishers.instagram_pub import pages_to_verify, verify_pages
    except ImportError:  # the Instagram publisher was removed: nothing to show
        return

    with st.container(border=True):
        head = st.columns([3, 1], vertical_alignment="center")
        head[0].markdown("**Instagram API with Unified Login**")
        head[0].caption("Read-only: checks the token, the professional account and the publishing permissions.")
        if head[1].button("Test Instagram Connection", key="verify_instagram", width="stretch"):
            with st.spinner("Asking graph.facebook.com…"):
                st.session_state["instagram_check"] = [
                    {"ok": r.ok, "page": r.page_name, "summary": r.summary(), "lines": r.lines()}
                    for r in verify_pages(engine.registry, pages_to_verify(engine.registry))
                ]
        for result in st.session_state.get("instagram_check", []):
            show = st.success if result["ok"] else st.error
            show(f"{result['page']}: {redact(result['summary'])}")
            st.code(redact("\n".join(result["lines"])), language=None)


# --------------------------------------------------------------------------- #
# Tab 2 — KPIs & budget
# --------------------------------------------------------------------------- #
def budget_meter(budget: dict[str, Any]) -> None:
    status = STATUS[budget["state"]]
    fill = status["color"] or SERIES_BLUE[theme_mode()]
    percent = min(max(budget["ratio"], 0.0), 1.0) * 100
    st.markdown(
        f"""
        <div role="group" aria-label="Monthly AI spend against budget">
          <div class="tsh-meter-head">
            <span><span class="tsh-meter-value">{money(budget['spent_usd'], 4)}</span>
              &nbsp;of {money(budget['limit_usd'])} · {budget['ratio']:.0%} used
              · {money(budget['remaining_usd'], 4)} left</span>
            <span>{status['icon']} {esc(status['label'])}</span>
          </div>
          <div class="tsh-meter-track" style="background: color-mix(in srgb, {fill} 22%, transparent);"
               role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{percent:.0f}">
            <div class="tsh-meter-fill" style="width: {percent:.2f}%; background: {fill};"></div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def ranked_bars(
    rows: list[dict[str, Any]], category: str, measure: str, title: str, fmt: Callable[[float], str]
) -> None:
    """Horizontal bars, largest first, one hue, value at the tip — or a note when there is nothing to plot."""
    values = sorted(((str(row[category]), float(row.get(measure) or 0)) for row in rows), key=lambda item: -item[1])
    top = max((value for _, value in values), default=0.0)
    if top <= 0:
        st.markdown(f'<div class="tsh-bars"><div class="tsh-bars-title">{esc(title)}</div></div>', unsafe_allow_html=True)
        st.caption("No data yet.")
        return
    color = SERIES_BLUE[theme_mode()]
    parts = [f'<div class="tsh-bars" role="img" aria-label="{esc(title)}"><div class="tsh-bars-title">{esc(title)}</div>']
    for name, value in values:
        share = max(value, 0.0) / top
        label = fmt(value)
        parts.append(
            f'<div class="tsh-bar-row" title="{esc(name)}: {esc(label)}">'
            f'<span class="tsh-bar-name">{esc(name)}</span>'
            f'<span class="tsh-bar-area">'
            # The bar scales inside the row minus the room reserved for its value label.
            f'<span class="tsh-bar-fill" style="width: calc((100% - 5.5em) * {share:.4f}); background: {color};"></span>'
            f'<span class="tsh-bar-value">{esc(label)}</span></span></div>'
        )
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def breakdown(title: str, rows: list[dict[str, Any]], category: str, label: str, note: str) -> None:
    st.subheader(title)
    if not rows:
        st.info("Nothing has been generated yet.")
        return
    charts = st.columns(3)
    with charts[0]:
        ranked_bars(rows, category, "published", "Published posts", whole)
    with charts[1]:
        ranked_bars(rows, category, "clicks", "Clicks", whole)
    with charts[2]:
        ranked_bars(rows, category, "revenue_usd", "Revenue (USD)", money)
    html_table(
        rows,
        {
            category: label, "published": "Published", "queued": "Queued", "failed": "Failed", "views": "Views",
            "clicks": "Clicks", "conversions": "Conversions", "revenue_usd": "Revenue", "ai_spend_usd": "AI spend",
            "net_profit_usd": "Net profit",
        },
        formats={
            "published": whole, "queued": whole, "failed": whole, "views": whole, "clicks": whole,
            "conversions": whole, "revenue_usd": money, "ai_spend_usd": lambda value: money(value, 4),
            "net_profit_usd": money,
        },
        numeric=("published", "queued", "failed", "views", "clicks", "conversions", "revenue_usd", "ai_spend_usd",
                 "net_profit_usd"),
    )
    st.caption(note)


def render_metrics_entry(engine: Engine) -> None:
    published = engine.db.list_publications(statuses=["PUBLISHED"], limit=200)
    with st.expander("Record a metrics snapshot (manual entry)"):
        st.caption(
            "Until a platform metrics collector is plugged in, enter the cumulative numbers reported by the "
            "platform / affiliate network. The latest snapshot of each publication is the one that counts."
        )
        if not published:
            st.info("Nothing has been published yet.")
            return
        labels = {
            f"#{row['id']} · {row['platform']} → {row['target_page']} · {row['product_title'][:60]}": row["id"]
            for row in published
        }
        with st.form("metrics_snapshot", border=False):
            choice = st.selectbox("Publication", list(labels))
            c1, c2, c3, c4 = st.columns(4)
            views = c1.number_input("Views", min_value=0, step=100)
            clicks = c2.number_input("Clicks", min_value=0, step=10)
            conversions = c3.number_input("Conversions", min_value=0, step=1)
            revenue = c4.number_input("Revenue (USD)", min_value=0.0, step=1.0, format="%.2f")
            if st.form_submit_button("Save snapshot"):
                engine.db.record_metrics_snapshot(
                    labels[choice], views=int(views), clicks=int(clicks), conversions=int(conversions),
                    revenue_usd=float(revenue),
                )
                flash("Metrics snapshot saved.")
                st.rerun()


def render_storefront_clicks(engine: Engine, metrics: MetricsService) -> None:
    """Outbound clicks on the public storefront, read from ``click_events`` on every redraw.

    No auto-refreshing fragment on purpose: ``st.fragment(run_every=...)`` imports numpy, one of the
    native libraries Windows Smart App Control blocks. The button below redraws the page instead.
    """
    summary = metrics.get_storefront_click_summary()
    head = st.columns([5, 1.2], vertical_alignment="bottom")
    head[0].subheader("Storefront click-throughs")
    head[1].button("Refresh clicks", key="refresh_clicks", width="stretch")
    last = local_time(engine, summary["last_click_at"])
    st.caption(
        "Confirmed outbound clicks on the FindHub storefront (shopper pressed “Continue to Store”), "
        "counted live from the database. " + (f"Last click: {last}." if last else "No clicks recorded yet.")
    )
    tiles = st.columns(4)
    tiles[0].metric("Storefront clicks", compact(summary["total_clicks"]), border=True, help="All time")
    tiles[1].metric("Last 7 days", compact(summary["clicks_7d"]), border=True)
    tiles[2].metric("Last 24 hours", compact(summary["clicks_24h"]), border=True)
    tiles[3].metric("Products clicked", compact(summary["products_clicked"]), border=True,
                    help="Distinct products with at least one outbound click")

    by_category = metrics.get_storefront_clicks_by_category()
    by_source = metrics.get_storefront_clicks_by_source()
    columns = {"clicks": "Clicks", "clicks_7d": "Last 7 days", "clicks_24h": "Last 24 hours",
               "products_clicked": "Products clicked", "last_click_at": "Last click"}
    formats = {"clicks": whole, "clicks_7d": whole, "clicks_24h": whole, "products_clicked": whole,
               "last_click_at": lambda value: local_time(engine, value)}
    numeric = ("clicks", "clicks_7d", "clicks_24h", "products_clicked")
    left, right = st.columns(2)
    with left:
        ranked_bars(by_category, "category", "clicks", "Clicks by category", whole)
        html_table(by_category, {"category": "Category", **columns}, formats=formats, numeric=numeric)
    with right:
        ranked_bars(by_source, "source", "clicks", "Clicks by source", whole)
        html_table(by_source, {"source": "Source", **columns}, formats=formats, numeric=numeric)


def render_kpis_tab(engine: Engine, metrics: MetricsService) -> None:
    summary = metrics.get_overall_summary()
    budget = metrics.get_budget_status()

    st.subheader("Performance")
    tiles = st.columns(6)
    tiles[0].metric("Total views", compact(summary["total_views"]), border=True)
    tiles[1].metric("Clicks", compact(summary["total_clicks"]), border=True, help=f"CTR {summary['ctr']:.1%}")
    tiles[2].metric(
        "Conversions", compact(summary["total_conversions"]), border=True,
        help=f"Conversion rate {summary['conversion_rate']:.1%} of clicks",
    )
    tiles[3].metric("Revenue (USD)", money(summary["revenue_usd"]), border=True)
    tiles[4].metric("AI spend (USD)", money(summary["ai_spend_usd"], 4), border=True, help="All-time, from the AI usage ledger")
    tiles[5].metric("Net profit (USD)", money(summary["net_profit_usd"]), border=True, help="Revenue minus AI spend")

    st.subheader("Monthly AI spend vs. MAX_MONTHLY_AI_BUDGET_USD")
    budget_meter(budget)
    st.caption("When the cap is reached the budget guardrail raises and halts every pipeline that needs a paid LLM.")

    st.subheader("Output")
    output = st.columns(4)
    output[0].metric("Text posts published", compact(summary["total_text"]), border=True)
    output[1].metric("Videos published", compact(summary["total_videos"]), border=True)
    output[2].metric("In queue", compact(summary["queued"]), border=True)
    output[3].metric("Failed", compact(summary["failed"]), border=True)

    breakdown(
        "By platform", metrics.get_metrics_by_platform(), "platform", "Platform",
        "AI spend per platform is the generation cost attributed to its publications.",
    )
    breakdown(
        "By content type", metrics.get_metrics_by_content_type(), "content_format", "Content type",
        "AI spend per content type is the generation cost attributed to its publications.",
    )
    breakdown(
        "By pipeline (category × language)", metrics.get_metrics_by_pipeline(), "pipeline_id", "Pipeline",
        "AI spend per pipeline comes from the AI usage ledger (includes content that was later flushed).",
    )
    render_storefront_clicks(engine, metrics)
    render_metrics_entry(engine)


# --------------------------------------------------------------------------- #
# Tab 3 — queue & logs
# --------------------------------------------------------------------------- #
def publications_table(engine: Engine, rows: list[dict[str, Any]], columns: dict[str, str]) -> None:
    as_local_time = lambda value: local_time(engine, value)  # noqa: E731
    html_table(
        rows,
        columns,
        formats={
            "scheduled_for": as_local_time, "published_at": as_local_time, "created_at": as_local_time,
            "generation_cost_usd": lambda value: money(value, 4),
        },
        numeric=("id", "attempts", "generation_cost_usd"),
        clipped=("product_title", "caption", "error_message"),
    )


def read_log_tail(path: Path | None, lines: int) -> str:
    if path is None or not path.is_file():
        return ""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        tail = handle.readlines()[-lines:]
    return redact("".join(tail))  # the file is already redacted; this is defence in depth


def render_queue_tab(engine: Engine) -> None:
    counts = engine.db.get_status_counts()
    st.subheader("Publications")
    tiles = st.columns(4)
    tiles[0].metric("Queued", counts["QUEUED"], border=True)
    tiles[1].metric("Pending", counts["PENDING"], border=True, help="Delivery in flight")
    tiles[2].metric("Published", counts["PUBLISHED"], border=True)
    tiles[3].metric("Failed", counts["FAILED"], border=True)

    queued = engine.db.list_publications(statuses=["QUEUED", "PENDING"])
    published = engine.db.list_publications(statuses=["PUBLISHED"])
    failed = engine.db.list_publications(statuses=["FAILED"])

    actions = st.columns([1.3, 1.6, 1.3, 3])
    if actions[0].button("Retry all failed", disabled=not failed, width="stretch"):
        flash(f"Re-queued {engine.db.retry_failed()} failed publication(s). The worker will publish them.")
        st.rerun()
    if actions[1].button("Run a worker pass now", disabled=not queued, width="stretch",
                         help="Publishes what is due, honouring active hours and pacing."):
        with st.spinner("Publishing due items…"):
            report = engine.create_worker().run_once()
        note = " Outside active hours — nothing was sent." if report.outside_active_hours else ""
        flash(f"Worker pass: published {report.published}, failed {report.failed}, deferred {report.deferred}.{note}", "info")
        st.rerun()
    with actions[2].popover("Flush queue", disabled=not queued, width="stretch"):
        st.write(f"Delete **{len(queued)}** pending/queued publication(s)? AI spend already incurred stays on record.")
        if st.button("Yes, flush the queue", type="primary"):
            flash(f"Flushed {engine.db.flush_queue()} publication(s) from the queue.")
            st.rerun()

    tab_queued, tab_published, tab_failed = st.tabs(
        [f"Queued ({len(queued)})", f"Published ({len(published)})", f"Failed ({len(failed)})"]
    )
    with tab_queued:
        publications_table(engine, queued, {
            "id": "ID", "pipeline_id": "Pipeline", "content_format": "Content type", "platform": "Platform",
            "target_page": "Page", "product_title": "Product", "status": "Status", "scheduled_for": "Scheduled for",
            "attempts": "Attempts", "caption": "Caption",
        })
    with tab_published:
        publications_table(engine, published, {
            "id": "ID", "pipeline_id": "Pipeline", "content_format": "Content type", "platform": "Platform",
            "target_page": "Page", "product_title": "Product", "published_at": "Published at", "platform_post_id": "Post ID",
            "model_used": "Model", "generation_cost_usd": "AI cost (USD)", "caption": "Caption",
        })
    with tab_failed:
        publications_table(engine, failed, {
            "id": "ID", "pipeline_id": "Pipeline", "content_format": "Content type", "platform": "Platform",
            "target_page": "Page", "product_title": "Product", "error_message": "Error", "attempts": "Attempts", "created_at": "Created at",
        })
        if failed:
            chosen = st.multiselect(
                "Retry selected", [row["id"] for row in failed], format_func=lambda value: f"#{value}",
                placeholder="Choose failed publications to re-queue",
            )
            if st.button("Retry selected", disabled=not chosen):
                flash(f"Re-queued {engine.db.retry_failed(publication_ids=chosen)} publication(s).")
                st.rerun()

    st.subheader("Logs")
    log_file = engine.settings.log_file
    controls = st.columns([1.2, 2.4, 3])
    line_count = controls[0].selectbox("Lines", [100, 250, 500, 1000], index=0)
    levels = controls[1].multiselect("Levels", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], default=[])
    controls[2].caption(f"`{log_file}` — secrets are masked as [REDACTED] before they are written.")
    text = read_log_tail(log_file, int(line_count))
    if levels:
        text = "\n".join(line for line in text.splitlines() if any(f"| {level}" in line for level in levels))
    if text.strip():
        st.code(text, language="log", height=380)
    else:
        st.caption("No log lines to show.")
    st.button("Refresh", key="refresh_logs")


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
def require_login() -> None:
    """Stop the page unless the visitor knows ``DASHBOARD_PASSWORD``.

    The dashboard edits the configuration and starts paid, public publishing runs, so a
    deployment reachable from the internet must be locked. With ``DATABASE_URL`` set (a
    production database) the page refuses to open until a password is configured; a purely
    local SQLite setup may run without one.
    """
    from dotenv import load_dotenv

    from core.config import default_env_file

    env_file = default_env_file()
    if env_file.is_file():
        load_dotenv(env_file, override=False, encoding="utf-8")
    password = (os.environ.get("DASHBOARD_PASSWORD") or "").strip()
    if not password:
        if (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or "").strip():
            st.error("Set DASHBOARD_PASSWORD before opening the dashboard on a production database.")
            st.caption("Streamlit Community Cloud: app ⋮ > Settings > Secrets, add  DASHBOARD_PASSWORD = \"…\"")
            st.stop()
        return
    if st.session_state.get("_authenticated"):
        return
    st.title("⛵ Tarshish Engine")
    with st.form("login"):
        attempt = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        if hmac.compare_digest(attempt.encode("utf-8"), password.encode("utf-8")):
            st.session_state["_authenticated"] = True
            st.rerun()
        st.error("Wrong password.")
    st.stop()


def main() -> None:
    require_login()
    try:
        engine = get_engine()
    except ConfigError as exc:
        st.error("Configuration error — the engine did not start.")
        st.code(str(exc), language=None)
        st.stop()

    metrics = MetricsService(engine.db, engine.settings)
    st.title("⛵ Tarshish Engine")
    st.caption("Two-stage affiliate engine — harvest products per category, publish per category × language")
    show_flash()

    labels = [TAB_CATEGORIES, TAB_ACCOUNTS, TAB_CONTENT, TAB_RUN]
    default = _TAB_SLUGS.get(str(st.query_params.get("tab", "")).lower(), TAB_CATEGORIES)  # deep link: ?tab=run
    tab_categories, tab_accounts, tab_content, tab_run = st.tabs(labels, default=default)
    with tab_categories:
        render_categories_tab(engine)
    with tab_accounts:
        render_accounts_tab(engine)
    with tab_content:
        render_content_tab(engine)
    with tab_run:
        render_run_tab(engine, metrics)


main()
