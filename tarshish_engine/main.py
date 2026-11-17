#!/usr/bin/env python
"""Tarshish Engine command line — a two-stage, decoupled affiliate engine.

Stage 1 · harvest (language-agnostic, per category × source, no LLM)
    python main.py --harvest                                   # every enabled category, every store it has rules for
    python main.py --harvest --category gadgets                # one category
    python main.py --harvest --source temu_rapidapi            # one store, every category
    python main.py --harvest --source temu_mock --category gadgets   # offline catalogue, no API quota

Stage 2 · publish (per category × language × content types, reads harvested products)
    python main.py --publish --category gadgets --lang he --content-types instagram_post,instagram_story
    python main.py --publish --category gadgets --lang he --content-types youtube_short,tiktok_video
    python main.py --publish --category gadgets --lang en --dry-run    # simulate: no posts, no paid LLM, no writes
    python main.py --publish --category gadgets                        # every language with an enabled account
    python main.py --mode worker                                       # publish the queue with human-like pacing

Management
    python main.py --list-config        # categories, content-type routing and the social-accounts matrix
    python main.py --sync-config        # push config/*.yaml into the database (overwrites dashboard edits)
    python main.py --stats              # queue size, published count and monthly AI spend
    python main.py --flush-queue        # drop pending/queued items   (--category/--lang narrow it)
    python main.py --retry-failed       # re-queue failed items        (--category/--lang narrow it)
    python main.py --seed-storefront    # load the storefront demo catalogue
    python main.py --seed-temu --category gadgets --keyword "smart watch" --limit 10   # ad-hoc keyword harvest
    python main.py --verify-instagram [--category gadgets --lang he]   # read-only Instagram connection check
    python main.py --verify-youtube [--category gadgets --lang he]     # OAuth refresh + youtube.upload scope check
    python main.py --verify-tiktok [--category gadgets --lang he]      # token + Direct Post permission check
    python main.py --verify-telegram [--category gadgets --lang he]    # bot token + channel posting rights check
    python main.py --import-sqlite tarshish_engine.db   # copy a local SQLite database into DATABASE_URL (Supabase)
"""

from __future__ import annotations

import argparse
import sys

EXIT_OK, EXIT_FAILURES, EXIT_CONFIG, EXIT_BUDGET = 0, 1, 2, 3
_RULE = "─" * 78
_MARKS = {
    "published": "✓", "queued": "⏱", "preview": "•", "skipped": "-", "failed": "✗", "inserted": "✓",
}


_ENVIRONMENT_HELP = """
[ENVIRONMENT ERROR] A required package could not be imported: {error}

The virtual environment is missing dependencies, or it was built for a different
Python than the one running now ({version}). This happens after upgrading Python
or moving the project folder. Rebuild it from the project folder:

    python -m venv --clear .venv
    .venv\\Scripts\\activate          (Linux/macOS: source .venv/bin/activate)
    python -m pip install -r requirements-engine.txt
"""

_APP_CONTROL_HELP = """
[ENVIRONMENT ERROR] Windows blocked a library this program needs: {error}

This is Windows Smart App Control (or another application-control policy) refusing
to load an unsigned file from a freshly installed Python package. It is a setting
of this computer, not a fault in the project, and it can come and go between runs.

  1. Try again in a minute, then rebuild the environment (README, "Quick start").
  2. Always use the "python -m ..." commands from the README; the generated
     pip.exe / streamlit.exe launchers are blocked more often than python.exe.
  3. If it keeps happening, see "Windows Smart App Control" in the README.
"""


