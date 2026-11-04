from datetime import UTC, datetime, timedelta

from scarletwatch.machine import (
    MAX_NOTIFY_ATTEMPTS,
    EventKind,
    Status,
    WatchState,
    mark_notified,
    mark_notify_failed,
    step,
)

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def kinds(events):
    return [e.kind for e in events]


def test_first_poll_closed_is_silent():
    state, events = step(WatchState(), False, at(0))
    assert state.status == Status.CLOSED
    assert events == []


def test_first_poll_open_reports_already_open_but_does_not_arm_notification():
    state, events = step(WatchState(), True, at(0))
    assert kinds(events) == [EventKind.ALREADY_OPEN]
    assert state.status == Status.OPEN
    assert not state.pending_notify
    # and never again while it stays open
    for m in range(1, 5):
        state, events = step(state, True, at(m))
        assert events == []


def test_closed_to_open_fires_once():
    state, _ = step(WatchState(), False, at(0))
    state, events = step(state, True, at(1))
    assert kinds(events) == [EventKind.OPENED]
    assert state.pending_notify
    assert state.last_changed_at == at(1)
    state = mark_notified(state, at(1))
    assert state.notify_count == 1
    assert not state.pending_notify
    for m in range(2, 20):
        state, events = step(state, True, at(m))
        assert events == [], "no repeat spam while still open"


def test_open_to_closed_reports_duration():
    state, _ = step(WatchState(), False, at(0))
    state, _ = step(state, True, at(1))
    state = mark_notified(state, at(1))
    state, events = step(state, False, at(5))
    assert kinds(events) == [EventKind.CLOSED]
    assert events[0].open_duration == timedelta(minutes=4)
    assert state.status == Status.CLOSED


def test_flapping_notifies_on_every_closed_to_open_transition():
    state, _ = step(WatchState(), False, at(0))
    for cycle in range(3):
        state, events = step(state, True, at(1 + 2 * cycle))
        assert kinds(events) == [EventKind.OPENED]
        state = mark_notified(state, at(1 + 2 * cycle))
        state, events = step(state, False, at(2 + 2 * cycle))
        assert kinds(events) == [EventKind.CLOSED]
    assert state.notify_count == 3


def test_closed_stays_closed_is_silent():
    state, _ = step(WatchState(), False, at(0))
    for m in range(1, 10):
        state, events = step(state, False, at(m))
        assert events == []


def test_repeat_while_open_only_when_configured_and_due():
    state, _ = step(WatchState(), False, at(0))
    state, _ = step(state, True, at(1))
    state = mark_notified(state, at(1))
    _, events = step(state, True, at(20), repeat_minutes=None)
    assert events == []
    _, events = step(state, True, at(5), repeat_minutes=10)
    assert events == []
    _, events = step(state, True, at(11), repeat_minutes=10)
    assert kinds(events) == [EventKind.REPEAT]
    state = mark_notified(state, at(11))
    _, events = step(state, True, at(12), repeat_minutes=10)
    assert events == []


def test_failed_delivery_is_retried_then_abandoned():
    state, _ = step(WatchState(), False, at(0))
    state, _ = step(state, True, at(1))
    for n in range(MAX_NOTIFY_ATTEMPTS):
        state, events = step(state, True, at(2 + n))
        assert len(events) == 1
        assert events[0].retry
        state = mark_notify_failed(state)
    state, events = step(state, True, at(50))
    assert events == []
    assert not state.pending_notify


def test_pending_cleared_when_it_closes_again():
    state, _ = step(WatchState(), False, at(0))
    state, _ = step(state, True, at(1))
    state, _ = step(state, False, at(2))
    assert not state.pending_notify
    state, events = step(state, True, at(3))
    assert kinds(events) == [EventKind.OPENED]
    assert not events[0].retry
