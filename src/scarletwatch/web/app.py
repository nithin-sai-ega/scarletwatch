"""FastAPI app: dashboard, HTMX partials, /health. No login; bound to 127.0.0.1 by default."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import REPO_URL, __version__
from ..catalog import CatalogUnavailable, SectionInfo
from ..config import Settings
from ..machine import Status
from ..messages import duration_text, humanize, register_link
from ..notifiers import UI_SETTING_KEYS
from ..notifiers.discord import validate_webhook_url
from ..notifiers.ntfy import validate_topic
from ..runtime import configure_logging, open_service
from ..semesters import CAMPUS_NAMES, TERM_NAMES, Semester, SemesterError
from ..service import AddError, Service
from ..store import Watch, utcnow

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


@dataclass
class WatchRow:
    watch: Watch
    info: SectionInfo | None
    badge: str  # open | closed | pending | done
    since: str
    last_open: str | None


def _default_semester(now: datetime) -> tuple[int, int]:
    if now.month >= 10:
        return now.year + 1, 1
    if now.month <= 3:
        return now.year, 1
    return now.year, 9


def _hostname(header: str) -> str:
    """Strip the port: ``localhost:8000`` -> ``localhost``, ``[::1]:8000`` -> ``[::1]``."""
    if header.startswith("["):
        return header.split("]")[0] + "]"
    return header.rsplit(":", 1)[0] if ":" in header else header


def mask(value: str) -> str:
    if not value:
        return ""
    return "•" * 6 + value[-4:] if len(value) > 8 else "•" * len(value)


def create_app(settings: Settings | None = None, service: Service | None = None) -> FastAPI:
    """Create the app. With no ``service``, one is built (and the scheduler started) on startup."""
    from ..config import load_settings

    settings = settings or (service.settings if service else load_settings())
    templates = Jinja2Templates(directory=str(HERE / "templates"))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if service is not None:
            app.state.service = service
            yield
            return
        configure_logging(settings)
        async with open_service(settings) as svc:
            app.state.service = svc
            task = asyncio.create_task(svc.run(), name="scheduler")
            log.info("dashboard at http://%s:%s/", settings.host, settings.port)
            try:
                yield
            finally:
                log.info("shutting down")
                svc.stop()
                try:
                    await asyncio.wait_for(task, timeout=10)
                except (TimeoutError, asyncio.CancelledError):
                    task.cancel()

    app = FastAPI(
        title="ScarletWatch", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def svc(request: Request) -> Service:
        s: Service = request.app.state.service
        return s

    # -- safety: host header + same-origin checks (the dashboard has no login) ---------------

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Response:
        host = _hostname(request.headers.get("host", ""))
        if settings.host in {"127.0.0.1", "localhost", "::1"} and host not in LOOPBACK_HOSTS:
            return Response("Invalid Host header", status_code=400)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return Response("Cross-origin request blocked", status_code=403)
            if not origin and request.headers.get("sec-fetch-site") == "cross-site":
                return Response("Cross-origin request blocked", status_code=403)
        response: Response = await call_next(request)
        return response

    # -- view helpers ---------------------------------------------------------------------

    def rows_for(s: Service) -> list[WatchRow]:
        now = utcnow()
        rows: list[WatchRow] = []
        for w in s.store.list_watches():
            catalog = s.catalogs.peek(w.semester)
            info = catalog.get(w.index) if catalog else None
            if w.done:
                badge = "done"
            elif w.state.status == Status.OPEN:
                badge = "open"
            elif w.state.status == Status.CLOSED:
                badge = "closed"
            else:
                badge = "pending"
            since = (
                humanize(now - w.state.last_changed_at).replace(" ago", "")
                if w.state.last_changed_at and w.state.status
                else "-"
            )
            last_open = None
            for h in s.store.history(w.id, limit=8):
                if h.status == Status.OPEN and h.ended_at is not None:
                    last_open = f"was open for {duration_text(h.ended_at - h.started_at)}"
                    break
            rows.append(WatchRow(w, info, badge, since, last_open))
        return rows

    def health_rows(s: Service) -> list[dict[str, Any]]:
        now = utcnow()
        out = []
        for sem, h in s.health_summary().items():
            age = (now - h.last_success_at) if h.last_success_at else None
            stale = age is None or age > timedelta(seconds=max(s.interval * 3, 180))
            out.append(
                {
                    "semester": sem,
                    "last_ok": humanize(age) if age is not None else "never",
                    "last_attempt": humanize(now - h.last_attempt_at)
                    if h.last_attempt_at
                    else "never",
                    "failures": h.consecutive_failures,
                    "error": h.last_error,
                    "open_count": h.last_list_size,
                    "healthy": h.consecutive_failures == 0
                    and not stale
                    and h.last_success_at is not None,
                }
            )
        return out

    def notify_status(s: Service) -> dict[str, Any]:
        overrides = s.store.all_settings()
        topic = overrides.get("ntfy_topic") or s.settings.ntfy_topic.get_secret_value()
        discord = (
            overrides.get("discord_webhook_url")
            or s.settings.discord_webhook_url.get_secret_value()
        )
        return {
            "ntfy_masked": mask(topic),
            "discord_masked": mask(discord),
            "active": [n.name for n in s.notifiers()],
        }

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx)

    def watches_partial(request: Request, flash: dict[str, str] | None = None) -> HTMLResponse:
        s = svc(request)
        return render(
            request,
            "_watches.html",
            rows=rows_for(s),
            health=health_rows(s),
            flash=flash,
            register_link=lambda w: register_link(s.register_template(), w.semester, [w.index]),
        )

    # -- pages ----------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        s = svc(request)
        year, term = _default_semester(utcnow())
        latest = s.store.list_watches()
        if latest:
            year, term = latest[0].semester.year, latest[0].semester.term
        return render(
            request,
            "index.html",
            version=__version__,
            repo_url=REPO_URL,
            terms=TERM_NAMES,
            campuses=CAMPUS_NAMES,
            default_year=year,
            default_term=term,
            notify=notify_status(s),
            interval=int(s.interval),
        )

    @app.get("/partials/watches", response_class=HTMLResponse)
    async def watches(request: Request) -> HTMLResponse:
        return watches_partial(request)

    def parse_sem(year: str, term: str, campus: str) -> Semester:
        return Semester.of(year, term, campus)

    @app.get("/search", response_class=HTMLResponse)
    async def search(
        request: Request, q: str = "", year: str = "", term: str = "", campus: str = "NB"
    ) -> HTMLResponse:
        s = svc(request)
        if not q.strip():
            return render(request, "_results.html", hits=None, error=None, query="")
        try:
            sem = parse_sem(year, term, campus)
            catalog = await s.catalogs.get(sem)
        except (SemesterError, CatalogUnavailable) as exc:
            return render(request, "_results.html", hits=None, error=str(exc), query=q)
        hits = catalog.search(q, limit=15)
        watched = {w.index for w in s.store.watches_for(sem, active_only=False)}
        return render(
            request,
            "_results.html",
            hits=hits,
            error=None,
            query=q,
            sem=sem,
            watched=watched,
            total=len(hits),
        )

    @app.post("/watches", response_class=HTMLResponse)
    async def add_watch(
        request: Request,
        index: Annotated[str, Form()],
        year: Annotated[str, Form()],
        term: Annotated[str, Form()],
        campus: Annotated[str, Form()] = "NB",
        repeat_minutes: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        s = svc(request)
        messages: list[str] = []
        errors: list[str] = []
        try:
            sem = parse_sem(year, term, campus)
            repeat = int(repeat_minutes) if repeat_minutes.strip() else None
            # Accept "10195, 10201 10202" pasted in one go.
            raw_indexes = index.replace(",", " ").split()
            for raw, item in await s.add_many(sem, raw_indexes, repeat_minutes=repeat):
                if isinstance(item, AddError):
                    errors.append(str(item))
                    continue
                name = item.info.display_name if item.info else f"index {raw}"
                if not item.created:
                    messages.append(f"Already watching {name}.")
                elif item.already_open:
                    messages.append(f"{name} is ALREADY OPEN right now: register immediately!")
                else:
                    messages.append(f"Now watching {name}.")
        except (SemesterError, ValueError) as exc:
            errors.append(str(exc))
        flash = {"ok": " ".join(messages), "error": " ".join(errors)}
        return watches_partial(request, flash)

    @app.post("/watches/{watch_id}/remove", response_class=HTMLResponse)
    async def remove(request: Request, watch_id: int) -> HTMLResponse:
        svc(request).store.remove_watch(watch_id)
        return watches_partial(request)

    @app.post("/watches/{watch_id}/done", response_class=HTMLResponse)
    async def done(request: Request, watch_id: int) -> HTMLResponse:
        s = svc(request)
        w = s.store.get_watch(watch_id)
        if w is not None:
            s.store.set_done(watch_id, not w.done)
        return watches_partial(request)

    @app.post("/settings/notifications", response_class=HTMLResponse)
    async def save_notifications(
        request: Request,
        ntfy_topic: Annotated[str, Form()] = "",
        discord_webhook_url: Annotated[str, Form()] = "",
        clear: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        s = svc(request)
        error = ""
        if clear in UI_SETTING_KEYS:
            s.store.set_setting(clear, "")
        try:
            if ntfy_topic.strip():
                s.store.set_setting("ntfy_topic", validate_topic(ntfy_topic))
            if discord_webhook_url.strip():
                s.store.set_setting(
                    "discord_webhook_url", validate_webhook_url(discord_webhook_url)
                )
        except ValueError as exc:
            error = str(exc)
        return render(request, "_notify.html", notify=notify_status(s), error=error, results=None)

    @app.post("/notifications/test", response_class=HTMLResponse)
    async def test_notify(request: Request) -> HTMLResponse:
        s = svc(request)
        results = await s.send_test()
        return render(request, "_notify.html", notify=notify_status(s), error="", results=results)

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        s = svc(request)
        now = utcnow()
        semesters = []
        for row in health_rows(s):
            semesters.append(
                {
                    "semester": row["semester"].key,
                    "healthy": row["healthy"],
                    "last_success": row["last_ok"],
                    "consecutive_failures": row["failures"],
                    "last_error": row["error"],
                    "open_sections": row["open_count"],
                }
            )
        ok = all(x["healthy"] for x in semesters)
        return JSONResponse(
            {
                "status": "ok" if ok else "degraded",
                "version": __version__,
                "uptime_seconds": int((now - s.started_at).total_seconds()),
                "active_watches": len(s.store.list_watches(active_only=True)),
                "notifiers": [n.name for n in s.notifiers()],
                "semesters": semesters,
            },
            status_code=200,
        )

    return app
