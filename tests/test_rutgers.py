import httpx
import pytest

from scarletwatch import USER_AGENT
from scarletwatch.config import MIN_POLL_INTERVAL
from scarletwatch.rutgers import (
    BACKOFF_CAP,
    FetchError,
    RutgersClient,
    compute_backoff,
    parse_retry_after,
    poll_delay,
)
from scarletwatch.semesters import Semester

SEM = Semester(2026, 9, "NB")


def client_for(handler) -> RutgersClient:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"User-Agent": USER_AGENT}
    )
    return RutgersClient(http)


async def test_open_sections_request_shape(open_data):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=open_data)

    client = client_for(handler)
    assert await client.open_sections(SEM) == open_data
    req = seen[0]
    assert req.url.path == "/soc/api/openSections.json"
    assert dict(req.url.params) == {"year": "2026", "term": "9", "campus": "NB"}
    assert req.headers["user-agent"].startswith("course-snipe/")
    assert "@" not in req.headers["user-agent"]


async def test_courses_request_path(courses_data):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/soc/api/courses.json"
        return httpx.Response(200, json=courses_data)

    assert await client_for(handler).courses(SEM) == courses_data


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(429, headers={"Retry-After": "120"}), "rate_limited"),
        (httpx.Response(500), "server"),
        (httpx.Response(503, headers={"Retry-After": "5"}), "server"),
        (httpx.Response(400), "client"),
        (httpx.Response(200, content=b"<html>not json"), "malformed"),
    ],
)
async def test_errors_are_classified(response, kind):
    client = client_for(lambda request: response)
    with pytest.raises(FetchError) as info:
        await client.open_sections(SEM)
    assert info.value.kind == kind


async def test_retry_after_is_carried():
    client = client_for(lambda r: httpx.Response(429, headers={"Retry-After": "120"}))
    with pytest.raises(FetchError) as info:
        await client.open_sections(SEM)
    assert info.value.retry_after == 120


async def test_timeout_and_network_errors():
    def timeout(request):
        raise httpx.ReadTimeout("slow")

    def refused(request):
        raise httpx.ConnectError("nope")

    with pytest.raises(FetchError) as t:
        await client_for(timeout).open_sections(SEM)
    assert t.value.kind == "timeout"
    with pytest.raises(FetchError) as n:
        await client_for(refused).open_sections(SEM)
    assert n.value.kind == "network"


def test_parse_retry_after():
    assert parse_retry_after("30") == 30
    assert parse_retry_after(None) is None
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert parse_retry_after("-5") is None


def test_backoff_grows_and_caps():
    mid = lambda: 0.5  # noqa: E731  (jitter factor exactly 1.0)
    delays = [compute_backoff(n, rng=mid) for n in range(1, 8)]
    assert delays[0] == 30
    assert delays[1] == 60
    assert delays == sorted(delays)
    assert delays[-1] == BACKOFF_CAP
    assert compute_backoff(0) == 0


def test_backoff_honors_retry_after():
    assert compute_backoff(1, retry_after=300, rng=lambda: 0.5) == 300
    assert compute_backoff(1, retry_after=1, rng=lambda: 0.5) == 30
    assert compute_backoff(1, retry_after=10**9, rng=lambda: 0.5) == 3600


def test_poll_delay_has_floor_and_jitter():
    assert poll_delay(5, rng=lambda: 0.0) >= MIN_POLL_INTERVAL * 0.95
    low, high = poll_delay(60, rng=lambda: 0.0), poll_delay(60, rng=lambda: 1.0)
    assert low < 60 < high
    assert high <= 66
