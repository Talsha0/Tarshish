"""Tarshish Engine — admin & analytics dashboard.

Run from the project root:

    python -m streamlit run dashboard/app.py

Tabs
----
1. Pipelines   — visual wiring (source ➔ generator + model ➔ social pages), create / edit, enable, dry-run
2. KPIs        — reach, revenue, net profit, the monthly AI budget guardrail and live
                 storefront click-throughs per category and source
3. Queue/Logs  — queued / published / failed publications, retry, flush, log tail

The page talks to the same engine objects as the CLI (``core`` + ``MetricsService``),
and pipelines are read from SQLite on every rerun, so edits apply immediately.

Only Streamlit's basic widgets are used, with small HTML snippets for tables and bars.
Streamlit's dataframe, data-editor and chart elements all load the native Apache Arrow
library, which Windows Smart App Control can refuse to load; avoiding them keeps the
dashboard working on locked-down machines.
"""

from __future__ import annotations

import contextlib
import html
import io
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
from core.config import PipelineConfig, SocialPage, describe_validation_error  # noqa: E402
from core.resilience import ConfigError, TarshishError, redact  # noqa: E402
from dashboard.metrics_service import MetricsService  # noqa: E402

TAB_PIPELINES, TAB_KPIS, TAB_QUEUE = "Pipelines", "KPIs & Budget", "Queue & Logs"
_TAB_SLUGS = {"pipelines": TAB_PIPELINES, "kpis": TAB_KPIS, "queue": TAB_QUEUE}
NEW_PIPELINE = "➕ Create a new pipeline"
SELECT_NEXT = "_select_pipeline"  # pipeline to show in the form on the next draw (see render_pipeline_form)
NO_PLATFORM = "— none —"          # a social-page row with this platform is ignored / removed on save
LANGUAGES = ["he", "en", "ar", "ru", "es", "fr", "de"]
RTL_LANGUAGES = {"he", "ar", "fa", "ur"}
_STANDARD_PAGE_KEYS = {"platform", "page_name", "token_env_var", "chat_id_env_var"}

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
# Tab 1 — pipelines
# --------------------------------------------------------------------------- #
def flow_html(engine: Engine, pipeline: PipelineConfig) -> str:
    """[Input: source] ➔ [Generate: generator + LLM model] ➔ [Output: publishers & social pages]."""
    pages = []
    for page in pipeline.social_pages:
        _, reason = engine.registry.resolve_target(page)
        if reason is None:
            status = "✅ ready"
        else:
            status = f"⚠️ skipped — {esc(reason)}"
        env_names = ", ".join(sorted(page.env_refs().values()))
        env_note = f"<br><small>env: {esc(env_names)}</small>" if env_names else ""
        pages.append(
            f'<div class="tsh-page"><b>{esc(page.platform)}</b> → {esc(page.page_name)} · {status}{env_note}</div>'
        )
    filters = pipeline.filters
    return f"""
    <div class="tsh-flow">
      <div class="tsh-node">
        <div class="tsh-kicker">Input · source plugin</div>
        <div class="tsh-main">{esc(pipeline.source)}</div>
        <div class="tsh-sub">niche {esc(pipeline.niche)}<br>rating ≥ {filters.min_rating:g} · orders ≥ {filters.min_orders:,}
          · up to {pipeline.limit} per run</div>
      </div>
      <div class="tsh-arrow">➔</div>
      <div class="tsh-node">
        <div class="tsh-kicker">Generate · generator + LLM model</div>
        <div class="tsh-main">{esc(pipeline.generator)}</div>
        <div class="tsh-sub">model {esc(pipeline.llm_model)}<br>language {esc(pipeline.language)}
          · {"custom prompt" if pipeline.prompt_template else "default prompt"}</div>
      </div>
      <div class="tsh-arrow">➔</div>
      <div class="tsh-node">
        <div class="tsh-kicker">Output · publishers &amp; social pages · {esc(pipeline.publishing_mode)}</div>
        {''.join(pages)}
      </div>
    </div>
    """


