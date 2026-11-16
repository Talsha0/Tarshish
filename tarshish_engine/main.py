#!/usr/bin/env python
"""Tarshish Engine command line.

Examples
--------
    python main.py --pipeline sports_he --mode immediate --dry-run   # simulate, publish nothing
    python main.py --pipeline sports_he --mode immediate             # generate and publish now
    python main.py --pipeline sports_he --mode queued                # generate and queue
    python main.py --mode worker                                     # publish the queue with jitter
    python main.py --stats                                           # queue size, published, AI spend
    python main.py --flush-queue                                     # drop pending/queued items
"""

from __future__ import annotations

import argparse
import sys

EXIT_OK, EXIT_FAILURES, EXIT_CONFIG, EXIT_BUDGET = 0, 1, 2, 3
_RULE = "─" * 78
_MARKS = {
    "published": "✓", "queued": "⏱", "preview": "•", "skipped": "-", "partial": "!", "failed": "✗",
    "unavailable": "!",
}


_ENVIRONMENT_HELP = """
[ENVIRONMENT ERROR] A required package could not be imported: {error}

The virtual environment is missing dependencies, or it was built for a different
Python than the one running now ({version}). This happens after upgrading Python
or moving the project folder. Rebuild it from the project folder:

    python -m venv --clear .venv
    .venv\\Scripts\\activate          (Linux/macOS: source .venv/bin/activate)
    python -m pip install -r requirements.txt
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Automated affiliate content pipelines: fetch products, generate content, publish or queue.",
        epilog=__doc__.split("Examples\n--------\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run = parser.add_argument_group("running pipelines")
    run.add_argument("--pipeline", metavar="ID", help="pipeline id (default: every enabled pipeline)")
    run.add_argument(
        "--mode", choices=["immediate", "queued", "worker"],
        help="immediate: publish now · queued: store for the worker · worker: consume the queue "
             "(default: each pipeline's configured publishing mode)",
    )
    run.add_argument("--limit", type=_positive_int, metavar="N", help="max products to handle per pipeline")
    run.add_argument(
        "--dry-run", action="store_true",
        help="simulate the full pipeline and print the result: no publishing, no paid LLM calls, no DB writes",
    )
    run.add_argument("--force", action="store_true", help="re-process products that were already published/queued")
    run.add_argument("--once", action="store_true", help="with --mode worker: publish what is due, then exit")

    manage = parser.add_argument_group("management")
    manage.add_argument("--stats", action="store_true", help="print queue size, published count and monthly AI spend")
    manage.add_argument("--flush-queue", action="store_true", help="delete pending/queued publications")
    manage.add_argument("--retry-failed", action="store_true", help="put failed publications back in the queue")
    manage.add_argument("--list-pipelines", action="store_true", help="list pipelines stored in the database")
    manage.add_argument(
        "--sync-config", action="store_true", help="upsert config/pipelines.yaml into the database (overwrites edits)"
    )
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="override LOG_LEVEL")
    return parser


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
def print_report(report) -> None:
    flags = " · DRY-RUN" if report.dry_run else ""
    print(_RULE)
    print(f" Pipeline {report.pipeline_id} · mode {report.mode}{flags}")
    print(
        f" fetched {report.fetched} · published {report.count('published')} · queued {report.count('queued')}"
        f" · previewed {report.count('preview')} · skipped {report.count('skipped')}"
        f" · failed {report.count('failed', 'partial')} · AI cost ${report.cost_usd:.4f}"
    )
    for item in report.items:
        title = item.title if len(item.title) <= 58 else item.title[:57] + "…"
        print(f"   {_MARKS.get(item.status, '?')} {item.status:<9} {title}")
        for target in item.targets:
            # Why a target is unavailable is the same for every item: it is printed once, as a warning.
            show_detail = target.detail and target.status not in ("preview", "unavailable")
            detail = f" — {target.detail}" if show_detail else ""
            print(f"       {_MARKS.get(target.status, '?')} {target.platform} → {target.page_name}: {target.status}{detail}")
        if item.detail and not item.targets:
            print(f"       {item.detail}")
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
    print(_RULE)


def cmd_list_pipelines(engine) -> None:
    pipelines = engine.registry.list_pipelines()
    print(_RULE)
    print(f" {len(pipelines)} pipeline(s) in {engine.settings.database_path.name}")
    for p in pipelines:
        state = "enabled " if p.enabled else "disabled"
        pages = ", ".join(page.label for page in p.social_pages)
        print(f"   {p.id:<18} {state} {p.language}  {p.source} ➔ {p.generator} [{p.llm_model}] ➔ {pages}"
              f"  ({p.publishing_mode})")
    print(_RULE)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def run_pipelines(engine, args) -> int:
    from core.resilience import BudgetExceededError, PipelineError

    if args.pipeline:
        pipeline_ids = [args.pipeline]
    else:
        pipeline_ids = [p.id for p in engine.registry.list_pipelines(enabled_only=True)]
        if not pipeline_ids:
            print("No enabled pipelines. Enable one in the dashboard or pass --pipeline ID.", file=sys.stderr)
            return EXIT_FAILURES

    exit_code = EXIT_OK
    for pipeline_id in pipeline_ids:
        try:
            report = engine.orchestrator.run(
                pipeline_id, mode=args.mode, limit=args.limit, dry_run=args.dry_run, force=args.force
            )
        except BudgetExceededError as exc:
            partial = getattr(exc, "report", None)
            if partial is not None:
                print_report(partial)
            print(f"\n[BUDGET HALT] {exc}", file=sys.stderr)
            return EXIT_BUDGET
        except PipelineError as exc:
            print(f"\n[ERROR] {exc}", file=sys.stderr)
            exit_code = EXIT_FAILURES
            continue
        print_report(report)
        if not report.ok:
            exit_code = EXIT_FAILURES
    return exit_code


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
    if args.mode == "worker" and (args.dry_run or args.pipeline or args.limit or args.force):
        parser.error("--mode worker consumes the queue; it cannot be combined with --pipeline/--limit/--dry-run/--force")
    if args.once and args.mode != "worker":
        parser.error("--once only applies to --mode worker")

    try:
        from core import bootstrap
        from core.resilience import ConfigError, TarshishError

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

    try:
        management = False
        if args.sync_config:
            management = True
            print(f"Synced {engine.registry.sync_from_yaml()} pipeline(s) from "
                  f"{engine.settings.pipelines_config_path.name} into the database.")
        if args.flush_queue:
            management = True
            print(f"Flushed {engine.db.flush_queue(args.pipeline)} pending/queued publication(s).")
        if args.retry_failed:
            management = True
            print(f"Re-queued {engine.db.retry_failed(pipeline_id=args.pipeline)} failed publication(s); "
                  "run `python main.py --mode worker` to publish them.")
        if args.list_pipelines:
            management = True
            cmd_list_pipelines(engine)
        if args.stats:
            management = True
            cmd_stats(engine, args.pipeline)
        if management:
            return EXIT_OK
        if args.mode == "worker":
            return run_worker(engine, args)
        return run_pipelines(engine, args)
    except ConfigError as exc:
        print(f"\n[CONFIG ERROR] {exc}\n", file=sys.stderr)
        return EXIT_CONFIG
    except TarshishError as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        return EXIT_FAILURES


if __name__ == "__main__":
    sys.exit(main())
