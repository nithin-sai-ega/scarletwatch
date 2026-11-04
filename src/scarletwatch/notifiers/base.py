"""Notifier interface and the dispatcher that fans a notification out to all notifiers."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import redact

log = logging.getLogger(__name__)


class NotifyError(Exception):
    """Delivery failed. The message must not contain secrets (it is logged and shown)."""


@dataclass(frozen=True)
class Notification:
    title: str
    body: str
    link: str | None = None
    link_label: str = "Register"
    urgent: bool = True
    kind: str = "section_opened"
    data: dict[str, Any] = field(default_factory=dict)


class Notifier(ABC):
    """One delivery channel. Subclasses implement ``send`` and raise NotifyError on failure."""

    name: str = "notifier"

    @abstractmethod
    async def send(self, notification: Notification) -> None: ...


@dataclass(frozen=True)
class DeliveryResult:
    notifier: str
    ok: bool
    error: str | None = None


async def _send_one(notifier: Notifier, notification: Notification) -> DeliveryResult:
    try:
        await notifier.send(notification)
    except NotifyError as exc:
        error = redact(str(exc))
    except Exception as exc:
        # A buggy notifier must never take down the others or the poll loop.
        error = redact(f"{type(exc).__name__}: {exc}")
        log.debug("notifier %s crashed", notifier.name, exc_info=True)
    else:
        return DeliveryResult(notifier.name, True)
    log.warning("notifier %s failed: %s", notifier.name, error)
    return DeliveryResult(notifier.name, False, error)


async def dispatch(
    notifiers: Sequence[Notifier], notification: Notification
) -> list[DeliveryResult]:
    """Send to every notifier concurrently; one failure never blocks the others."""
    if not notifiers:
        return []
    return list(await asyncio.gather(*(_send_one(n, notification) for n in notifiers)))
