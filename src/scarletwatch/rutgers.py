"""HTTP client for the two Rutgers Schedule of Classes JSON endpoints.

Only these two public endpoints are used; there is no login and no HTML scraping.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from typing import Any, Literal

import httpx

from . import USER_AGENT
from .config import MIN_POLL_INTERVAL
from .semesters import Semester

BASE_URL = "https://classes.rutgers.edu/soc/api"
OPEN_SECTIONS_URL = f"{BASE_URL}/openSections.json"
COURSES_URL = f"{BASE_URL}/courses.json"

ErrorKind = Literal["timeout", "network", "rate_limited", "server", "client", "malformed"]

BACKOFF_BASE = 30.0
BACKOFF_CAP = 900.0


class FetchError(Exception):
    """A request failed in a way that should trigger backoff."""

    def __init__(self, kind: ErrorKind, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After in seconds. HTTP-date form is ignored (treated as absent)."""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def compute_backoff(
    failures: int,
    retry_after: float | None = None,
    *,
    base: float = BACKOFF_BASE,
    cap: float = BACKOFF_CAP,
    rng: Callable[[], float] = random.random,
) -> float:
    """Exponential backoff with a cap and +/-10% jitter; honors Retry-After (also capped at 1h)."""
    if failures <= 0:
        return 0.0
    delay = min(cap, base * float(2 ** (failures - 1)))
    delay *= 0.9 + 0.2 * rng()
    if retry_after is not None:
        delay = max(delay, min(retry_after, 3600.0))
    return delay


def poll_delay(interval: float, rng: Callable[[], float] = random.random) -> float:
    """Poll interval with small jitter; never below the 30 s minimum."""
    base = max(interval, MIN_POLL_INTERVAL)
    return base * (0.95 + 0.10 * rng())


class RutgersClient:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        user_agent: str = USER_AGENT,
        timeout: float = 20.0,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get_json(self, url: str, semester: Semester) -> Any:
        try:
            response = await self._client.get(url, params=semester.params)
        except httpx.TimeoutException as exc:
            raise FetchError("timeout", f"timeout fetching {url}") from exc
        except httpx.HTTPError as exc:
            name = type(exc).__name__
            raise FetchError("network", f"network error fetching {url}: {name}") from exc

        status = response.status_code
        if status == 429:
            raise FetchError(
                "rate_limited",
                "HTTP 429 from Rutgers",
                parse_retry_after(response.headers.get("Retry-After")),
            )
        if status >= 500:
            raise FetchError(
                "server",
                f"HTTP {status} from Rutgers",
                parse_retry_after(response.headers.get("Retry-After")),
            )
        if status >= 400:
            raise FetchError("client", f"HTTP {status} from Rutgers for {semester.label}")
        try:
            return json.loads(response.content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise FetchError("malformed", f"response from {url} is not valid JSON") from exc

    async def open_sections(self, semester: Semester) -> Any:
        """Raw decoded JSON of openSections.json. Validate with ``guards`` before use."""
        return await self._get_json(OPEN_SECTIONS_URL, semester)

    async def courses(self, semester: Semester) -> Any:
        """Raw decoded JSON of courses.json (large; callers should cache it)."""
        return await self._get_json(COURSES_URL, semester)
