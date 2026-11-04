"""Test harness: a Service wired to a mocked Rutgers + ntfy, with controllable clocks."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from scarletwatch.catalog import CatalogStore
from scarletwatch.config import Settings
from scarletwatch.rutgers import RutgersClient
from scarletwatch.service import Service
from scarletwatch.store import Store

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


class FakeWorld:
    """Mutable fake internet. Records every request that is made."""

    def __init__(self, courses: list[dict[str, Any]], open_list: list[str]) -> None:
        self.courses = courses
        self.open_by_term: dict[str, Any] = {"9": list(open_list), "1": list(open_list)}
        self.open_status = 200
        self.open_headers: dict[str, str] = {}
        self.ntfy_status = 200
        self.requests: list[httpx.Request] = []
        self.mono = 1000.0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/openSections.json"):
            if self.open_status != 200:
                return httpx.Response(self.open_status, headers=self.open_headers)
            return httpx.Response(200, json=self.open_by_term[request.url.params["term"]])
        if path.endswith("/courses.json"):
            return httpx.Response(200, json=self.courses)
        if request.url.host == "ntfy.sh":
            return httpx.Response(self.ntfy_status)
        return httpx.Response(404)

    def count(self, suffix: str) -> int:
        return sum(1 for r in self.requests if r.url.path.endswith(suffix))

    def ntfy_posts(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.host == "ntfy.sh"]

    def advance(self, seconds: float) -> None:
        self.mono += seconds


def make_service(
    tmp_path: Path,
    world: FakeWorld,
    monkeypatch: Any,
    *,
    ntfy_topic: str | None = "test-topic",
    **env: str,
) -> tuple[Service, Store]:
    if ntfy_topic:
        monkeypatch.setenv("SCARLETWATCH_NTFY_TOPIC", ntfy_topic)
    for key, value in env.items():
        monkeypatch.setenv(f"SCARLETWATCH_{key.upper()}", value)
    settings = Settings(_env_file=None, data_dir=tmp_path)  # type: ignore[call-arg]
    http = httpx.AsyncClient(transport=httpx.MockTransport(world.handler))
    rutgers = RutgersClient(http)
    wall = {"t": T0}

    def now() -> datetime:
        return wall["t"] + timedelta(seconds=world.mono - 1000.0)

    catalogs = CatalogStore(rutgers, settings.catalog_dir, 3600.0, clock=lambda: now().timestamp())
    store = Store(":memory:")
    service = Service(
        settings,
        store,
        rutgers,
        catalogs,
        http,
        now=now,
        monotonic=lambda: world.mono,
        rng=lambda: 0.5,
    )
    return service, store
