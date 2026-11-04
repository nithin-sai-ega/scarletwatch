"""SQLite persistence: watches, status history, per-semester poll health, UI settings."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .machine import Status, WatchState
from .semesters import Semester

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    term INTEGER NOT NULL,
    campus TEXT NOT NULL,
    idx TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',      -- active | done
    repeat_minutes INTEGER,
    last_status TEXT,
    last_changed_at TEXT,
    last_notified_at TEXT,
    notify_count INTEGER NOT NULL DEFAULT 0,
    pending_notify INTEGER NOT NULL DEFAULT 0,
    notify_attempts INTEGER NOT NULL DEFAULT 0,
    missing_since TEXT,
    added_at TEXT NOT NULL,
    UNIQUE (year, term, campus, idx)
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE INDEX IF NOT EXISTS history_watch ON history(watch_id, started_at);
CREATE TABLE IF NOT EXISTS semester_health (
    key TEXT PRIMARY KEY,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_list_size INTEGER
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _s(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Watch:
    id: int
    semester: Semester
    index: str
    done: bool
    repeat_minutes: int | None
    state: WatchState
    missing_since: datetime | None
    added_at: datetime


@dataclass(frozen=True)
class HistoryEntry:
    status: Status
    started_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True)
class Health:
    key: str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    last_list_size: int | None = None


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        with self._tx() as c:
            c.executescript(_SCHEMA)
            c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        with suppress(sqlite3.DatabaseError):
            self._conn.execute("PRAGMA journal_mode = WAL")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    # -- watches ---------------------------------------------------------------------------

    @staticmethod
    def _watch(row: sqlite3.Row) -> Watch:
        return Watch(
            id=row["id"],
            semester=Semester(row["year"], row["term"], row["campus"]),
            index=row["idx"],
            done=row["state"] == "done",
            repeat_minutes=row["repeat_minutes"],
            state=WatchState(
                status=Status(row["last_status"]) if row["last_status"] else None,
                last_changed_at=_dt(row["last_changed_at"]),
                last_notified_at=_dt(row["last_notified_at"]),
                notify_count=row["notify_count"],
                pending_notify=bool(row["pending_notify"]),
                notify_attempts=row["notify_attempts"],
            ),
            missing_since=_dt(row["missing_since"]),
            added_at=_dt(row["added_at"]) or utcnow(),
        )

    def add_watch(
        self,
        semester: Semester,
        index: str,
        repeat_minutes: int | None = None,
        now: datetime | None = None,
    ) -> tuple[Watch, bool]:
        """Add a watch. Returns (watch, created); re-adding reactivates a done watch."""
        now = now or utcnow()
        with self._tx() as c:
            row = c.execute(
                "SELECT * FROM watches WHERE year=? AND term=? AND campus=? AND idx=?",
                (semester.year, semester.term, semester.campus, index),
            ).fetchone()
            if row is not None:
                if row["state"] == "done":
                    c.execute("UPDATE watches SET state='active' WHERE id=?", (row["id"],))
                    row = c.execute("SELECT * FROM watches WHERE id=?", (row["id"],)).fetchone()
                    return self._watch(row), True
                return self._watch(row), False
            cur = c.execute(
                "INSERT INTO watches (year, term, campus, idx, repeat_minutes, added_at)"
                " VALUES (?,?,?,?,?,?)",
                (semester.year, semester.term, semester.campus, index, repeat_minutes, _s(now)),
            )
            row = c.execute("SELECT * FROM watches WHERE id=?", (cur.lastrowid,)).fetchone()
            return self._watch(row), True

    def get_watch(self, watch_id: int) -> Watch | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
        return self._watch(row) if row else None

    def find_watch(self, semester: Semester, index: str) -> Watch | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM watches WHERE year=? AND term=? AND campus=? AND idx=?",
                (semester.year, semester.term, semester.campus, index),
            ).fetchone()
        return self._watch(row) if row else None

    def list_watches(self, *, active_only: bool = False) -> list[Watch]:
        sql = "SELECT * FROM watches"
        if active_only:
            sql += " WHERE state='active'"
        sql += " ORDER BY year DESC, term DESC, campus, idx"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [self._watch(r) for r in rows]

    def watches_for(self, semester: Semester, *, active_only: bool = True) -> list[Watch]:
        return [w for w in self.list_watches(active_only=active_only) if w.semester == semester]

    def active_semesters(self) -> set[Semester]:
        return {w.semester for w in self.list_watches(active_only=True)}

    def remove_watch(self, watch_id: int) -> bool:
        with self._tx() as c:
            return c.execute("DELETE FROM watches WHERE id=?", (watch_id,)).rowcount > 0

    def set_done(self, watch_id: int, done: bool) -> bool:
        with self._tx() as c:
            return (
                c.execute(
                    "UPDATE watches SET state=? WHERE id=?",
                    ("done" if done else "active", watch_id),
                ).rowcount
                > 0
            )

    def save_state(self, watch_id: int, state: WatchState) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE watches SET last_status=?, last_changed_at=?, last_notified_at=?,"
                " notify_count=?, pending_notify=?, notify_attempts=? WHERE id=?",
                (
                    state.status.value if state.status else None,
                    _s(state.last_changed_at),
                    _s(state.last_notified_at),
                    state.notify_count,
                    int(state.pending_notify),
                    state.notify_attempts,
                    watch_id,
                ),
            )

    def set_missing(self, watch_id: int, since: datetime | None) -> None:
        with self._tx() as c:
            c.execute("UPDATE watches SET missing_since=? WHERE id=?", (_s(since), watch_id))

    # -- history ----------------------------------------------------------------------------

    def record_status(self, watch_id: int, status: Status, at: datetime) -> None:
        """Close the current history interval (if any) and open a new one."""
        with self._tx() as c:
            c.execute(
                "UPDATE history SET ended_at=? WHERE watch_id=? AND ended_at IS NULL",
                (_s(at), watch_id),
            )
            c.execute(
                "INSERT INTO history (watch_id, status, started_at) VALUES (?,?,?)",
                (watch_id, status.value, _s(at)),
            )

    def history(self, watch_id: int, limit: int = 10) -> list[HistoryEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM history WHERE watch_id=? ORDER BY started_at DESC, id DESC LIMIT ?",
                (watch_id, limit),
            ).fetchall()
        return [
            HistoryEntry(Status(r["status"]), _dt(r["started_at"]) or utcnow(), _dt(r["ended_at"]))
            for r in rows
        ]

    # -- health ----------------------------------------------------------------------------

    def get_health(self, semester: Semester) -> Health:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM semester_health WHERE key=?", (semester.key,)
            ).fetchone()
        if row is None:
            return Health(semester.key)
        return Health(
            row["key"],
            _dt(row["last_attempt_at"]),
            _dt(row["last_success_at"]),
            row["last_error"],
            row["consecutive_failures"],
            row["last_list_size"],
        )

    def save_health(self, h: Health) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO semester_health (key, last_attempt_at, last_success_at, last_error,"
                " consecutive_failures, last_list_size) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,"
                " last_success_at=excluded.last_success_at, last_error=excluded.last_error,"
                " consecutive_failures=excluded.consecutive_failures,"
                " last_list_size=excluded.last_list_size",
                (
                    h.key,
                    _s(h.last_attempt_at),
                    _s(h.last_success_at),
                    h.last_error,
                    h.consecutive_failures,
                    h.last_list_size,
                ),
            )

    # -- settings --------------------------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._tx() as c:
            if value:
                c.execute(
                    "INSERT INTO settings (key, value) VALUES (?,?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            else:
                c.execute("DELETE FROM settings WHERE key=?", (key,))

    def all_settings(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
