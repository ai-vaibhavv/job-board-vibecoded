"""APScheduler-based local scheduler with overlap protection.

The lock is a PID file rather than an in-process flag: the point is to survive
the case where a cron entry *and* `run-scheduler` are both live, which an
in-process guard cannot see. Stale locks (from a killed process) are detected
and reclaimed, otherwise one crash would wedge the scheduler forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import ProfileSettings, Secrets, Settings, SourcesConfig, load_profile
from .database import Database
from .models import RunSummary
from .pipeline import Pipeline

logger = logging.getLogger(__name__)


class RunLockedError(RuntimeError):
    """Another run holds the lock."""


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but belongs to another user — alive as far as we care.
        return True
    except OSError:
        return False
    return True


@contextmanager
def run_lock(lock_file: Path) -> Iterator[None]:
    """Hold an exclusive run lock, or raise `RunLockedError`."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    if lock_file.exists():
        try:
            existing = int(lock_file.read_text().strip() or 0)
        except (ValueError, OSError):
            existing = 0
        if existing and _process_alive(existing):
            raise RunLockedError(
                f"another run is in progress (pid {existing}, lock {lock_file}). "
                "Delete the lock file if you are certain it is stale."
            )
        logger.warning("removing stale lock file %s (pid %s is gone)", lock_file, existing or "?")
        lock_file.unlink(missing_ok=True)

    # O_EXCL makes creation atomic, closing the race between two runs that both
    # just saw an empty directory.
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RunLockedError(f"another run just acquired {lock_file}") from exc

    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock_file.unlink(missing_ok=True)


async def run_once(
    settings: Settings,
    sources_config: SourcesConfig,
    secrets: Secrets,
    *,
    dry_run: bool = False,
    use_lock: bool = True,
    incremental: bool = False,
    profile: ProfileSettings | None = None,
) -> RunSummary:
    """One pipeline run, guarded by the lock. Returns the run summary.

    The summary is also printed for the CLI; callers that only want the side
    effects (cmd_search, the scheduler tick) simply ignore the return value,
    while the dashboard uses it to report real counts.

    `incremental` stores each job as its verdict lands (for the dashboard's live
    board); the headless CLI/scheduler leave it off and store once at the end.
    """
    profile = profile if profile is not None else load_profile(secrets=secrets)
    lock_ctx = run_lock(settings.scheduler.lock_file) if use_lock else _null_context()
    with lock_ctx, Database(settings.database.path) as db:
        pipeline = Pipeline(settings, sources_config, secrets, db, profile)
        summary = await pipeline.run(dry_run=dry_run, incremental=incremental)
        print(summary.render())
        return summary


@contextmanager
def _null_context() -> Iterator[None]:
    yield


class JobScheduler:
    """Runs the pipeline at the configured local times."""

    def __init__(self, settings: Settings, sources_config: SourcesConfig, secrets: Secrets) -> None:
        self.settings = settings
        self.sources_config = sources_config
        self.secrets = secrets
        self.timezone = ZoneInfo(settings.scheduler.timezone)
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)

    async def _tick(self) -> None:
        logger.info("scheduled run starting (%s)", datetime.now(self.timezone).isoformat())
        try:
            await run_once(self.settings, self.sources_config, self.secrets)
        except RunLockedError as exc:
            # Expected when a run overruns its slot; the next tick will catch up.
            logger.warning("skipping scheduled run: %s", exc)
        except Exception:
            logger.exception("scheduled run failed; the scheduler will continue")

    async def start(self) -> None:
        for time_str in self.settings.scheduler.run_at:
            hour, minute = time_str.split(":")
            self.scheduler.add_job(
                self._tick,
                CronTrigger(hour=int(hour), minute=int(minute), timezone=self.timezone),
                id=f"search-{time_str}",
                name=f"Job search at {time_str} {self.settings.scheduler.timezone}",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )

        self.scheduler.start()
        times = ", ".join(self.settings.scheduler.run_at)
        logger.info("scheduler started — runs at %s (%s)", times, self.settings.scheduler.timezone)
        for job in self.scheduler.get_jobs():
            logger.info("  next run of %s: %s", job.id, job.next_run_time)
        timezone_name = self.settings.scheduler.timezone
        print(f"Scheduler running. Searches at {times} {timezone_name}. Ctrl+C to stop.")

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # Windows has no signal handlers on the event loop; it falls back to
            # the KeyboardInterrupt path below.
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        try:
            await stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            logger.info("shutting down scheduler")
            self.scheduler.shutdown(wait=True)