def run_dry_run(engine: Engine, pipeline_id: str) -> None:
    buffer = io.StringIO()
    try:
        with st.spinner("Simulating the pipeline…"), contextlib.redirect_stdout(buffer):
            report = engine.orchestrator.run(pipeline_id, dry_run=True, force=True)
        st.session_state[f"dryrun_{pipeline_id}"] = {"report": report, "console": buffer.getvalue()}
    except TarshishError as exc:
        st.session_state[f"dryrun_{pipeline_id}"] = {"error": str(exc)}


def render_dry_run_result(pipeline: PipelineConfig) -> None:
    pipeline_id = pipeline.id
    direction = "rtl" if pipeline.language.split("-")[0] in RTL_LANGUAGES else "ltr"
    result = st.session_state.get(f"dryrun_{pipeline_id}")
    if not result:
        return
    with st.expander("Dry-run result — nothing was published, no AI cost", expanded=True):
        if "error" in result:
            st.error(result["error"])
            return
        report = result["report"]
        if report.error:
            st.error(report.error)
        st.caption(
            f"Fetched {report.fetched} product(s) · previewed {report.count('preview')} · "
            f"failed {report.count('failed', 'partial')}"
        )
        for warning in report.warnings:
            st.warning(warning, icon="⚠️")
        for item in report.items:
            with st.container(border=True):
                st.markdown(f"**{esc(item.title)}**")
                if item.status == "failed":
                    st.error(item.detail)
                    continue
                targets = " · ".join(
                    f"{'✅' if t.status == 'preview' else '⚠️'} {t.platform} → {t.page_name}" for t in item.targets
                )
                st.caption(f"Model: {item.model_used} · would go to: {targets}")
                if item.detail:
                    st.caption(f"ℹ️ {item.detail}")
                tags = " ".join(f"#{tag}" for tag in item.hashtags)
                st.markdown(
                    f'<div class="tsh-caption" dir="{direction}">{esc(item.caption)}<br><br>{esc(tags)}</div>',
                    unsafe_allow_html=True,
                )
        if result.get("console"):
            with st.popover("Console output"):
                st.code(result["console"], language=None)
        if st.button("Clear result", key=f"clear_{pipeline_id}"):
            st.session_state.pop(f"dryrun_{pipeline_id}", None)
            st.rerun()


def render_pipeline_card(engine: Engine, pipeline: PipelineConfig) -> None:
    with st.container(border=True):
        head = st.columns([5, 1.4, 2.6, 1], vertical_alignment="center")
        head[0].markdown(f"#### {esc(pipeline.id)}")
        enabled = head[1].toggle("Enabled", value=pipeline.enabled, key=f"enabled_{pipeline.id}")
        if enabled != pipeline.enabled:
            engine.registry.set_enabled(pipeline.id, enabled)
            st.rerun()
        if head[2].button("▶ Run Pipeline Now (Dry-Run)", key=f"run_{pipeline.id}", width="stretch"):
            run_dry_run(engine, pipeline.id)
        if head[3].button("Edit", key=f"edit_{pipeline.id}", width="stretch"):
            st.session_state[SELECT_NEXT] = pipeline.id
        st.markdown(flow_html(engine, pipeline), unsafe_allow_html=True)
        render_dry_run_result(pipeline)


