"""Command line interface: ``snipe``."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from typing import Annotated, TypeVar

import typer

from . import __version__
from .catalog import CatalogUnavailable
from .config import Settings, load_settings
from .machine import Status
from .messages import duration_text, humanize, register_link
from .runtime import configure_logging, open_service
from .semesters import Semester, SemesterError
from .service import AddError, Service
from .store import Store, Watch, utcnow

app = typer.Typer(
    name="snipe",
    help="ScarletWatch: get notified when a closed Rutgers section opens. "
    "Unofficial; never registers for you.",
    no_args_is_help=True,
    add_completion=False,
)

T = TypeVar("T")

YearOpt = Annotated[int | None, typer.Option("--year", "-y", help="Semester year, e.g. 2026.")]
TermOpt = Annotated[
    str | None, typer.Option("--term", "-t", help="fall, spring, summer, winter, or 9/1/7/0.")
]
CampusOpt = Annotated[
    str, typer.Option("--campus", "-c", help="NB, NK, CM, or a name like 'New Brunswick'.")
]
SemesterOpt = Annotated[
    str | None,
    typer.Option("--semester", "-s", help="Free text instead of year/term/campus: 'fall 2026 NB'."),
]


def _fail(message: str) -> typer.Exit:
    typer.secho(f"Error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(1)


def _semester(year: int | None, term: str | None, campus: str, text: str | None) -> Semester:
    try:
        if text:
            return Semester.parse(text)
        if year is None or term is None:
            raise SemesterError("Give --year and --term (or --semester 'fall 2026 NB').")
        return Semester.of(year, term, campus)
    except SemesterError as exc:
        raise _fail(str(exc)) from exc


def _settings() -> Settings:
    try:
        settings = load_settings()
    except Exception as exc:
        raise _fail(f"Invalid configuration: {exc}") from exc
    configure_logging(settings)
    return settings


def _with_service(settings: Settings, fn: Callable[[Service], Awaitable[T]]) -> T:
    async def go() -> T:
        async with open_service(settings) as service:
            return await fn(service)

    return asyncio.run(go())


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"scarletwatch {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show version and exit.",
            callback=_print_version,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """ScarletWatch: get notified when a closed Rutgers section opens."""


@app.command()
def add(
    indexes: Annotated[list[str], typer.Argument(help="One or more 5-digit index numbers.")],
    year: YearOpt = None,
    term: TermOpt = None,
    campus: CampusOpt = "NB",
    semester: SemesterOpt = None,
    repeat: Annotated[
        int | None,
        typer.Option("--repeat", help="Re-notify every N minutes while the section stays open."),
    ] = None,
    force: Annotated[
        bool, typer.Option(help="Add even if the index is not in the catalog.")
    ] = False,
) -> None:
    """Watch one or more sections (validated against the course catalog)."""
    sem = _semester(year, term, campus, semester)
    settings = _settings()

    async def go(service: Service) -> int:
        failures = 0
        for index, item in await service.add_many(sem, indexes, repeat_minutes=repeat, force=force):
            if isinstance(item, AddError):
                typer.secho(f"  {index}: {item}", fg=typer.colors.RED, err=True)
                failures += 1
                continue
            name = item.info.display_name if item.info else f"index {index}"
            if not item.created:
                typer.echo(f"  {index}: already watching {name}")
            elif item.already_open:
                link = register_link(settings.register_url_template, sem, [index])
                typer.secho(f"  {index}: {name} is ALREADY OPEN right now!", fg=typer.colors.GREEN)
                typer.echo(f"      Register: {link}")
            else:
                state = "closed" if item.already_open is False else "status unknown yet"
                typer.echo(f"  {index}: watching {name} ({state})")
            if item.info:
                typer.echo(f"      {item.info.instructor_text} | {item.info.meeting_text}")
        return failures

    typer.echo(f"Adding to {sem.label}:")
    failures = _with_service(settings, go)
    typer.echo("Run `snipe run` to start watching (leave it running).")
    if failures:
        raise typer.Exit(1)


def _find(store: Store, index: str, sem: Semester | None) -> list[Watch]:
    return [
        w for w in store.list_watches() if w.index == index and (sem is None or w.semester == sem)
    ]


def _resolve_optional_sem(
    year: int | None, term: str | None, campus: str, text: str | None
) -> Semester | None:
    if text or year is not None or term is not None:
        return _semester(year, term, campus, text)
    return None


@app.command()
def remove(
    indexes: Annotated[list[str], typer.Argument(help="Index numbers to stop watching.")],
    year: YearOpt = None,
    term: TermOpt = None,
    campus: CampusOpt = "NB",
    semester: SemesterOpt = None,
) -> None:
    """Stop watching (deletes the watch and its history)."""
    _act(indexes, year, term, campus, semester, "removed", lambda s, w: s.remove_watch(w.id))


@app.command()
def done(
    indexes: Annotated[list[str], typer.Argument(help="Index numbers to mark done.")],
    year: YearOpt = None,
    term: TermOpt = None,
    campus: CampusOpt = "NB",
    semester: SemesterOpt = None,
) -> None:
    """Mark watches as done (kept in the list, no longer polled)."""
    _act(indexes, year, term, campus, semester, "marked done", lambda s, w: s.set_done(w.id, True))


def _act(
    indexes: list[str],
    year: int | None,
    term: str | None,
    campus: str,
    text: str | None,
    verb: str,
    action: Callable[[Store, Watch], object],
) -> None:
    sem = _resolve_optional_sem(year, term, campus, text)
    settings = _settings()
    store = Store(settings.db_path)
    code = 0
    try:
        for index in indexes:
            matches = _find(store, index, sem)
            if not matches:
                typer.secho(f"  {index}: not found", fg=typer.colors.RED, err=True)
                code = 1
            elif len(matches) > 1:
                typer.secho(
                    f"  {index}: watched in several semesters; add --semester to choose "
                    f"({', '.join(m.semester.label for m in matches)})",
                    fg=typer.colors.RED,
                    err=True,
                )
                code = 1
            else:
                action(store, matches[0])
                typer.echo(f"  {index}: {verb} ({matches[0].semester.label})")
    finally:
        store.close()
    if code:
        raise typer.Exit(code)


@app.command("list")
def list_watches() -> None:
    """List all watches."""
    settings = _settings()
    store = Store(settings.db_path)
    try:
        watches = store.list_watches()
    finally:
        store.close()
    if not watches:
        typer.echo("No watches yet. Try: snipe add 10195 --year 2026 --term fall")
        return
    for w in watches:
        status = "done" if w.done else (w.state.status.value if w.state.status else "unchecked")
        typer.echo(f"{w.index}  {status:<9} {w.semester.label}")


@app.command()
def status() -> None:
    """Show watches with live-ish status and poll health (from the database)."""
    settings = _settings()
    store = Store(settings.db_path)
    now = utcnow()
    try:
        watches = store.list_watches()
        if not watches:
            typer.echo("No watches yet. Try: snipe add 10195 --year 2026 --term fall")
            return
        for sem in sorted({w.semester for w in watches if not w.done}):
            h = store.get_health(sem)
            ok = h.last_success_at is not None and h.consecutive_failures == 0
            colour = typer.colors.GREEN if ok else typer.colors.YELLOW
            last = humanize(now - h.last_success_at) if h.last_success_at else "never"
            line = f"{sem.label}: last good check {last}"
            if h.consecutive_failures:
                line += f" | {h.consecutive_failures} failed in a row ({h.last_error})"
            typer.secho(line, fg=colour)
        typer.echo("")
        for w in watches:
            label = "DONE" if w.done else (w.state.status.value.upper() if w.state.status else "?")
            since = ""
            if w.state.last_changed_at and w.state.status and not w.done:
                since = f" for {duration_text(now - w.state.last_changed_at)}"
            row_colour = typer.colors.GREEN if label == "OPEN" else None
            sent = f"notified {w.state.notify_count}x"
            typer.secho(
                f"{w.index}  {label}{since}  | {w.semester.label} | {sent}",
                fg=row_colour,
            )
            if w.missing_since:
                typer.secho(
                    "      warning: no longer in the course catalog", fg=typer.colors.YELLOW
                )
            last_open = next(
                (h for h in store.history(w.id, limit=8) if h.status == Status.OPEN and h.ended_at),
                None,
            )
            if last_open and last_open.ended_at:
                dur = duration_text(last_open.ended_at - last_open.started_at)
                typer.echo(f"      last opening lasted {dur}")
    finally:
        store.close()
    typer.echo("\n(This reads the database. Use `snipe run` to actually poll.)")


@app.command("test-notify")
def test_notify(
    preview: Annotated[
        str | None,
        typer.Option(
            "--preview",
            help="Instead of a plain test, send a labelled SAMPLE alert for this index "
            "(needs --year/--term or --semester). Shows exactly what a real alert looks like.",
        ),
    ] = None,
    year: YearOpt = None,
    term: TermOpt = None,
    campus: CampusOpt = "NB",
    semester: SemesterOpt = None,
) -> None:
    """Send a test notification through every configured notifier."""
    sem = _semester(year, term, campus, semester) if preview else None
    settings = _settings()

    async def go(service: Service) -> int:
        if preview and sem:
            try:
                results = await service.send_preview(sem, preview)
            except AddError as exc:
                raise _fail(str(exc)) from exc
        else:
            results = await service.send_test()
        if not results:
            typer.secho(
                "No notifier configured. Set SCARLETWATCH_NTFY_TOPIC (see README) and retry.",
                fg=typer.colors.RED,
                err=True,
            )
            return 2
        bad = 0
        for r in results:
            if r.ok:
                typer.secho(f"  {r.notifier}: sent", fg=typer.colors.GREEN)
            else:
                typer.secho(f"  {r.notifier}: FAILED - {r.error}", fg=typer.colors.RED, err=True)
                bad += 1
        return 1 if bad else 0

    code = _with_service(settings, go)
    if code:
        raise typer.Exit(code)


@app.command()
def search(
    query: Annotated[
        str, typer.Argument(help='Course code or title, e.g. "198:112" or "calculus".')
    ],
    year: YearOpt = None,
    term: TermOpt = None,
    campus: CampusOpt = "NB",
    semester: SemesterOpt = None,
    limit: Annotated[int, typer.Option(help="Max courses to show.")] = 10,
) -> None:
    """Search the course catalog and show index numbers."""
    sem = _semester(year, term, campus, semester)
    settings = _settings()

    async def go(service: Service) -> None:
        try:
            catalog = await service.catalogs.get(sem)
        except CatalogUnavailable as exc:
            raise _fail(str(exc)) from exc
        hits = catalog.search(query, limit=limit)
        if not hits:
            typer.echo(f"No courses matched {query!r} in {sem.label}.")
            return
        for hit in hits:
            cred = f" ({hit.credits} cr)" if hit.credits else ""
            typer.secho(f"{hit.course_string}  {hit.title.title()}{cred}", bold=True)
            for s in hit.sections:
                state = "open  " if s.catalog_open else "closed"
                colour = typer.colors.GREEN if s.catalog_open else None
                typer.secho(
                    f"   {s.index}  §{s.number}  {state}  {s.instructor_text} | {s.meeting_text}",
                    fg=colour,
                )
        typer.echo("\nStatus is from the cached catalog (may be up to an hour old).")

    _with_service(settings, go)


@app.command()
def run(
    host: Annotated[str | None, typer.Option(help="Dashboard bind address.")] = None,
    port: Annotated[int | None, typer.Option(help="Dashboard port.")] = None,
    no_web: Annotated[bool, typer.Option("--no-web", help="Poll only; no dashboard.")] = False,
) -> None:
    """Start polling (and the dashboard). Leave this running."""
    settings = _settings()
    if host:
        settings.host = host
    if port:
        settings.port = port
    if (
        settings.host not in {"127.0.0.1", "localhost", "::1"}
        and not no_web
        and not os.environ.get("SCARLETWATCH_SUPPRESS_BIND_WARNING")
    ):
        typer.secho(
            f"Warning: the dashboard has NO login and is bound to {settings.host}. "
            "Anyone who can reach it can edit your watches. Put it behind a VPN or reverse proxy "
            "with authentication.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    if no_web:
        _run_headless(settings)
        return
    import uvicorn

    from .web import create_app

    typer.echo(f"ScarletWatch {__version__}: dashboard at http://{settings.host}:{settings.port}/")
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
    )


def _run_headless(settings: Settings) -> None:
    async def go() -> None:
        async with open_service(settings) as service:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(sig, service.stop)
            await service.run()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(go())


if __name__ == "__main__":  # pragma: no cover
    app(sys.argv[1:])
