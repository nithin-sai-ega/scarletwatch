from datetime import UTC, datetime, timedelta

import pytest

from scarletwatch import engine
from scarletwatch.catalog import Catalog, reduce_catalog
from scarletwatch.machine import EventKind, Status
from scarletwatch.semesters import Semester
from scarletwatch.store import Health, Store

SEM = Semester(2026, 9, "NB")
OTHER = Semester(2027, 1, "NB")
T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def mins(n: float) -> datetime:
    return T0 + timedelta(minutes=n)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db" / "t.db")
    yield s
    s.close()


def test_add_list_remove(store):
    w, created = store.add_watch(SEM, "11425")
    assert created
    assert w.semester == SEM
    assert w.index == "11425"
    assert not w.done
    again, created = store.add_watch(SEM, "11425")
    assert not created
    assert again.id == w.id
    store.add_watch(OTHER, "10001")
    assert len(store.list_watches()) == 2
    assert store.active_semesters() == {SEM, OTHER}
    assert store.remove_watch(w.id)
    assert store.get_watch(w.id) is None
    assert not store.remove_watch(w.id)


def test_done_watches_are_not_polled_and_can_be_reactivated(store):
    w, _ = store.add_watch(SEM, "11425")
    store.set_done(w.id, True)
    assert store.active_semesters() == set()
    assert store.watches_for(SEM) == []
    assert store.list_watches()[0].done
    again, created = store.add_watch(SEM, "11425")
    assert created
    assert not again.done


def test_state_roundtrip_and_cascade_delete(store):
    w, _ = store.add_watch(SEM, "11425", repeat_minutes=15)
    engine.apply_poll(store, SEM, set(), T0)
    engine.apply_poll(store, SEM, {"11425"}, mins(1))
    w = store.get_watch(w.id)
    assert w.repeat_minutes == 15
    assert w.state.status == Status.OPEN
    assert w.state.pending_notify
    assert store.history(w.id)
    store.remove_watch(w.id)
    assert store.history(w.id) == []


def test_settings_roundtrip(store):
    assert store.get_setting("ntfy_topic") is None
    store.set_setting("ntfy_topic", "abc")
    store.set_setting("ntfy_topic", "abcd")
    assert store.all_settings() == {"ntfy_topic": "abcd"}
    store.set_setting("ntfy_topic", "")
    assert store.all_settings() == {}


def test_health_roundtrip(store):
    assert store.get_health(SEM).consecutive_failures == 0
    store.save_health(Health(SEM.key, T0, None, "boom", 3, 8000))
    h = store.get_health(SEM)
    assert (h.last_error, h.consecutive_failures, h.last_list_size) == ("boom", 3, 8000)
    assert h.last_attempt_at == T0


def test_engine_full_lifecycle_with_history(store):
    w, _ = store.add_watch(SEM, "11425")
    res = engine.apply_poll(store, SEM, {"99999"}, T0)  # first poll: closed
    assert res.alerts == []
    res = engine.apply_poll(store, SEM, {"11425"}, mins(1))
    assert [a.event.kind for a in res.alerts] == [EventKind.OPENED]
    engine.confirm_notified(store, w.id, mins(1))
    res = engine.apply_poll(store, SEM, {"11425"}, mins(3))
    assert res.alerts == []
    res = engine.apply_poll(store, SEM, set(), mins(5))
    assert [a.event.kind for a in res.alerts] == [EventKind.CLOSED]
    assert res.alerts[0].event.open_duration == timedelta(minutes=4)

    hist = store.history(w.id)
    assert [h.status for h in hist] == [Status.CLOSED, Status.OPEN, Status.CLOSED]
    open_entry = hist[1]
    assert open_entry.ended_at - open_entry.started_at == timedelta(minutes=4)
    assert store.get_watch(w.id).state.notify_count == 1


def test_engine_first_poll_open_reports_without_notifying(store):
    store.add_watch(SEM, "11425")
    res = engine.apply_poll(store, SEM, {"11425"}, T0)
    assert [a.event.kind for a in res.alerts] == [EventKind.ALREADY_OPEN]
    res = engine.apply_poll(store, SEM, {"11425"}, mins(1))
    assert res.alerts == []


def test_engine_open_for_duration_on_repeat(store):
    w, _ = store.add_watch(SEM, "11425", repeat_minutes=10)
    engine.apply_poll(store, SEM, set(), T0)
    engine.apply_poll(store, SEM, {"11425"}, mins(1))
    engine.confirm_notified(store, w.id, mins(1))
    res = engine.apply_poll(store, SEM, {"11425"}, mins(12))
    assert [a.event.kind for a in res.alerts] == [EventKind.REPEAT]
    assert res.alerts[0].open_for == timedelta(minutes=11)


def test_engine_only_touches_its_own_semester(store):
    store.add_watch(SEM, "11425")
    store.add_watch(OTHER, "11425")
    engine.apply_poll(store, SEM, set(), T0)
    assert store.find_watch(OTHER, "11425").state.status is None
    assert store.find_watch(SEM, "11425").state.status == Status.CLOSED


def test_engine_flags_index_missing_from_catalog_once(store, courses_data):
    catalog = Catalog(SEM, reduce_catalog(courses_data), 0.0)
    store.add_watch(SEM, "11425")
    store.add_watch(SEM, "77777")
    res = engine.apply_poll(store, SEM, set(), T0, catalog)
    assert [w.index for w in res.newly_missing] == ["77777"]
    res = engine.apply_poll(store, SEM, set(), mins(1), catalog)
    assert res.newly_missing == []
    assert store.find_watch(SEM, "77777").missing_since == T0
    assert store.find_watch(SEM, "11425").missing_since is None
