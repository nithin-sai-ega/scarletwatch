import pytest

from scarletwatch.machine import Status
from scarletwatch.semesters import Semester
from scarletwatch.service import AddError

from .harness import FakeWorld, make_service

FALL = Semester(2026, 9, "NB")
SPRING = Semester(2027, 1, "NB")
CLOSED_IDX = "11425"  # 01:198:112 section 01, closed in the fixture
OPEN_IDX = "10052"  # open in the fixture


@pytest.fixture
def world(courses_data, open_data):
    return FakeWorld(courses_data, open_data)


@pytest.fixture
def svc(tmp_path, world, monkeypatch):
    service, store = make_service(tmp_path, world, monkeypatch)
    return service, store


async def test_add_validates_index_format_and_existence(svc):
    service, _ = svc
    with pytest.raises(AddError, match="5-digit"):
        await service.add_watch(FALL, "01")
    with pytest.raises(AddError, match="does not exist"):
        await service.add_watch(FALL, "77777")
    forced = await service.add_watch(FALL, "77777", force=True)
    assert forced.created
    assert forced.info is None


async def test_add_reports_status_immediately(svc, world):
    service, store = svc
    closed = await service.add_watch(FALL, CLOSED_IDX)
    assert closed.created
    assert closed.already_open is False
    assert closed.info.course_string == "01:198:112"
    world.advance(31)
    opened = await service.add_watch(FALL, OPEN_IDX)
    assert opened.already_open is True
    assert world.ntfy_posts() == [], "adding an already-open section must not push"
    again = await service.add_watch(FALL, OPEN_IDX)
    assert not again.created
    assert store.find_watch(FALL, OPEN_IDX).state.status == Status.OPEN


async def test_add_throttled_poll_leaves_status_unknown(svc, world):
    service, store = svc
    await service.add_watch(FALL, CLOSED_IDX)
    second = await service.add_watch(FALL, "11426")  # within 30 s of the previous request
    assert second.already_open is None
    assert world.count("/openSections.json") == 1
    assert store.find_watch(FALL, "11426").state.status is None


async def test_opens_once_then_flaps_notify_each_transition(svc, world):
    service, _ = svc
    await service.add_watch(FALL, CLOSED_IDX)
    base = [i for i in world.open_by_term["9"] if i != CLOSED_IDX]

    async def cycle(open_now: bool):
        world.open_by_term["9"] = base + ([CLOSED_IDX] if open_now else [])
        world.advance(61)
        await service.tick()

    await cycle(False)
    assert world.ntfy_posts() == []
    await cycle(True)
    posts = world.ntfy_posts()
    assert len(posts) == 1
    assert posts[0]["topic"] == "test-topic"
    assert "Data Structures" in posts[0]["title"]
    assert "01:198:112" in posts[0]["title"]
    assert "indexList=11425" in posts[0]["click"]
    for _ in range(5):
        await cycle(True)
    assert len(world.ntfy_posts()) == 1, "no repeat spam while it stays open"
    await cycle(False)
    await cycle(True)
    assert len(world.ntfy_posts()) == 2


async def test_one_request_per_semester_per_cycle(svc, world):
    service, store = svc
    for idx in ("11425", "11426", "11427", "11428"):
        store.add_watch(FALL, idx)
    store.add_watch(SPRING, "11425")
    await service.tick()
    assert world.count("/openSections.json") == 2
    world.advance(61)
    await service.tick()
    assert world.count("/openSections.json") == 4


async def test_not_due_means_no_request(svc, world):
    service, store = svc
    store.add_watch(FALL, CLOSED_IDX)
    await service.tick()
    world.advance(20)
    await service.tick()
    assert world.count("/openSections.json") == 1


async def test_semester_without_active_watches_is_not_polled(svc, world):
    service, store = svc
    w, _ = store.add_watch(FALL, CLOSED_IDX)
    await service.tick()
    store.set_done(w.id, True)
    world.advance(120)
    await service.tick()
    assert world.count("/openSections.json") == 1
    assert service._state == {}


async def test_empty_list_is_a_bad_poll_not_everything_closed(svc, world):
    service, store = svc
    await service.add_watch(FALL, OPEN_IDX)
    world.advance(61)
    await service.tick()
    world.open_by_term["9"] = []
    world.advance(61)
    await service.tick()
    watch = store.find_watch(FALL, OPEN_IDX)
    assert watch.state.status == Status.OPEN, "state must be untouched by a bad poll"
    health = store.get_health(FALL)
    assert health.consecutive_failures == 1
    assert "empty" in health.last_error
    # recover: section is open again (never really closed) -> still no notification
    world.open_by_term["9"] = ["10052"] + [f"{20000 + i}" for i in range(50)]
    world.advance(200)
    await service.tick()
    assert store.get_health(FALL).consecutive_failures == 0
    assert world.ntfy_posts() == []


