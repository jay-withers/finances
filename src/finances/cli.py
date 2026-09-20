"""One entrypoint — `finances serve|import|seed|show|digest`.

`serve` is the web workload and `digest` is the scheduled job; both run in
Azure from the same image, told apart only by the `args` Terraform sets.
`import` and `show` are operator commands run against the real blob, so they
need `STATE_CONTAINER_URL` and a credential with `Storage Blob Data Contributor`
on the container — which whoever applied the Terraform already has.

`seed` is the opposite: it exists only for local work and refuses to run at all
when `STATE_CONTAINER_URL` is set.

Note Terraform deliberately sets **no** `command`: the Dockerfile's `ENTRYPOINT`
names this console script, and duplicating that name in Terraform creates a
second source of truth that is not versioned with the code defining it.
market-agent took a full outage from exactly that in September 2026, when an
`ignore_changes` command went stale against a renamed script and every workload
crash-looped on `executable file not found`. `args` picks the subcommand.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import date

from . import telemetry
from .settings import settings

logger = logging.getLogger("finances")


def _configure_logging() -> None:
    """Plain logging to stdout.

    The Container Apps environment already ships stdout to Log Analytics, where
    the daily quota is shared with every other tenant on the platform.
    `telemetry.configure()` then exports this same `finances` logger to
    Application Insights when a connection string is present, so a line logged
    here is paid for twice — keep it that way round rather than logging more
    because one of the two destinations happens to be quiet.
    """
    logging.basicConfig(
        level=getattr(logging, settings().log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stdout,
    )
    # Chatty at INFO and say nothing useful about the household's money.
    for noisy in (
        "httpx",
        "httpcore",
        "azure.core.pipeline.policies.http_logging_policy",
        "azure.identity",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _terminate(signum: int, _frame: object) -> None:
    """Turn SIGTERM into an exception so `finally` blocks run.

    Container Apps sends SIGTERM before SIGKILL when a replica is scaled down —
    which, at `min_replicas = 0`, happens after every visit. Without this the
    default disposition kills the process outright and buffered telemetry goes
    with it, because `telemetry.flush()` never runs.
    """
    raise SystemExit(f"terminated by signal {signum}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finances")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the web application")
    # Binds every interface because the container is the boundary: Container
    # Apps routes to the replica's own address, and 127.0.0.1 would be
    # unreachable from ingress.
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true", help="reload on edit, for local work")

    load = sub.add_parser("import", help="import the spreadsheet")
    load.add_argument("path", help="path to the workbook, e.g. Finances_3.xlsx")
    load.add_argument(
        "--force",
        action="store_true",
        help="replace a document that already holds data",
    )
    load.add_argument(
        "--as-of",
        metavar="YYYY-MM-DD",
        help="the date the spreadsheet is current as of; defaults to today",
    )

    sub.add_parser("show", help="print the document as JSON")

    seed = sub.add_parser("seed", help="write a sample document for local work")
    seed.add_argument("--force", action="store_true", help="overwrite an existing local document")

    digest = sub.add_parser("digest", help="send the reminder digest")
    digest.add_argument(
        "--dry-run", action="store_true", help="print the digest instead of sending it"
    )
    digest.add_argument("--url", default="", help="link to the app, included in the email")

    args = parser.parse_args(argv)

    _configure_logging()
    # Before the app is imported: the instrumentation patches FastAPI.__init__.
    telemetry.configure(args.command)
    signal.signal(signal.SIGTERM, _terminate)

    try:
        if args.command == "serve":
            return _serve(args)
        if args.command == "import":
            return _import(args)
        if args.command == "show":
            return _show()
        if args.command == "seed":
            return _seed(args)
        if args.command == "digest":
            return _digest(args)
    finally:
        telemetry.flush()

    return 1


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "finances.api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        # Access logs are noise against a tight shared ingestion cap, and the
        # probes alone would dominate them.
        access_log=False,
    )
    return 0


def _import(args: argparse.Namespace) -> int:
    """Migrate the spreadsheet, printing every total beside the sheet's own.

    Refuses to overwrite a document that already holds anything, because this
    replaces rather than merges: running it twice against a live document would
    discard every payday, spend and valuation recorded since the first run.
    """
    from . import store
    from .importer import import_workbook

    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    existing, etag = store.load()
    populated = bool(existing.pots or existing.outgoings or existing.renewals)
    if populated and not args.force:
        logger.error(
            "refusing to import: the document already holds %d pot(s), %d outgoing(s) "
            "and %d renewal(s), and importing replaces rather than merges. "
            "Pass --force if that is what you want.",
            len(existing.pots),
            len(existing.outgoings),
            len(existing.renewals),
        )
        return 1

    result = import_workbook(args.path, today=as_of)

    print()
    print(f"{'':36} {'spreadsheet':>14} {'imported':>14}")
    failures = 0
    for check in result.checks:
        mark = " " if check.ok else "!"
        if not check.ok:
            failures += 1
        print(f"{mark} {check.label:34} {check.sheet:>14} {check.imported:>14}")
        if check.note:
            print(f"    {check.note}")
    print()
    for note in result.skipped:
        print(f"  not imported: {note}")
    print()

    if failures:
        logger.error("%d check(s) did not reconcile; nothing was written", failures)
        return 1

    store.save(result.document, etag)
    logger.info(
        "imported %d pot(s), %d outgoing(s), %d renewal(s), %d wealth account(s)",
        len(result.document.pots),
        len(result.document.outgoings),
        len(result.document.renewals),
        len(result.document.wealth_accounts),
    )
    return 0


def _seed(args: argparse.Namespace) -> int:
    """Write a sample document to the *local* file, for working on the app offline.

    **Refuses point blank when STATE_CONTAINER_URL is set.** `store.save` writes
    wherever it is pointed, and pointing this at the deployed container would
    replace real balances with invented ones — the one thing this application
    exists not to do. A flag to override is deliberately absent: unset the
    variable for the length of one command instead.
    """
    from . import store
    from .seed import sample_document

    if settings().state_container_url:
        logger.error(
            "refusing to seed: STATE_CONTAINER_URL is set, and this would overwrite "
            "the real document with invented figures. Run it without that variable."
        )
        return 1

    path = store.local_path()
    if path.exists() and not args.force:
        logger.error("refusing to seed: %s already exists. Pass --force to replace it.", path)
        return 1

    doc = sample_document()
    store.save(doc)
    logger.info("seeded %s — %d pot(s), %d renewal(s)", path, len(doc.pots), len(doc.renewals))
    return 0


def _digest(args: argparse.Namespace) -> int:
    from .digest import run

    # Always 0, including on a failed send. The digest is composed and logged
    # whatever the mail provider does, so the run genuinely succeeded; exiting
    # non-zero would turn a bad minute at Resend into a job-failure alert on the
    # shared platform. `result.status` is on the log line for whoever looks.
    run(dry_run=args.dry_run, app_url=args.url)
    return 0


def _show() -> int:
    from . import store

    doc, _etag = store.load()
    print(doc.to_json())
    return 0
