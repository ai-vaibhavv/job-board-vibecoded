"""Command-line interface.

`argparse` rather than a CLI framework: the spec's dependency list does not
include one, and `python -m job_alerts <command>` is exactly what argparse
subcommands give.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, Secrets, Settings, SourcesConfig, load_settings, load_sources
from .database import Database
from .logging_setup import configure_logging
from .models import JobStatus
from .notifications.discord import DiscordNotifier
from .scheduler import JobScheduler, RunLockedError, run_once

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m job_alerts",
        description="Find student research positions across Germany and send new ones to Discord.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m job_alerts search --dry-run     # safe first run, sends nothing\n"
            "  python -m job_alerts send-test            # check the Discord webhook\n"
            "  python -m job_alerts search               # real run\n"
            "  python -m job_alerts list --new\n"
            "  python -m job_alerts list --min-score 70\n"
            "  python -m job_alerts stats\n"
            "  python -m job_alerts run-scheduler        # stay running, 08:00 + 18:00\n"
            "  python -m job_alerts export --format csv > jobs.csv\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--settings", type=Path, help="path to settings.yaml")
    parser.add_argument("--sources", type=Path, help="path to sources.yaml")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--log-format", choices=["text", "json"])

    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    search = sub.add_parser("search", help="run one complete search now")
    search.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "print what would be sent; makes no request to Discord "
            "and writes nothing to the database"
        ),
    )
    search.add_argument(
        "--no-lock", action="store_true", help="skip the overlap lock (not recommended)"
    )

    sub.add_parser("send-test", help="send a test message to Discord")

    listing = sub.add_parser("list", help="list stored jobs")
    listing.add_argument("--new", action="store_true", help="only jobs not yet notified")
    listing.add_argument("--min-score", type=int, help="only jobs scoring at least this")
    listing.add_argument("--status", choices=[s.value for s in JobStatus])
    listing.add_argument("--limit", type=int, default=25)
    listing.add_argument("--explain", action="store_true", help="show the score breakdown")

    sub.add_parser("stats", help="show database statistics")
    sub.add_parser("run-scheduler", help="run continuously on the configured schedule")

    export = sub.add_parser("export", help="export stored jobs")
    export.add_argument("--format", choices=["csv", "json"], default="csv")
    export.add_argument("--min-score", type=int)
    export.add_argument("--limit", type=int, default=10000)
    export.add_argument("--output", type=Path, help="write to a file instead of stdout")

    check = sub.add_parser(
        "check-source",
        help="fetch one source and report what it parses, without storing or sending anything",
    )
    check.add_argument("name", help="source name from sources.yaml")

    sub.add_parser("show-config", help="show effective configuration (secrets masked)")

    return parser


def _load(args: argparse.Namespace) -> tuple[Settings, SourcesConfig, Secrets]:
    secrets = Secrets()
    settings = load_settings(args.settings, secrets)
    sources = load_sources(args.sources, secrets)
    configure_logging(
        args.log_level or settings.logging.level,
        args.log_format or settings.logging.format,
    )
    return settings, sources, secrets


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_search(args: argparse.Namespace) -> int:
    settings, sources, secrets = _load(args)

    if not args.dry_run and not secrets.has_discord:
        # Fail before doing the work, not after.
        secrets.require_discord()

    try:
        asyncio.run(
            run_once(settings, sources, secrets, dry_run=args.dry_run, use_lock=not args.no_lock)
        )
    except RunLockedError as exc:
        print(f"Not starting: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_send_test(args: argparse.Namespace) -> int:
    settings, _, secrets = _load(args)
    webhook = secrets.require_discord()

    async def _send() -> bool:
        async with DiscordNotifier(webhook, settings.notifications) as notifier:
            return await notifier.send_test()

    if asyncio.run(_send()):
        print("✅ Test message sent. Check your Discord channel.")
        return 0
    print(
        "❌ Could not send the test message.\n"
        "   - Is DISCORD_WEBHOOK_URL correct and not deleted in Discord?\n"
        "   - Re-run with --log-level DEBUG for details.",
        file=sys.stderr,
    )
    return 1


def cmd_list(args: argparse.Namespace) -> int:
    settings, _, _ = _load(args)
    with Database(settings.database.path) as db:
        jobs = db.list_jobs(
            status=JobStatus(args.status) if args.status else None,
            min_score=args.min_score,
            limit=args.limit,
            new_only=args.new,
        )

    if not jobs:
        print("No matching jobs. Run `python -m job_alerts search` first.")
        return 0

    print(f"\n{len(jobs)} job(s):\n")
    for job in jobs:
        flag = "🆕" if job.notified_at is None else "✅"
        print(f"{flag} [{job.relevance_score:3d}] {job.title}")
        print(f"        {job.organization or '—'} · {job.location or '—'} · via {job.source}")
        print(f"        {job.url}")
        if args.explain and job.score_explanation:
            for line in job.score_explanation:
                print(f"           {line}")
        print()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    settings, _, _ = _load(args)
    with Database(settings.database.path) as db:
        stats = db.stats()

    print(f"\nDatabase: {stats['database_path']}")
    print(f"Total jobs stored : {stats['total_jobs']}")
    average = stats["average_score"]
    print(f"Average score     : {average if average is not None else '—'}")
    print("\nBy status:")
    for status, count in sorted(stats["by_status"].items()):
        print(f"  {status:12s} {count}")
    print("\nBy source:")
    for source, count in stats["by_source"].items():
        print(f"  {source:24s} {count}")
    last = stats["last_run"]
    if last:
        print(f"\nLast run: {last['started_at']} → {last['finished_at']}")
    print()
    return 0


def cmd_run_scheduler(args: argparse.Namespace) -> int:
    settings, sources, secrets = _load(args)
    secrets.require_discord()
    scheduler = JobScheduler(settings, sources, secrets)
    try:
        asyncio.run(scheduler.start())
    except KeyboardInterrupt:
        print("\nScheduler stopped.")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    settings, _, _ = _load(args)
    with Database(settings.database.path) as db:
        jobs = db.list_jobs(min_score=args.min_score, limit=args.limit)

    if args.format == "json":
        payload = json.dumps(
            [j.model_dump(mode="json") for j in jobs], indent=2, ensure_ascii=False
        )
    else:
        buffer = io.StringIO()
        columns = [
            "id",
            "source",
            "title",
            "organization",
            "location",
            "country",
            "remote_status",
            "url",
            "published_at",
            "discovered_at",
            "application_deadline",
            "employment_type",
            "language",
            "salary",
            "relevance_score",
            "matched_keywords",
            "status",
            "notified_at",
        ]
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for job in jobs:
            row = job.model_dump(mode="json")
            row["matched_keywords"] = ", ".join(job.matched_keywords)
            writer.writerow({c: row.get(c, "") for c in columns})
        payload = buffer.getvalue()

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(f"Wrote {len(jobs)} job(s) to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(payload)
    return 0


def cmd_check_source(args: argparse.Namespace) -> int:
    """Fetch one source and report what it parsed.

    The tool for turning an UNVERIFIED entry in sources.example.yaml into a
    verified one: it touches only that source, stores nothing and sends nothing.
    """
    settings, sources_config, secrets = _load(args)

    config = next((s for s in sources_config.sources if s.name == args.name), None)
    if config is None:
        names = ", ".join(s.name for s in sources_config.sources) or "(none)"
        print(f"No source named {args.name!r}. Available: {names}", file=sys.stderr)
        return 1
    if config.forbidden:
        print(
            f"Source {args.name!r} is marked `forbidden: true` because its terms disallow "
            f"automated access. Refusing to fetch it.",
            file=sys.stderr,
        )
        return 2

    from .config import build_search_query
    from .http import PoliteClient
    from .normalization import normalize_candidate
    from .sources import build_source

    async def _check() -> int:
        async with PoliteClient(settings.http) as client:
            source = build_source(config, client, secrets)
            if config.url:
                allowed = await client.is_allowed(config.url)
                print(f"robots.txt allows fetching: {'yes' if allowed else 'NO'}")
                if not allowed:
                    print("Refusing to fetch a URL that robots.txt disallows.", file=sys.stderr)
                    return 2
            result = await source.run(build_search_query(settings))

        print(f"\nSource   : {result.source} (type: {config.type})")
        print(f"URL      : {config.url or '—'}")
        print(f"Duration : {result.duration_seconds:.2f}s")

        if result.skipped_reason:
            print(f"SKIPPED  : {result.skipped_reason}")
            return 0
        if result.error:
            print(f"\n❌ FAILED: {result.error}")
            print("\nIf this is an `html` source, the selectors are probably wrong.")
            print("Open the URL in a browser, inspect the markup, and fix `selectors:`.")
            return 1

        print(f"\n✅ Parsed {len(result.candidates)} candidate(s).")
        if not result.candidates:
            print("\n⚠️  The fetch worked but nothing parsed — selectors likely no longer match.")
            return 1

        for candidate in result.candidates[:5]:
            job = normalize_candidate(candidate)
            print(f"\n  • {job.title}")
            print(f"    org      : {job.organization or '—'}")
            print(f"    location : {job.location or '—'}")
            print(f"    url      : {job.url}")
            print(f"    published: {job.published_at.isoformat() if job.published_at else '—'}")
        if len(result.candidates) > 5:
            print(f"\n  … and {len(result.candidates) - 5} more.")
        print("\nIf these look right, this source can be trusted. Set `enabled: true`.")
        return 0

    return asyncio.run(_check())


def cmd_show_config(args: argparse.Namespace) -> int:
    settings, sources, secrets = _load(args)

    def mask(value: str) -> str:
        if not value:
            return "(not set)"
        return f"set ({len(value)} chars, ends …{value[-4:]})"

    print("\nSecrets (from environment / .env):")
    print(f"  DISCORD_WEBHOOK_URL : {mask(secrets.discord_webhook_url)}")
    print(f"  SEARCH_API_PROVIDER : {secrets.search_api_provider or '(not set)'}")
    print(f"  SEARCH_API_KEY      : {mask(secrets.search_api_key)}")
    print(f"  GOOGLE_CSE_ID       : {mask(secrets.google_cse_id)}")
    print(f"\n  Discord ready    : {'yes' if secrets.has_discord else 'NO'}")
    search_state = "yes" if secrets.has_search_api else "no (RSS/HTML sources still work)"
    print(f"  Search API ready : {search_state}")

    print(f"\nDatabase   : {settings.database.path}")
    print(f"Threshold  : notify at score >= {settings.scoring.min_score_to_notify}")
    print(f"Max per run: {settings.notifications.max_per_run}")
    print(f"Schedule   : {', '.join(settings.scheduler.run_at)} {settings.scheduler.timezone}")
    print(
        f"Keywords   : {len(settings.keywords.positive)} positive, "
        f"{len(settings.keywords.negative)} negative, {len(settings.keywords.topics)} topics"
    )

    print("\nSources:")
    for source in sources.sources:
        if source.forbidden:
            state = "FORBIDDEN (terms disallow automation)"
        elif source.enabled:
            state = "enabled"
        else:
            state = "disabled"
        print(f"  {source.name:24s} {source.type:12s} {state}")
    if not sources.active:
        print("\n  ⚠️  No sources are active. Enable at least one in config/sources.yaml.")
    print()
    return 0


_COMMANDS = {
    "search": cmd_search,
    "send-test": cmd_send_test,
    "list": cmd_list,
    "stats": cmd_stats,
    "run-scheduler": cmd_run_scheduler,
    "export": cmd_export,
    "check-source": cmd_check_source,
    "show-config": cmd_show_config,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except ConfigError as exc:
        # A user-fixable problem: show the message, not a traceback.
        print(f"\nConfiguration problem:\n{exc}\n", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
