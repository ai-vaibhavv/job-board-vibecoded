"""The notifier interface.

Kept deliberately small so another channel (email, Telegram) can be added
without touching the pipeline. Discord is currently the only implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..models import Job


@dataclass(slots=True)
class DeliveryResult:
    """What actually got through.

    `delivered_ids` drives `mark_notified`, and nothing else does. A job only
    appears here after the channel confirmed acceptance, which is what stops a
    failed send from marking jobs as notified.
    """

    delivered_ids: list[str] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    messages_sent: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed_ids and not self.errors


@runtime_checkable
class Notifier(Protocol):
    """Somewhere jobs can be sent."""

    name: str

    async def send_jobs(self, jobs: list[Job]) -> DeliveryResult: ...

    async def send_test(self) -> bool:
        """Send a "wiring works" message. Returns True on success."""
        ...
