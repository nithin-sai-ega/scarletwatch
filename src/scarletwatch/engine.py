"""Applies one validated poll result to every active watch of a semester."""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import machine
from .catalog import Catalog
from .machine import Event, EventKind, Status
from .semesters import Semester
from .store import Store, Watch

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Alert:
    """Something the scheduler should act on (notify, or just report)."""

    watch: Watch
    event: Event
    open_for: timedelta | None = None  # how long the section has been open


@dataclass(frozen=True)
class PollResult:
    alerts: list[Alert]
    newly_missing: list[Watch]


def apply_poll(
    store: Store,
    semester: Semester,
    open_indexes: Collection[str],
    now: datetime,
    catalog: Catalog | None = None,
    only_ids: Collection[int] | None = None,
) -> PollResult:
    """Run the state machine for each active watch and persist the outcome."""
    alerts: list[Alert] = []
    newly_missing: list[Watch] = []
    open_set = open_indexes if isinstance(open_indexes, (set, frozenset)) else set(open_indexes)

    for watch in store.watches_for(semester):
        if only_ids is not None and watch.id not in only_ids:
            continue
        if catalog is not None:
            if not catalog.has(watch.index):
                if watch.missing_since is None:
                    store.set_missing(watch.id, now)
                    newly_missing.append(watch)
            elif watch.missing_since is not None:
                store.set_missing(watch.id, None)

        previous = watch.state.status
        new_state, events = machine.step(
            watch.state, watch.index in open_set, now, watch.repeat_minutes
        )
        if new_state != watch.state:
            store.save_state(watch.id, new_state)
        if new_state.status is not None and new_state.status != previous:
            store.record_status(watch.id, new_state.status, now)

        updated = store.get_watch(watch.id) or watch
        for event in events:
            open_for = (
                now - new_state.last_changed_at
                if new_state.status == Status.OPEN and new_state.last_changed_at
                else None
            )
            alerts.append(Alert(updated, event, open_for))
            if event.kind == EventKind.ALREADY_OPEN:
                log.info("%s is already open", watch.index)
    return PollResult(alerts, newly_missing)


def confirm_notified(store: Store, watch_id: int, now: datetime) -> None:
    watch = store.get_watch(watch_id)
    if watch is not None:
        store.save_state(watch_id, machine.mark_notified(watch.state, now))


def confirm_notify_failed(store: Store, watch_id: int) -> None:
    watch = store.get_watch(watch_id)
    if watch is not None:
        store.save_state(watch_id, machine.mark_notify_failed(watch.state))
