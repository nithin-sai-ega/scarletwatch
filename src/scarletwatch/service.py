"""The long-running service: polls each active semester once per cycle and dispatches alerts.

Politeness rules enforced here:
* exactly one openSections request per (year, term, campus) per cycle, however many watches;
* semesters with no active watches are not polled at all;
* never more often than every 30 s per semester (also for add-triggered checks);
* exponential backoff (honoring Retry-After) on errors and suspicious data;
* the 21 MB course catalog is cached and refreshed lazily, never per poll.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import httpx

from . import engine
from .catalog import Catalog, CatalogStore, CatalogUnavailable, SectionInfo
from .config import MIN_POLL_INTERVAL, Settings
from .guards import OpenListGuard, SuspiciousData
from .logging_setup import register_secrets
from .machine import EventKind, Status
from .messages import make_test_notification, open_notification
from .notifiers import DeliveryResult, Notification, Notifier, build_notifiers, dispatch
from .rutgers import FetchError, RutgersClient, compute_backoff, poll_delay
from .semesters import Semester
from .store import Health, Store, Watch, utcnow

log = logging.getLogger(__name__)

_INDEX_RE = re.compile(r"^\d{5}$")
CATALOG_RETRY_SECONDS = 300.0


class AddError(Exception):
    """A watch could not be added; the message is safe to show to the user."""


@dataclass
class PollOutcome:
    semester: Semester
    ok: bool
    skipped: bool = False
    error: str | None = None
    open_count: int | None = None


@dataclass
class AddResult:
    watch: Watch
    info: SectionInfo | None
    created: bool
    already_open: bool | None  # None = status unknown (poll failed or throttled)


@dataclass
class _SemesterState:
    guard: OpenListGuard = field(default_factory=OpenListGuard)
    next_due: float = 0.0
    last_request: float | None = None
    failures: int = 0
    catalog_retry_at: float = 0.0


class Service:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        rutgers: RutgersClient,
        catalogs: CatalogStore,
        http: httpx.AsyncClient,
        *,
        now: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.settings = settings
        self.store = store
        self.rutgers = rutgers
        self.catalogs = catalogs
        self.http = http
        self._now = now
        self._mono = monotonic
        self._rng = rng
        self._state: dict[str, _SemesterState] = {}
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self.started_at = now()

    # -- notifiers ------------------------------------------------------------------------

    def notifiers(self) -> list[Notifier]:
        overrides = self.store.all_settings()
        register_secrets([*self.settings.secret_values(), *overrides.values()])
        return build_notifiers(self.settings, overrides, self.http)

    def register_template(self) -> str:
        return self.settings.register_url_template

    async def send_test(self) -> list[DeliveryResult]:
        notifiers = self.notifiers()
        if not notifiers:
            return []
        return await dispatch(notifiers, make_test_notification())

    async def send_preview(self, semester: Semester, index: str) -> list[DeliveryResult]:
        """Send a clearly labelled SAMPLE of the real alert for a real section (no state change)."""
        index = index.strip()
        if not _INDEX_RE.match(index):
            raise AddError(f"{index!r} is not a 5-digit index number.")
        try:
            info = (await self.catalogs.get(semester)).get(index)
        except CatalogUnavailable:
            info = None
        note = open_notification(
            semester, index, info, timedelta(seconds=0), self.register_template()
        )
        note = replace(
            note,
            title=f"SAMPLE (not a real opening) - {note.title}",
            body="This is a preview of what a real alert looks like. Nothing has opened."
            + "\n\n"
            + note.body,
            kind="sample",
        )
        notifiers = self.notifiers()
        return await dispatch(notifiers, note) if notifiers else []

    # -- polling --------------------------------------------------------------------------

    def _sem_state(self, semester: Semester) -> _SemesterState:
        return self._state.setdefault(semester.key, _SemesterState())

    @property
    def interval(self) -> float:
        return max(self.settings.poll_interval, MIN_POLL_INTERVAL)

    async def poll_semester(
        self, semester: Semester, *, only_ids: set[int] | None = None
    ) -> PollOutcome:
        """One request for this semester; applies the result to the watches."""
        st = self._sem_state(semester)
        mono = self._mono()
        if st.last_request is not None and mono - st.last_request < MIN_POLL_INTERVAL:
            return PollOutcome(semester, ok=False, skipped=True)
        st.last_request = mono

        health = self.store.get_health(semester)
        attempted = self._now()
        error: str | None = None
        retry_after: float | None = None
        open_set: frozenset[str] | None = None
        try:
            raw = await self.rutgers.open_sections(semester)
            open_set = st.guard.check(raw)
        except FetchError as exc:
            error = f"{exc.kind}: {exc}"
            retry_after = exc.retry_after
        except SuspiciousData as exc:
            error = f"suspicious data: {exc}"

        if open_set is None:
            st.failures += 1
            delay = max(
                poll_delay(self.interval, self._rng), compute_backoff(st.failures, retry_after)
            )
            st.next_due = self._mono() + delay
            log.warning("poll of %s skipped (%s); retrying in %.0fs", semester.label, error, delay)
            self.store.save_health(
                Health(
                    semester.key,
                    attempted,
                    health.last_success_at,
                    error,
                    st.failures,
                    health.last_list_size,
                )
            )
            return PollOutcome(semester, ok=False, error=error)

        st.failures = 0
        st.next_due = self._mono() + poll_delay(self.interval, self._rng)
        self.store.save_health(Health(semester.key, attempted, attempted, None, 0, len(open_set)))

        catalog = await self._catalog_if_due(semester, st)
        result = engine.apply_poll(
            self.store, semester, open_set, self._now(), catalog, only_ids=only_ids
        )
        for watch in result.newly_missing:
            log.warning("index %s no longer appears in the %s catalog", watch.index, semester.label)
            await self._send(
                Notification(
                    title=f"Index {watch.index} vanished from the catalog",
                    body=(
                        f"Index {watch.index} ({semester.label}) is no longer listed in the "
                        "Schedule of Classes. The section may have been cancelled or renumbered. "
                        "Check it and re-add the new index if needed."
                    ),
                    urgent=False,
                    kind="section_missing",
                )
            )
        for alert in result.alerts:
            await self._handle_alert(alert, semester, catalog)
        return PollOutcome(semester, ok=True, open_count=len(open_set))

    async def _catalog_if_due(self, semester: Semester, st: _SemesterState) -> Catalog | None:
        """Return a catalog, refreshing at most once per TTL (and retrying failures slowly)."""
        cached = self.catalogs.peek(semester)
        fresh = cached is not None and (
            self._now().timestamp() - cached.fetched_at < self.settings.catalog_ttl_minutes * 60
        )
        if fresh or self._mono() < st.catalog_retry_at:
            return cached
        try:
            return await self.catalogs.get(semester)
        except CatalogUnavailable as exc:
            st.catalog_retry_at = self._mono() + CATALOG_RETRY_SECONDS
            log.warning("course catalog unavailable for %s: %s", semester.label, exc)
            return cached

    async def _handle_alert(
        self, alert: engine.Alert, semester: Semester, catalog: Catalog | None
    ) -> None:
        kind = alert.event.kind
        watch = alert.watch
        if kind == EventKind.CLOSED:
            log.info("%s closed again after %s", watch.index, alert.event.open_duration)
            return
        if kind == EventKind.ALREADY_OPEN:
            return
        info = catalog.get(watch.index) if catalog else None
        note = open_notification(
            semester,
            watch.index,
            info,
            alert.open_for,
            self.register_template(),
            repeat=kind == EventKind.REPEAT,
        )
        results = await self._send(note)
        if any(r.ok for r in results):
            engine.confirm_notified(self.store, watch.id, self._now())
            log.info(
                "notified: %s opened (%s)",
                watch.index,
                ", ".join(r.notifier for r in results if r.ok),
            )
        else:
            engine.confirm_notify_failed(self.store, watch.id)
            log.error("section %s is OPEN but no notification could be delivered", watch.index)

    async def _send(self, note: Notification) -> list[DeliveryResult]:
        notifiers = self.notifiers()
        if not notifiers:
            log.warning("no notifiers configured; set an ntfy topic (see README)")
            return []
        return await dispatch(notifiers, note)

    # -- adding watches ---------------------------------------------------------------------

    async def _prepare(
        self, semester: Semester, index: str, repeat_minutes: int | None, force: bool
    ) -> tuple[str, SectionInfo | None]:
        """Validate one index (format, then existence in the catalog)."""
        index = index.strip()
        if not _INDEX_RE.match(index):
            raise AddError(
                f"{index!r} is not a 5-digit index number. The index (e.g. 10195) is not the "
                "section number (e.g. 01)."
            )
        if repeat_minutes is not None and repeat_minutes < 1:
            raise AddError("Repeat interval must be at least 1 minute.")
        info: SectionInfo | None = None
        try:
            catalog = await self.catalogs.get(semester)
            info = catalog.get(index)
            if info is None and not force:
                raise AddError(
                    f"Index {index} does not exist in {semester.label}. Check the index, term "
                    "and campus (use `snipe search` to find it)."
                )
        except CatalogUnavailable as exc:
            if not force:
                raise AddError(
                    f"Could not validate the index: {exc} (use --force to skip)"
                ) from exc
        return index, info

    async def add_many(
        self,
        semester: Semester,
        indexes: list[str],
        *,
        repeat_minutes: int | None = None,
        force: bool = False,
    ) -> list[tuple[str, AddResult | AddError]]:
        """Add several watches with a single status check (one request for the semester)."""
        outcomes: list[tuple[str, AddResult | AddError]] = []
        created_ids: set[int] = set()
        for raw in indexes:
            try:
                index, info = await self._prepare(semester, raw, repeat_minutes, force)
            except AddError as exc:
                outcomes.append((raw, exc))
                continue
            watch, created = self.store.add_watch(semester, index, repeat_minutes)
            if created:
                created_ids.add(watch.id)
            outcomes.append((index, AddResult(watch, info, created, None)))
        if created_ids:
            outcome = await self.poll_semester(semester, only_ids=created_ids)
            self.wake()
            if outcome.ok:
                refreshed: list[tuple[str, AddResult | AddError]] = []
                for index, item in outcomes:
                    if isinstance(item, AddResult) and item.watch.id in created_ids:
                        watch = self.store.get_watch(item.watch.id) or item.watch
                        item = AddResult(
                            watch, item.info, item.created, watch.state.status == Status.OPEN
                        )
                    refreshed.append((index, item))
                outcomes = refreshed
        return outcomes

    async def add_watch(
        self,
        semester: Semester,
        index: str,
        *,
        repeat_minutes: int | None = None,
        force: bool = False,
    ) -> AddResult:
        """Add one watch; raises AddError if it is invalid."""
        ((_, item),) = await self.add_many(
            semester, [index], repeat_minutes=repeat_minutes, force=force
        )
        if isinstance(item, AddError):
            raise item
        return item

    # -- scheduler loop ---------------------------------------------------------------------

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def tick(self) -> float:
        """Poll every due semester; return seconds until something is due again."""
        active = self.store.active_semesters()
        for key in [k for k in self._state if k not in {s.key for s in active}]:
            del self._state[key]  # no active watches -> stop polling and forget
        wait = 30.0
        for semester in sorted(active):
            st = self._sem_state(semester)
            if self._mono() >= st.next_due:
                try:
                    await self.poll_semester(semester)
                except Exception:
                    # Never let one semester's bug stop the others or the loop.
                    log.exception("unexpected error polling %s", semester.label)
                    st.failures += 1
                    st.next_due = self._mono() + compute_backoff(st.failures)
            wait = min(wait, max(0.5, st.next_due - self._mono()))
        return wait

    async def run(self) -> None:
        log.info("scheduler started (interval %.0fs)", self.interval)
        while not self._stop.is_set():
            # Clear *before* ticking so a wake()/stop() that arrives mid-tick is not lost.
            self._wake.clear()
            wait = await self.tick()
            if self._stop.is_set():
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
        log.info("scheduler stopped")

    def health_summary(self) -> dict[Semester, Health]:
        return {s: self.store.get_health(s) for s in sorted(self.store.active_semesters())}