async def test_malformed_json_and_http_errors_do_not_crash_or_notify(svc, world):
    service, store = svc
    await service.add_watch(FALL, CLOSED_IDX)
    world.open_by_term["9"] = {"unexpected": "object"}
    world.advance(61)
    await service.tick()
    assert "suspicious" in store.get_health(FALL).last_error
    world.open_status = 503
    world.advance(300)
    await service.tick()
    assert store.get_health(FALL).consecutive_failures == 2
    assert world.ntfy_posts() == []


async def test_429_retry_after_is_honored(svc, world):
    service, store = svc
    store.add_watch(FALL, CLOSED_IDX)
    world.open_status = 429
    world.open_headers = {"Retry-After": "600"}
    await service.tick()
    requests_before = world.count("/openSections.json")
    world.advance(300)  # well past the normal interval, but inside Retry-After
    await service.tick()
    assert world.count("/openSections.json") == requests_before
    world.advance(301)
    world.open_status = 200
    await service.tick()
    assert world.count("/openSections.json") == requests_before + 1
    assert store.get_health(FALL).consecutive_failures == 0


async def test_failed_delivery_is_retried_until_it_works(svc, world):
    service, store = svc
    await service.add_watch(FALL, CLOSED_IDX)
    world.open_by_term["9"] = [*world.open_by_term["9"], CLOSED_IDX]
    world.ntfy_status = 500
    world.advance(61)
    await service.tick()
    w = store.find_watch(FALL, CLOSED_IDX)
    assert w.state.pending_notify
    assert w.state.notify_count == 0
    world.ntfy_status = 200
    world.advance(61)
    await service.tick()
    w = store.find_watch(FALL, CLOSED_IDX)
    assert not w.state.pending_notify
    assert w.state.notify_count == 1
    ok_posts = [p for p in world.ntfy_posts()]
    assert len(ok_posts) == 2  # one failed attempt, one success
    world.advance(61)
    await service.tick()
    assert len(world.ntfy_posts()) == 2


async def test_no_notifier_configured_keeps_notification_pending(tmp_path, world, monkeypatch):
    service, store = make_service(tmp_path, world, monkeypatch, ntfy_topic=None)
    await service.add_watch(FALL, CLOSED_IDX)
    world.open_by_term["9"] = [*world.open_by_term["9"], CLOSED_IDX]
    world.advance(61)
    await service.tick()
    w = store.find_watch(FALL, CLOSED_IDX)
    assert w.state.pending_notify
    assert world.ntfy_posts() == []


async def test_ui_saved_topic_overrides_env(svc, world):
    service, store = svc
    store.set_setting("ntfy_topic", "ui-topic")
    results = await service.send_test()
    assert [r.ok for r in results] == [True]
    assert world.ntfy_posts()[0]["topic"] == "ui-topic"


async def test_catalog_downloaded_once_across_many_polls(svc, world):
    service, store = svc
    store.add_watch(FALL, CLOSED_IDX)
    for _ in range(6):
        await service.tick()
        world.advance(61)
    assert world.count("/courses.json") == 1
    assert world.count("/openSections.json") == 6


async def test_index_vanishing_from_catalog_warns_once(svc, world):
    service, store = svc
    store.add_watch(FALL, "77777")
    for _ in range(3):
        await service.tick()
        world.advance(61)
    posts = world.ntfy_posts()
    assert len(posts) == 1
    assert "vanished" in posts[0]["title"]
    assert store.find_watch(FALL, "77777").missing_since is not None


async def test_unexpected_exception_in_one_semester_does_not_stop_the_others(
    svc, world, monkeypatch
):
    service, store = svc
    store.add_watch(FALL, CLOSED_IDX)
    store.add_watch(SPRING, CLOSED_IDX)
    original = service.poll_semester

    async def flaky(semester, **kw):
        if semester == FALL:
            raise RuntimeError("boom")
        return await original(semester, **kw)

    monkeypatch.setattr(service, "poll_semester", flaky)
    await service.tick()
    assert world.count("/openSections.json") == 1  # spring still polled