def social_page_inputs(existing: list[SocialPage], publishers: list[str], row_count: int, suffix: str) -> list[dict[str, Any]]:
    """One row of plain widgets per target page. Returns the raw values of every row."""
    rows: list[dict[str, Any]] = []
    for index in range(row_count):
        page = existing[index] if index < len(existing) else None
        options = [NO_PLATFORM, *publishers]
        if page and page.platform not in options:
            options.append(page.platform)  # a plugin that is no longer installed: keep it visible
        visibility = "visible" if index == 0 else "collapsed"
        cols = st.columns([1.25, 1.25, 1.5, 1.5])
        rows.append({
            "platform": cols[0].selectbox(
                "Platform (publisher plugin)", options, index=options.index(page.platform) if page else 0,
                key=f"f_pp_{suffix}_{index}", label_visibility=visibility,
            ),
            "page_name": cols[1].text_input(
                "Page / channel", value=page.page_name if page else "", placeholder="e.g. @my_channel",
                key=f"f_pn_{suffix}_{index}", label_visibility=visibility,
            ),
            "token_env_var": cols[2].text_input(
                "Token env var", value=(page.token_env_var or "") if page else "", placeholder="e.g. TG_SPORTS_BOT_TOKEN",
                key=f"f_pt_{suffix}_{index}", label_visibility=visibility,
            ),
            "chat_id_env_var": cols[3].text_input(
                "Chat / page ID env var", value=(page.chat_id_env_var or "") if page else "",
                placeholder="e.g. TG_SPORTS_CHAT_ID", key=f"f_pc_{suffix}_{index}", label_visibility=visibility,
            ),
        })
    return rows


def render_pipeline_form(engine: Engine, pipelines: list[PipelineConfig]) -> None:
    st.subheader("Create / edit pipeline")
    by_id = {pipeline.id: pipeline for pipeline in pipelines}
    requested = st.session_state.pop(SELECT_NEXT, None)
    if requested is not None:  # set by "Edit", after a save, or after a delete
        st.session_state["form_pipeline"] = requested
    if st.session_state.get("form_pipeline") not in (None, NEW_PIPELINE, *by_id):
        st.session_state["form_pipeline"] = NEW_PIPELINE
    selected = st.selectbox("Pipeline", [NEW_PIPELINE, *by_id], key="form_pipeline")
    current = by_id.get(selected)
    suffix = current.id if current else "new"

    plugins = engine.plugins
    sources, generators, publishers = plugins.names("source"), plugins.names("generator"), plugins.names("publisher")
    models = list(dict.fromkeys(m for name in generators for m in plugins.get("generator", name).supported_models()))
    if current and current.llm_model not in models:
        models.append(current.llm_model)
    languages = LANGUAGES + ([current.language] if current and current.language not in LANGUAGES else [])
    existing_pages = list(current.social_pages) if current else [SocialPage(platform="console_pub")]

    # Outside the form on purpose: changing it must redraw the form with more (or fewer) rows.
    row_count = int(st.number_input(
        "Number of target social pages", min_value=1, max_value=12, value=len(existing_pages), key=f"f_rows_{suffix}",
        help="Raise it to add a page. To remove a page, set its platform to “— none —” and save.",
    ))

    def index_of(options: list[str], value: str | None) -> int:
        return options.index(value) if value in options else 0

    with st.form(f"pipeline_form_{suffix}", border=True):
        left, right = st.columns(2)
        pipeline_id = left.text_input(
            "Pipeline ID", value=current.id if current else "", disabled=current is not None,
            placeholder="e.g. gadgets_en", help="Lowercase letters, digits, '_' or '-'. Cannot be changed later.",
            key=f"f_id_{suffix}",
        )
        niche = right.text_input(
            "Type / niche", value=current.niche if current else "", placeholder="e.g. running_and_sports",
            key=f"f_niche_{suffix}",
        )
        col_a, col_b, col_c = st.columns(3)
        language = col_a.selectbox(
            "Language", languages, index=index_of(languages, current.language if current else "he"),
            accept_new_options=True, key=f"f_lang_{suffix}",
        )
        source = col_b.selectbox(
            "Source plugin", sources, index=index_of(sources, current.source if current else None), key=f"f_src_{suffix}"
        )
        generator = col_c.selectbox(
            "Generator plugin", generators, index=index_of(generators, current.generator if current else None),
            key=f"f_gen_{suffix}",
        )
        col_d, col_e, col_f = st.columns(3)
        llm_model = col_d.selectbox(
            "LLM model", models, index=index_of(models, current.llm_model if current else "mock"),
            accept_new_options=True, key=f"f_model_{suffix}",
            help="A model id, a provider alias (anthropic / gemini) or 'mock' for free offline templates. "
                 "Type to enter any other model id.",
        )
        mode = col_e.radio(
            "Publishing mode", ["immediate", "queued"], horizontal=True,
            index=index_of(["immediate", "queued"], current.publishing_mode if current else None),
            key=f"f_mode_{suffix}",
        )
        limit = col_f.number_input(
            "Products per run", min_value=1, max_value=100, value=current.limit if current else 5, key=f"f_limit_{suffix}"
        )
        col_g, col_h, col_i = st.columns(3)
        min_rating = col_g.number_input(
            "Minimum rating", min_value=0.0, max_value=5.0, step=0.1, format="%.1f",
            value=float(current.filters.min_rating) if current else 4.5, key=f"f_rating_{suffix}",
        )
        min_orders = col_h.number_input(
            "Minimum orders", min_value=0, step=50, value=current.filters.min_orders if current else 200,
            key=f"f_orders_{suffix}",
        )
        enabled = col_i.checkbox("Enabled", value=current.enabled if current else True, key=f"f_enabled_{suffix}")

        st.markdown("**Target social pages**")
        st.caption(
            "One row per page or channel. The last two fields hold the **names** of the `.env` variables that "
            "contain the credentials — never the token itself. Leave them empty for publishers that need none "
            "(console) or to use the publisher's default variables."
        )
        page_rows = social_page_inputs(existing_pages, publishers, row_count, suffix)

        prompt_template = st.text_area(
            "Custom prompt template (optional)", value=(current.prompt_template or "") if current else "",
            height=110, key=f"f_prompt_{suffix}",
            help="Leave empty for the generator's default. Placeholders: {title} {price} {was_price} "
                 "{discount_percent} {rating} {orders_count} {niche} {language_name}.",
        )
        submitted = st.form_submit_button("Save pipeline", type="primary")

    if submitted:
        save_pipeline(
            engine, current,
            {
                "id": current.id if current else pipeline_id.strip(),
                "enabled": enabled,
                "niche": niche,
                "language": language,
                "source": source,
                "generator": generator,
                "llm_model": llm_model,
                "publishing_mode": mode,
                "limit": int(limit),
                "filters": {"min_rating": float(min_rating), "min_orders": int(min_orders)},
                "prompt_template": prompt_template.strip() or None,
                "options": dict(current.options) if current else {},
            },
            page_rows,
            existing_pages if current else [],
        )

    if current is not None:
        with st.popover("Delete this pipeline"):
            st.write(f"Delete **{current.id}**? Pipelines with publication history can only be disabled.")
            if st.button("Yes, delete", key=f"delete_{current.id}", type="primary"):
                try:
                    engine.registry.delete_pipeline(current.id)
                    st.session_state[SELECT_NEXT] = NEW_PIPELINE
                    flash(f"Pipeline '{current.id}' deleted.")
                    st.rerun()
                except TarshishError as exc:
                    st.error(str(exc))


