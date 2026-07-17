"""In-process registry for long-running search tasks.

A dashboard search runs the full pipeline (30–120s: several sources, LLM
scoring). Blocking the HTTP request that long invites proxy 504s and ties up a
threadpool worker, so `POST /api/search/run` starts the work here and returns a
`task_id` the frontend polls. This is a single-user tool, so a dict guarded by a
lock is enough — no Celery/Redis.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

logger = logging.getLogger(__name__)

TaskState = Literal["running", "done", "error"]


@dataclass
class Task:
    id: str
    status: TaskState = "running"
    result: str | None = None
    error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "task_id": self.id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class TaskRegistry:
    """Thread-safe store of background search tasks.

    Only the newest handful are kept; a single user never needs history, and an
    unbounded dict would leak. `run` refuses to start a second task while one is
    already running, which mirrors the pipeline's own `RunLockedError` and keeps
    the paid sources from being fired twice at once.
    """

    _MAX_KEPT = 20

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

    def running_task(self) -> Task | None:
        with self._lock:
            for task in self._tasks.values():
                if task.status == "running":
                    return task
        return None

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def start(self, work: Callable[[], str]) -> Task:
        """Start `work` in a daemon thread; return the freshly created Task.

        Raises RuntimeError if a task is already running.
        """
        with self._lock:
            for task in self._tasks.values():
                if task.status == "running":
                    raise RuntimeError("a search is already running")
            task = Task(id=uuid.uuid4().hex)
            self._tasks[task.id] = task
            self._prune_locked()

        def _run() -> None:
            try:
                result = work()
                self._finish(task.id, result=result)
            except Exception as exc:  # never let a worker thread die silently
                logger.exception("background search task failed")
                self._finish(task.id, error=str(exc))

        threading.Thread(target=_run, name=f"search-{task.id[:8]}", daemon=True).start()
        return task

    def _finish(self, task_id: str, *, result: str | None = None, error: str | None = None) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.status = "error" if error is not None else "done"
            task.result = result
            task.error = error
            task.finished_at = datetime.now(UTC)

    def _prune_locked(self) -> None:
        if len(self._tasks) <= self._MAX_KEPT:
            return
        # Drop the oldest finished tasks first; never evict a running one.
        finished = [t for t in self._tasks.values() if t.status != "running"]
        finished.sort(key=lambda t: t.started_at)
        for task in finished[: len(self._tasks) - self._MAX_KEPT]:
            self._tasks.pop(task.id, None)
