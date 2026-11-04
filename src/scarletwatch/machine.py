"""Pure state machine for one watched section. No I/O, no clocks: everything is passed in.

Rules:
* First observation: record the status. If the section is already open, emit ALREADY_OPEN
  (reported once, never pushed as a notification).
* closed -> open: emit OPENED exactly once and mark the notification as pending. If no
  notifier succeeds the pending flag survives and OPENED is re-emitted (retry=True) on
  following polls, up to MAX_NOTIFY_ATTEMPTS.
* open -> closed: emit CLOSED (the engine records how long it was open).
* open -> open: nothing, unless "repeat every N minutes" is configured and due.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

MAX_NOTIFY_ATTEMPTS = 10


class Status(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class EventKind(StrEnum):
    ALREADY_OPEN = "already_open"
    OPENED = "opened"
    CLOSED = "closed"
    REPEAT = "repeat"


@dataclass(frozen=True)
class WatchState:
    status: Status | None = None
    last_changed_at: datetime | None = None
    last_notified_at: datetime | None = None
    notify_count: int = 0
    pending_notify: bool = False
    notify_attempts: int = 0


@dataclass(frozen=True)
class Event:
    kind: EventKind
    retry: bool = False
    # For CLOSED: how long the section had been open.
    open_duration: timedelta | None = None


def step(
    state: WatchState,
    is_open: bool,
    now: datetime,
    repeat_minutes: int | None = None,
) -> tuple[WatchState, list[Event]]:
    observed = Status.OPEN if is_open else Status.CLOSED

    if state.status is None:
        new = replace(state, status=observed, last_changed_at=now)
        return new, ([Event(EventKind.ALREADY_OPEN)] if is_open else [])

    if state.status != observed:
        if is_open:
            new = replace(
                state,
                status=observed,
                last_changed_at=now,
                pending_notify=True,
                notify_attempts=0,
            )
            return new, [Event(EventKind.OPENED)]
        duration = now - state.last_changed_at if state.last_changed_at else None
        new = replace(
            state,
            status=observed,
            last_changed_at=now,
            pending_notify=False,
            notify_attempts=0,
        )
        return new, [Event(EventKind.CLOSED, open_duration=duration)]

    # unchanged
    if not is_open:
        return state, []
    if state.pending_notify:
        if state.notify_attempts >= MAX_NOTIFY_ATTEMPTS:
            return replace(state, pending_notify=False), []
        return state, [Event(EventKind.OPENED, retry=True)]
    if (
        repeat_minutes
        and state.last_notified_at is not None
        and now - state.last_notified_at >= timedelta(minutes=repeat_minutes)
    ):
        return state, [Event(EventKind.REPEAT)]
    return state, []


def mark_notified(state: WatchState, now: datetime) -> WatchState:
    return replace(
        state,
        pending_notify=False,
        notify_attempts=0,
        last_notified_at=now,
        notify_count=state.notify_count + 1,
    )


def mark_notify_failed(state: WatchState) -> WatchState:
    return replace(state, notify_attempts=state.notify_attempts + 1)