def save_pipeline(
    engine: Engine,
    current: PipelineConfig | None,
    payload: dict[str, Any],
    page_rows: list[dict[str, Any]],
    existing_pages: list[SocialPage],
) -> None:
    social_pages = []
    for index, row in enumerate(page_rows):
        platform = (row.get("platform") or "").strip()
        if not platform or platform == NO_PLATFORM:
            continue
        page: dict[str, Any] = {}
        # A row that still points at the same platform keeps that page's extra keys
        # (publisher options such as parse_mode, or additional <name>_env_var references).
        if index < len(existing_pages) and existing_pages[index].platform == platform:
            stored = existing_pages[index].model_dump(exclude_none=True)
            page.update({key: value for key, value in stored.items() if key not in _STANDARD_PAGE_KEYS})
        page["platform"] = platform
        page["page_name"] = (row.get("page_name") or "").strip() or "default"
        for key in ("token_env_var", "chat_id_env_var"):
            value = (row.get(key) or "").strip()
            if value:
                page[key] = value
        social_pages.append(page)
    payload["social_pages"] = social_pages

    if current is None and engine.db.get_pipeline(payload["id"]) is not None:
        st.error(f"A pipeline with id '{payload['id']}' already exists — select it above to edit it.")
        return
    try:
        pipeline = PipelineConfig.model_validate(payload)
        engine.registry.save_pipeline(pipeline)
    except ValidationError as exc:
        st.error("The pipeline is not valid:\n\n" + "\n".join(f"- {line}" for line in describe_validation_error(exc)))
        return
    except TarshishError as exc:
        st.error(str(exc))
        return
    st.session_state[SELECT_NEXT] = pipeline.id
    flash(f"Pipeline '{pipeline.id}' saved — it is live immediately.")
    st.rerun()


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
    """A read-only check of every Instagram account the pipelines publish to (nothing is posted)."""
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