def _utf8_stdio() -> None:
    """Hebrew captions and box characters must not crash on legacy Windows code pages."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _content_types(value: str) -> list[str]:
    names = [name.strip() for name in value.split(",") if name.strip()]
    if not names:
        raise argparse.ArgumentTypeError("name at least one content type, e.g. instagram_post,instagram_story")
    return names


def _id_list(value: str) -> list[int]:
    try:
        ids = [int(part) for part in value.split(",") if part.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError("asset ids are numbers separated by commas, e.g. 12,13") from None
    if not ids or any(i < 1 for i in ids):
        raise argparse.ArgumentTypeError("name at least one asset id, e.g. 12")
    return list(dict.fromkeys(ids))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Two-stage affiliate engine: harvest products per category, then publish localized content "
                    "per category × language × content type.",
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    stages = parser.add_argument_group("stages")
    stages.add_argument("--harvest", action="store_true",
                        help="Stage 1: fetch products per (category, source) into SQLite — once, for every language")
    stages.add_argument("--publish", action="store_true",
                        help="Stage 2: generate and publish content for harvested products per (category, language)")
    stages.add_argument("--generate-assets", action="store_true",
                        help="FindHub live content: copy (free LLM chain) + branded images for REAL products "
                             "(temu/aliexpress, never mock) into generated_assets; needs --category and --lang; "
                             "--content-types picks from telegram_post, instagram_post, instagram_story, "
                             "instagram_reel, tiktok_post")
    stages.add_argument("--publish-assets", type=_id_list, metavar="IDS",
                        help="publish generated assets now through their platform publishers, e.g. 12,13")

    scope = parser.add_argument_group("scope")
    scope.add_argument("--category", metavar="ID", help="category id (default: every enabled category)")
    scope.add_argument("--source", metavar="PLUGIN",
                       help="with --harvest: one source plugin, e.g. temu_rapidapi, aliexpress_rapidapi, temu_mock "
                            "(default: every store the category has search rules for)")
    scope.add_argument("--lang", metavar="CODE",
                       help="with --publish: language, e.g. he or en (default: every language with an enabled account)")
    scope.add_argument("--content-types", type=_content_types, metavar="LIST",
                       help="with --publish: comma-separated content types, e.g. instagram_post,instagram_story "
                            "(default: every enabled type with an account)")

    run = parser.add_argument_group("run options")
    run.add_argument(
        "--mode", choices=["immediate", "queued", "worker"],
        help="immediate: publish now · queued: store for the worker · worker: consume the queue "
             "(default for --publish: PUBLISHING_MODE, queued)",
    )
    run.add_argument("--limit", type=_positive_int, metavar="N",
                     help="--harvest: products kept per (category, source) · --publish: products per content type")
    run.add_argument("--dry-run", action="store_true",
                     help="simulate: no publishing, no paid LLM calls, no API quota, no database writes")
    run.add_argument("--force", action="store_true", help="with --publish: re-process products already handled")
    run.add_argument("--once", action="store_true", help="with --mode worker: publish what is due, then exit")

    manage = parser.add_argument_group("management")
    manage.add_argument("--stats", action="store_true", help="print queue size, published count and monthly AI spend")
    manage.add_argument("--flush-queue", action="store_true", help="delete pending/queued publications")
    manage.add_argument("--retry-failed", action="store_true", help="put failed publications back in the queue")
    manage.add_argument("--list-config", action="store_true",
                        help="print categories, content-type routing and the social-accounts matrix")
    manage.add_argument("--sync-config", action="store_true",
                        help="upsert config/categories.yaml, content_profiles.yaml and social_accounts.yaml "
                             "into the database (overwrites dashboard edits)")
    manage.add_argument("--seed-storefront", action="store_true",
                        help="load the demo catalogue (14 mock products, two stores) into the FindHub storefront")
    manage.add_argument("--seed-temu", action="store_true",
                        help="ad-hoc --harvest --source temu_rapidapi with an optional --keyword override (needs --category)")
    manage.add_argument("--seed-aliexpress", action="store_true",
                        help="ad-hoc --harvest --source aliexpress_rapidapi with an optional --keyword override "
                             "(needs --category)")
    manage.add_argument("--keyword", metavar="TEXT", help="with --seed-temu / --seed-aliexpress: search terms")
    manage.add_argument("--job", metavar="ID",
                        help="with --seed-temu: collect the results of an earlier search job instead of searching")
    manage.add_argument("--verify-instagram", action="store_true",
                        help="check the Instagram accounts of the matrix (token, professional account, publishing "
                             "permissions) with read-only calls; --category / --lang narrow it")
    manage.add_argument("--verify-youtube", action="store_true",
                        help="check the YouTube channels of the matrix (OAuth refresh token, youtube.upload scope, "
                             "daily quota) with read-only calls; --category / --lang narrow it")
    manage.add_argument("--verify-tiktok", action="store_true",
                        help="check the TikTok accounts of the matrix (user access token, Direct Post permission, "
                             "privacy levels) with read-only calls; --category / --lang narrow it")
    manage.add_argument("--verify-telegram", action="store_true",
                        help="check the Telegram channels of the matrix (bot token, chat, posting rights) with "
                             "read-only calls; --category / --lang narrow it")
    manage.add_argument("--import-sqlite", metavar="FILE",
                        help="copy everything from a local SQLite database file into the configured "
                             "DATABASE_URL (PostgreSQL / Supabase), keeping ids")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="override LOG_LEVEL")
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    seeding = args.seed_temu or args.seed_aliexpress
    if args.generate_assets or args.publish_assets:
        if args.generate_assets and args.publish_assets:
            parser.error("run --generate-assets and --publish-assets separately (review the assets in between)")
        if args.harvest or args.publish or args.mode or args.dry_run or args.source:
            parser.error("--generate-assets / --publish-assets cannot be combined with --harvest, --publish, --mode, "
                         "--dry-run or --source")
        if args.generate_assets and not (args.category and args.lang):
            parser.error("--generate-assets needs --category ID and --lang he|en")
        if args.generate_assets and args.lang not in ("he", "en"):
            parser.error("--generate-assets writes Hebrew or English copy: --lang he or --lang en")
        if args.publish_assets and (args.category or args.lang or args.content_types or args.force or args.limit):
            parser.error("--publish-assets takes asset ids only")
        return
    if args.harvest and args.publish:
        parser.error("run --harvest and --publish separately: they are independent stages")
    if args.mode == "worker" and (args.harvest or args.publish or args.dry_run or args.limit or args.force):
        parser.error("--mode worker consumes the queue; it cannot be combined with --harvest/--publish/--limit/"
                     "--dry-run/--force")
    if args.once and args.mode != "worker":
        parser.error("--once only applies to --mode worker")
    if args.mode in ("immediate", "queued") and not args.publish:
        parser.error(f"--mode {args.mode} only applies to --publish")
    if args.source and not args.harvest:
        parser.error("--source only applies to --harvest")
    if (args.content_types or args.force) and not args.publish:
        parser.error("--content-types and --force only apply to --publish")
    verifying = args.verify_instagram or args.verify_youtube or args.verify_tiktok or args.verify_telegram
    if args.lang and not (args.publish or verifying or args.flush_queue or args.retry_failed or args.stats):
        parser.error("--lang applies to --publish (and narrows --verify-instagram/--verify-youtube/--verify-tiktok/"
                     "--verify-telegram, "
                     "--flush-queue, --retry-failed, --stats)")
    if args.seed_temu and args.seed_aliexpress:
        parser.error("run --seed-temu and --seed-aliexpress separately (they share --category/--keyword/--limit)")
    if seeding:
        flag = "--seed-temu" if args.seed_temu else "--seed-aliexpress"
        if not args.category:
            parser.error(f"{flag} needs --category NAME (for example: --category gadgets)")
        if args.dry_run or args.mode or args.harvest or args.publish:
            parser.error(f"{flag} makes real API requests and writes products; "
                         "it cannot be combined with --dry-run, --mode, --harvest or --publish")
        if args.job and not args.seed_temu:
            parser.error("--job only applies to --seed-temu (AliExpress searches answer at once)")
    elif args.keyword or args.job:
        parser.error("--keyword and --job only apply to --seed-temu / --seed-aliexpress "
                     "(--harvest uses the category's stored search rules)")
    if verifying and (args.dry_run or args.mode or args.limit or args.force or args.harvest or args.publish):
        parser.error("--verify-instagram / --verify-youtube / --verify-tiktok / --verify-telegram only read from the APIs; "
                     "they take --category / --lang and nothing else")


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def print_harvest(report) -> None:
    from plugins import format_price

    flags = " · DRY-RUN (nothing written)" if report.dry_run else ""
    print(_RULE)
    print(f" Harvest {report.source} ➔ category {report.category} · keyword \"{report.keyword}\" · "
          f"provider {report.provider or 'none delivered'}{flags}")
    for attempt in report.attempts:
        print(f"   {'✓' if attempt.ok else '✗'} provider   {attempt.provider:<24} {attempt.outcome:<16} {attempt.detail}")
    print(f" received {report.received} · inserted {len(report.inserted)} · already stored {len(report.duplicates)}"
          f" · {len(report.over_limit)} beyond the limit · {len(report.rejected)} rejected"
          f" (standard: rating ≥ {report.min_rating:g}, orders ≥ {report.min_orders:,})")
    for status, products in (("inserted", report.inserted), ("stored", report.duplicates)):
        for product in products:
            print(f"   {'✓' if status == 'inserted' else '-'} {status:<9} {product.external_id:<16} "
                  f"⭐ {product.rating:<4g}{product.orders_count:>9,} sold "
                  f"{format_price(product.price, product.currency):>9}  {_clip(product.title, 38)}")
    if report.error:
        print(f"   ✗ {report.error}")
    print(_RULE)


def print_report(report) -> None:
    flags = " · DRY-RUN" if report.dry_run else ""
    print(_RULE)
    print(f" Publishing pipeline {report.pipeline_id} · category {report.category} · language {report.language}"
          f" · mode {report.mode}{flags}")
    for route in report.routes:
        account = f"{route.publisher} → {route.account}" if route.account else "no account"
        state = "ready" if route.ready else (route.reason or "not ready")
        print(f"   ▸ {route.content_type:<16} {route.generator} [{route.llm_model}] ➔ {account}"
              f" · {route.candidates} product(s) · {_clip(state, 60)}")
    print(f" published {report.count('published')} · queued {report.count('queued')} · previewed "
          f"{report.count('preview')} · failed {report.count('failed')} · AI cost ${report.cost_usd:.4f}")
    for item in report.items:
        print(f"   {_MARKS.get(item.status, '?')} {item.status:<9} {item.content_type:<16} {_clip(item.title, 44)}")
        written = "" if item.model_used == item.configured_model else f" (written by: {item.model_used})"
        print(f"       {item.target} · model {item.configured_model}{written}"
              + (f" — {item.detail}" if item.detail and item.status != "preview" else ""))
    for warning in report.warnings:
        print(f"   ! {warning}")
    if report.error:
        print(f"   ✗ {report.error}")
    print(_RULE)


def cmd_stats(engine, pipeline_id: str | None) -> None:
    counts = engine.db.get_status_counts(pipeline_id)
    spent = engine.db.get_monthly_ai_spend()
    cap = engine.settings.max_monthly_ai_budget_usd
    used = f"{spent / cap:.1%}" if cap > 0 else "cap is 0"
    scope = f"pipeline {pipeline_id}" if pipeline_id else "all pipelines"
    print(_RULE)
    print(f" Tarshish Engine stats · {scope}")
    print(f"   Queue size        : {counts['QUEUED'] + counts['PENDING']}"
          f"  ({counts['QUEUED']} queued, {counts['PENDING']} pending)")
    print(f"   Published         : {counts['PUBLISHED']}")
    print(f"   Failed            : {counts['FAILED']}")
    print(f"   Monthly AI spend  : ${spent:.4f} of ${cap:.2f} ({used})")
    inventory = engine.db.count_products()
    if inventory:
        print("   Harvested products: " + ", ".join(
            f"{row['category'] or '(unlisted)'}/{row['source']} {row['products']}" for row in inventory))
    print(_RULE)


def cmd_list_config(engine) -> None:
    registry = engine.registry
    print(_RULE)
    print(" Categories (Stage 1 · harvested once per source, shared by every language)")
    for c in registry.list_categories():
        stores = ", ".join(c.default_sources()) or "no store rules"
        print(f"   {c.category_id:<12} {'enabled ' if c.enabled else 'disabled'} {c.display_name_en} / "
              f"{c.display_name_he or '-'} · rating ≥ {c.min_rating:g} · orders ≥ {c.min_orders:,} · {stores}")
    print(" Content types (Stage 2 · generator + model routing)")
    for p in registry.list_content_profiles():
        print(f"   {p.content_type:<16} {'enabled ' if p.enabled else 'disabled'} {p.platform:<10} "
              f"{p.generator} [{p.llm_model}]" + (f" · format {p.publish_format}" if p.publish_format else ""))
    print(" Social accounts (category × language × platform)")
    for a in registry.list_social_accounts():
        refs = ", ".join(name for name in (a.token_env_var, a.account_id_env_var) if name) or "publisher defaults"
        _, reason = registry.resolve_target(registry.social_page(a))
        state = "ready" if reason is None else "credentials missing"
        print(f"   {a.category_id:<10} {a.language:<3} {a.platform:<10} {a.account_handle:<20} "
              f"{'enabled ' if a.enabled else 'disabled'} {state:<20} env: {refs}")
    print(_RULE)


_SEED_LABELS = {  # source plugin -> (store name, tracking variable, chain file)
    "temu_rapidapi": ("Temu", "TEMU_TRACKING_ID", "config/temu_providers.yaml"),
    "aliexpress_rapidapi": ("AliExpress", "ALIEXPRESS_TRACKING_ID", "config/aliexpress_providers.yaml"),
}


def print_seed(report, limit: int) -> None:
    """The --seed-temu / --seed-aliexpress report (an ad-hoc harvest with a keyword override)."""
    from plugins import format_price, is_placeholder_image

    store, tracking_var, chain_file = _SEED_LABELS.get(report.source, _SEED_LABELS["temu_rapidapi"])

    def row(mark: str, status: str, product) -> str:
        title = product.title if len(product.title) <= 38 else product.title[:37] + "…"
        return (f"   {mark} {status:<10} {product.external_id:<16} ⭐ {product.rating:<4g}"
                f"{product.orders_count:>9,} sold {format_price(product.price, product.currency):>9}  {title}")

    print(_RULE)
    print(f" {store} seed · category {report.category} · keyword \"{report.keyword}\" · "
          f"provider {report.provider or 'none delivered'} · no LLM calls")
    for attempt in report.attempts:
        print(f"   {'✓' if attempt.ok else '✗'} provider   {attempt.provider:<24} {attempt.outcome:<16} {attempt.detail}")
    print(
        f" received {report.received} · inserted {len(report.inserted)} · skipped {len(report.duplicates)} duplicate"
        f" · {len(report.over_limit)} beyond --limit {limit} · {len(report.rejected)} rejected"
        f" (standard: rating ≥ {report.min_rating:g}, sold ≥ {report.min_orders:,})"
    )
    for product in report.inserted:
        print(row("✓", "inserted", product))
    for product in report.duplicates:
        print(row("-", "duplicate", product))
    for product in report.over_limit:
        print(row("·", "not kept", product))
    for label, reason in report.rejected:
        print(f"   ✗ rejected   {label} — {reason}")
    if report.notice:
        print(f"   ! API notice: {report.notice}")
    if any(product.affiliate_url == product.raw_url for product in report.inserted):
        print(f"   ! {tracking_var} is not set: the stored links are plain product URLs, not affiliate links.")
    stand_ins = sum(1 for product in report.inserted if any(is_placeholder_image(url) for url in product.image_urls))
    if stand_ins:
        print(f"   ! {stand_ins} product(s) came without a photo and show a stand-in picture until enriched.")
    if report.provider is None:
        print("   ! No provider delivered usable products. Check the provider lines above: add or renew a key,")
        print(f"     subscribe the key to another API in {chain_file}, or try broader search terms.")
    print(_RULE)


def cmd_verify_instagram(engine, category: str | None, language: str | None) -> bool:
    """Print a read-only connection report for each Instagram account; True when all are ready."""
    from plugins.publishers.instagram_pub import pages_to_verify, verify_pages

    pages = pages_to_verify(engine.registry, category, language)
    print(_RULE)
    scope = "/".join(part for part in (category, language) if part) or "configured accounts"
    print(f" Instagram API with Unified Login · connection check · {scope} · nothing is posted")
    if not pages:
        print(f"   ! No Instagram account is configured for {scope} (Social Accounts tab / config/social_accounts.yaml).")
        print(_RULE)
        return False
    results = verify_pages(engine.registry, pages)
    for result in results:
        print(f"   {'✓' if result.ok else '✗'} {result.page_name}: {result.summary()}")
        for line in result.lines():
            print(f"       {line}")
    ready = all(result.ok for result in results)
    print(" Ready for live publishing." if ready else
          " Not ready: fix the ✗ items above (see setup_roadmap.html, \"Instagram API with Unified Login Setup\").")
    print(_RULE)
    return ready


# Other platforms: platform -> (plugin module, report title, setup_roadmap.html section)
_VIDEO_CHECKS = {
    "telegram": ("plugins.publishers.telegram_pub", "Telegram Bot API · channel check",
                 'the Telegram step of the setup roadmap'),
    "youtube": ("plugins.publishers.youtube_pub", "YouTube Data API v3 (Shorts) · OAuth check",
                'section A of "Short-Form Video APIs Setup (YouTube Shorts & TikTok)"'),
    "tiktok": ("plugins.publishers.tiktok_pub", "TikTok Content Posting API (Direct Post) · token check",
               'section B of "Short-Form Video APIs Setup (YouTube Shorts & TikTok)"'),
}


def cmd_verify_video(engine, platform: str, category: str | None, language: str | None) -> bool:
    """Print a read-only connection report for each YouTube / TikTok / Telegram account; True when all are ready."""
    import importlib

    module_name, title, section = _VIDEO_CHECKS[platform]
    module = importlib.import_module(module_name)
    pages = module.pages_to_verify(engine.registry, category, language)
    print(_RULE)
    scope = "/".join(part for part in (category, language) if part) or "configured accounts"
    print(f" {title} · {scope} · nothing is posted")
    if not pages:
        print(f"   ! No {platform} account is configured for {scope} (Social Accounts tab / config/social_accounts.yaml).")
        print(_RULE)
        return False
    results = module.verify_pages(engine.registry, pages)
    for result in results:
        print(f"   {'✓' if result.ok else '✗'} {result.page_name}: {result.summary()}")
        for line in result.lines():
            print(f"       {line}")
    ready = bool(results) and all(result.ok for result in results)
    print(" Ready for live publishing." if ready else
          f" Not ready: fix the ✗ items above (see setup_roadmap.html, {section}).")
    print(_RULE)
    return ready


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_import_sqlite(engine, path: str) -> bool:
    from pathlib import Path

    from core.database import Database, copy_database

    source_file = Path(path).expanduser()
    if not engine.db.is_postgres:
        print("Set DATABASE_URL to the Supabase connection string first: --import-sqlite copies INTO it.",
              file=sys.stderr)
        return False
    if not source_file.is_file():
        print(f"No SQLite database at {source_file}.", file=sys.stderr)
        return False
    busy = engine.db.query_one(
        "SELECT (SELECT COUNT(*) FROM products) AS products, (SELECT COUNT(*) FROM publications) AS publications"
    ) or {}
    if busy.get("products") or busy.get("publications"):
        print(f"The {engine.db.describe()} already holds {busy['products']} product(s) and "
              f"{busy['publications']} publication(s); import only into a fresh database.", file=sys.stderr)
        return False
    copied = copy_database(Database(source_file), engine.db)
    print(f"Copied from {source_file.name} into the {engine.db.describe()}:")
    for table, rows in copied.items():
        print(f"  {table:<18} {rows:>7} row(s)")
    return True


def run_harvest(engine, args) -> int:
    reports = engine.harvester.harvest(args.category, args.source, limit=args.limit, dry_run=args.dry_run)
    for report in reports:
        print_harvest(report)
    return EXIT_OK if all(report.ok for report in reports) else EXIT_FAILURES


def run_publish(engine, args) -> int:
    from core.resilience import BudgetExceededError

    pairs = engine.orchestrator.plan(args.category, args.lang)
    if not pairs:
        print("Nothing to publish: no enabled social account matches. Link one in the dashboard "
              "(Social Accounts tab) or config/social_accounts.yaml.", file=sys.stderr)
        return EXIT_FAILURES
    exit_code = EXIT_OK
    for category, language in pairs:
        try:
            report = engine.orchestrator.run(
                category, language, args.content_types, mode=args.mode, limit=args.limit, dry_run=args.dry_run,
                force=args.force,
            )
        except BudgetExceededError as exc:
            partial = getattr(exc, "report", None)
            if partial is not None:
                print_report(partial)
            print(f"\n[BUDGET HALT] {exc}", file=sys.stderr)
            return EXIT_BUDGET
        print_report(report)
        if not report.ok:
            exit_code = EXIT_FAILURES
    return exit_code


def run_generate_assets(engine, args) -> int:
    from plugins.generators.content_generator import ContentGenerator

    report = ContentGenerator(engine).generate_batch(
        args.category, args.lang, args.content_types, limit=args.limit or 3, force=args.force, progress=print,
    )
    print()
    for line in report.lines():
        print(line)
    return EXIT_OK if report.created or not report.count("skipped") else EXIT_FAILURES


def run_publish_assets(engine, args) -> int:
    from plugins.generators.content_generator import AssetPublishError, ContentGenerator

    generator = ContentGenerator(engine)
    failed = False
    for asset_id in args.publish_assets:
        try:
            outcome = generator.publish_asset(asset_id)
        except AssetPublishError as exc:
            print(f"✗ #{asset_id} not published: {exc}")
            failed = True
            continue
        if outcome.success:
            print(f"✓ #{asset_id} {outcome.message} (post id {outcome.platform_post_id}).")
        else:
            print(f"✗ #{asset_id} failed: {outcome.message}")
            failed = True
    return EXIT_FAILURES if failed else EXIT_OK


def run_worker(engine, args) -> int:
    worker = engine.create_worker()
    if args.once:
        report = worker.run_once()
        note = " (outside active hours — nothing sent)" if report.outside_active_hours else ""
        print(f"Worker pass: published {report.published}, failed {report.failed}, "
              f"deferred {report.deferred}{note}.")
        return EXIT_OK if report.failed == 0 else EXIT_FAILURES
    print("Queue worker running — press Ctrl+C to stop.")
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop()
        print("\nWorker stopped.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)

    try:
        from core import bootstrap
        from core.resilience import ConfigError

        engine = bootstrap(log_level=args.log_level)
    except ImportError as exc:
        # Missing packages, or a virtualenv whose compiled packages belong to another Python.
        template = _APP_CONTROL_HELP if "Application Control" in str(exc) else _ENVIRONMENT_HELP
        print(template.format(error=exc, version=sys.version.split()[0]), file=sys.stderr)
        return EXIT_CONFIG
    except ConfigError as exc:
        # Startup validation failed: say exactly what is wrong and stop.
        print(f"\n[CONFIG ERROR] {exc}\n", file=sys.stderr)
        return EXIT_CONFIG
    except Exception as exc:
        from core.database import database_errors

        if not isinstance(exc, database_errors()):
            raise
        print(f"\n[DATABASE ERROR] {exc}\nCheck DATABASE_URL (or DATABASE_PATH).\n", file=sys.stderr)
        return EXIT_CONFIG

    return dispatch(engine, args, parser)


def dispatch(engine, args: argparse.Namespace, parser: argparse.ArgumentParser | None = None) -> int:
    """Run validated CLI arguments against a bootstrapped engine and return the exit code.

    Shared by the command line and the web admin (``storefront_api/admin.py``), which runs the
    same commands as background jobs and records what they print.
    """
    from core.orchestrator import pipeline_id_for
    from core.resilience import ConfigError, TarshishError

    pipeline_id = pipeline_id_for(args.category, args.lang) if args.category and args.lang else None
    try:
        if args.harvest:
            return run_harvest(engine, args)
        if args.publish:
            return run_publish(engine, args)
        if args.generate_assets:
            return run_generate_assets(engine, args)
        if args.publish_assets:
            return run_publish_assets(engine, args)
        if args.mode == "worker":
            return run_worker(engine, args)

        management, failed = False, False
        if args.import_sqlite:
            management = True
            failed = not cmd_import_sqlite(engine, args.import_sqlite)
        if args.verify_instagram:
            management = True
            failed = not cmd_verify_instagram(engine, args.category, args.lang)
        for platform, wanted in (("youtube", args.verify_youtube), ("tiktok", args.verify_tiktok),
                                 ("telegram", args.verify_telegram)):
            if wanted:
                management = True
                failed = (not cmd_verify_video(engine, platform, args.category, args.lang)) or failed
        if args.sync_config:
            management = True
            counts = engine.registry.sync_from_yaml()
            print("Synced " + ", ".join(f"{n} {table}" for table, n in counts.items())
                  + f" from {engine.settings.config_dir} into the database.")
        if args.seed_storefront:
            management = True
            from storefront_api.seed import seed_storefront

            print(f"Seeded {seed_storefront(engine.db)} demo product(s) into "
                  f"the {engine.db.describe()} for the storefront.")
        if args.seed_temu or args.seed_aliexpress:
            management = True
            from storefront_api.seed import seed_aliexpress, seed_temu

            limit = args.limit or 10
            if args.seed_temu:
                report = seed_temu(engine, category=args.category, keyword=args.keyword, limit=limit, job_id=args.job)
            else:
                report = seed_aliexpress(engine, category=args.category, keyword=args.keyword, limit=limit)
            print_seed(report, limit)
            failed = failed or report.provider is None
        if args.flush_queue:
            management = True
            print(f"Flushed {engine.db.flush_queue(pipeline_id)} pending/queued publication(s).")
        if args.retry_failed:
            management = True
            print(f"Re-queued {engine.db.retry_failed(pipeline_id=pipeline_id)} failed publication(s); "
                  "run `python main.py --mode worker` to publish them.")
        if args.list_config:
            management = True
            cmd_list_config(engine)
        if args.stats:
            management = True
            cmd_stats(engine, pipeline_id)
        if management:
            return EXIT_FAILURES if failed else EXIT_OK
        if parser is not None:
            parser.print_usage(sys.stderr)
        print("\nChoose a stage: --harvest (Stage 1: fetch products) or --publish (Stage 2: generate and publish), "
              "--mode worker, or a management flag. See --help.", file=sys.stderr)
        return EXIT_CONFIG
    except ConfigError as exc:
        print(f"\n[CONFIG ERROR] {exc}\n", file=sys.stderr)
        return EXIT_CONFIG
    except TarshishError as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        return EXIT_FAILURES



if __name__ == "__main__":
    sys.exit(main())
