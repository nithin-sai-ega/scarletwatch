import gzip
import json

import httpx
import pytest

from scarletwatch.catalog import (
    Catalog,
    CatalogStore,
    CatalogUnavailable,
    format_meetings,
    reduce_catalog,
)
from scarletwatch.rutgers import RutgersClient
from scarletwatch.semesters import Semester

SEM = Semester(2026, 9, "NB")


@pytest.fixture
def catalog(courses_data) -> Catalog:
    return Catalog(SEM, reduce_catalog(courses_data), fetched_at=0.0)


def test_lookup_known_index(catalog):
    info = catalog.get("11425")
    assert info is not None
    assert info.course_string == "01:198:112"
    assert info.number == "01"
    assert info.course_title.upper() == "DATA STRUCTURES"
    assert info.catalog_open is False
    assert catalog.has("11425")


def test_lookup_unknown_index(catalog):
    assert catalog.get("99999") is None
    assert not catalog.has("99999")


def test_instructor_and_meeting_text(catalog):
    info = catalog.get("10052")
    assert info.instructor_text == "Selim, Samah"
    assert info.meeting_text == "Tue/Thu 3:50 PM-5:10 PM @ HH-A2 (CAC)"


def test_online_section_has_no_times_but_clear_text(catalog):
    info = catalog.get("10064")
    assert "Online" in info.meeting_text


def test_section_without_instructor_says_tba(catalog):
    info = catalog.get("10111")
    assert info.instructor_text == "TBA"


def test_cross_listed_alias_resolves_to_primary(catalog):
    # 10053 is listed as cross-listed with 10052 but may not be its own section
    if "10053" in {s.index for h in catalog.search("01:013:120") for s in h.sections}:
        pytest.skip("fixture contains the alias as its own section")
    info = catalog.get("10053")
    assert info is not None and info.via_cross_list
    assert info.index == "10053"
    assert info.course_string == "01:013:120"


@pytest.mark.parametrize("query", ["198:112", "01:198:112", "198 112", "198112", "01.198.112"])
def test_search_by_course_code(catalog, query):
    hits = catalog.search(query)
    assert [h.course_string for h in hits] == ["01:198:112"]
    assert len(hits[0].sections) == 4


def test_search_subject_prefix(catalog):
    assert {h.course_string for h in catalog.search("198")} == {"01:198:111", "01:198:112"}


def test_search_by_title_case_insensitive_all_tokens(catalog):
    assert [h.course_string for h in catalog.search("data struct")] == ["01:198:112"]
    assert catalog.search("data banana") == []


def test_search_by_index_returns_course(catalog):
    hits = catalog.search("11390")
    assert [h.course_string for h in hits] == ["01:198:111"]


def test_search_empty_and_limit(catalog):
    assert catalog.search("   ") == []
    assert len(catalog.search("198", limit=1)) == 1


def test_format_meetings_groups_days_and_orders():
    m = [
        {
            "day": "H",
            "start": "1550",
            "end": "1710",
            "building": "HH",
            "room": "A2",
            "campus": "CAC",
            "mode": "LEC",
        },
        {
            "day": "T",
            "start": "1550",
            "end": "1710",
            "building": "HH",
            "room": "A2",
            "campus": "CAC",
            "mode": "LEC",
        },
        {
            "day": "F",
            "start": "0930",
            "end": "1020",
            "building": "",
            "room": "",
            "campus": "",
            "mode": "REC",
        },
    ]
    assert format_meetings(m) == ("Tue/Thu 3:50 PM-5:10 PM @ HH-A2 (CAC); Fri 9:30 AM-10:20 AM")
    assert format_meetings([]) == "TBA"


def test_reduce_rejects_empty():
    with pytest.raises(CatalogUnavailable):
        reduce_catalog([])
    with pytest.raises(CatalogUnavailable):
        reduce_catalog({"x": 1})


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_store(tmp_path, courses_data, calls, ttl=900.0, status=200, clock=None):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json=courses_data)

    client = RutgersClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return CatalogStore(client, tmp_path / "cache", ttl, clock or Clock())


async def test_store_downloads_once_within_ttl(tmp_path, courses_data):
    calls, clock = [], Clock()
    store = make_store(tmp_path, courses_data, calls, clock=clock)
    first = await store.get(SEM)
    clock.now += 600
    second = await store.get(SEM)
    assert first is second
    assert len(calls) == 1


async def test_store_refreshes_after_ttl(tmp_path, courses_data):
    calls, clock = [], Clock()
    store = make_store(tmp_path, courses_data, calls, clock=clock)
    await store.get(SEM)
    clock.now += 901
    await store.get(SEM)
    assert len(calls) == 2


async def test_store_uses_disk_cache_across_restarts(tmp_path, courses_data):
    calls, clock = [], Clock()
    await make_store(tmp_path, courses_data, calls, clock=clock).get(SEM)
    assert (tmp_path / "cache" / "2026-9-NB.json.gz").exists()
    clock.now += 100
    fresh_store = make_store(tmp_path, courses_data, calls, clock=clock)
    catalog = await fresh_store.get(SEM)
    assert len(calls) == 1
    assert catalog.has("11425")


async def test_store_serves_stale_copy_when_refresh_fails(tmp_path, courses_data):
    calls, clock = [], Clock()
    await make_store(tmp_path, courses_data, calls, clock=clock).get(SEM)
    clock.now += 5000
    failing = make_store(tmp_path, courses_data, calls, status=503, clock=clock)
    assert (await failing.get(SEM)).has("11425")


async def test_store_raises_when_nothing_cached(tmp_path, courses_data):
    store = make_store(tmp_path, courses_data, [], status=503)
    with pytest.raises(CatalogUnavailable):
        await store.get(SEM)


async def test_store_rejects_empty_catalog(tmp_path):
    store = make_store(tmp_path, [], [])
    with pytest.raises(CatalogUnavailable, match="empty"):
        await store.get(SEM)


async def test_corrupt_disk_cache_is_ignored(tmp_path, courses_data):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "2026-9-NB.json.gz").write_bytes(gzip.compress(b"{not json"))
    calls = []
    catalog = await make_store(tmp_path, courses_data, calls).get(SEM)
    assert len(calls) == 1 and catalog.has("11425")
    json.loads(gzip.decompress((cache / "2026-9-NB.json.gz").read_bytes()))