def render_pipelines_tab(engine: Engine) -> None:
    pipelines = engine.registry.list_pipelines()
    st.subheader("Pipeline architecture")
    st.caption("Each card shows the exact wiring: input ➔ generate ➔ output. Changes apply without a restart.")
    if not pipelines:
        st.info("No pipelines yet — create the first one below.")
    for pipeline in pipelines:
        render_pipeline_card(engine, pipeline)
    if any(pipeline.source == "temu_rapidapi" for pipeline in pipelines):
        render_temu_provider_chain(engine)
    # Shown next to the Temu card, when a pipeline uses it, or once it has fetched (--seed-aliexpress).
    ali_status = aliexpress_chain_status(engine)
    if ali_status is not None and (
        ali_status["last_run"]
        or any(pipeline.source in ("aliexpress_rapidapi", "temu_rapidapi") for pipeline in pipelines)
    ):
        render_aliexpress_provider_chain(engine, ali_status)
    if any(page.platform == "instagram_pub" for pipeline in pipelines for page in pipeline.social_pages):
        render_instagram_connection(engine)
    st.divider()
    render_pipeline_form(engine, pipelines)


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
        "By pipeline", metrics.get_metrics_by_pipeline(), "pipeline_id", "Pipeline",
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


def read_log_tail(path: Path, lines: int) -> str:
    if not path.is_file():
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
            "id": "ID", "pipeline_id": "Pipeline", "platform": "Platform", "target_page": "Page",
            "product_title": "Product", "status": "Status", "scheduled_for": "Scheduled for",
            "attempts": "Attempts", "caption": "Caption",
        })
    with tab_published:
        publications_table(engine, published, {
            "id": "ID", "pipeline_id": "Pipeline", "platform": "Platform", "target_page": "Page",
            "product_title": "Product", "published_at": "Published at", "platform_post_id": "Post ID",
            "model_used": "Model", "generation_cost_usd": "AI cost (USD)", "caption": "Caption",
        })
    with tab_failed:
        publications_table(engine, failed, {
            "id": "ID", "pipeline_id": "Pipeline", "platform": "Platform", "target_page": "Page",
            "product_title": "Product", "error_message": "Error", "attempts": "Attempts", "created_at": "Created at",
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
def main() -> None:
    try:
        engine = get_engine()
    except ConfigError as exc:
        st.error("Configuration error — the engine did not start.")
        st.code(str(exc), language=None)
        st.stop()

    metrics = MetricsService(engine.db, engine.settings)
    st.title("⛵ Tarshish Engine")
    st.caption("Automated affiliate content pipelines — admin & analytics")
    show_flash()

    labels = [TAB_PIPELINES, TAB_KPIS, TAB_QUEUE]
    default = _TAB_SLUGS.get(str(st.query_params.get("tab", "")).lower(), TAB_PIPELINES)  # deep link: ?tab=kpis
    tab_pipelines, tab_kpis, tab_queue = st.tabs(labels, default=default)
    with tab_pipelines:
        render_pipelines_tab(engine)
    with tab_kpis:
        render_kpis_tab(engine, metrics)
    with tab_queue:
        render_queue_tab(engine)


main()
