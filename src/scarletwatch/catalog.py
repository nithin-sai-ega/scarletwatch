"""Course catalog: validation of indexes, friendly section info, search, and a disk cache.

The catalog (courses.json) is ~21 MB, so it is reduced to the few fields we use, cached on
disk with a TTL, and refreshed lazily. It is never downloaded per poll.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .rutgers import FetchError, RutgersClient
from .semesters import Semester

log = logging.getLogger(__name__)

DAY_NAMES = {
    "M": "Mon",
    "T": "Tue",
    "W": "Wed",
    "TH": "Thu",
    "H": "Thu",
    "F": "Fri",
    "S": "Sat",
    "U": "Sun",
}
_DAY_ORDER = ["M", "T", "W", "H", "F", "S", "U"]
_INDEX_RE = re.compile(r"^\d{5}$")


class CatalogUnavailable(Exception):
    """The catalog could not be loaded and no cached copy exists."""


@dataclass(frozen=True)
class SectionInfo:
    index: str
    number: str
    course_title: str
    course_string: str
    subject: str
    subject_description: str
    credits: float | None
    level: str
    instructors: tuple[str, ...]
    meetings: tuple[dict[str, str], ...]
    catalog_open: bool
    cross_listed: tuple[str, ...] = ()
    via_cross_list: bool = False  # the requested index is a cross-listed alias of this section

    @property
    def instructor_text(self) -> str:
        return "; ".join(_title_case_name(n) for n in self.instructors) or "TBA"

    @property
    def meeting_text(self) -> str:
        return format_meetings(self.meetings)

    @property
    def display_name(self) -> str:
        return f"{self.course_title.title()} ({self.course_string}) section {self.number}"


def _title_case_name(name: str) -> str:
    # Catalog names look like "SELIM, SAMAH"
    return ", ".join(part.strip().title() for part in name.split(",")) if name else name


def _fmt_time(military: str) -> str:
    if not military or not military.isdigit() or len(military) != 4:
        return "?"
    hour, minute = int(military[:2]), military[2:]
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute} {suffix}"


def format_meetings(meetings: tuple[dict[str, str], ...] | list[dict[str, str]]) -> str:
    """Human-readable meeting times, e.g. ``Tue/Thu 3:50 PM-5:10 PM @ HH-A2 (CAC)``."""
    if not meetings:
        return "TBA"
    groups: dict[tuple[str, str, str], list[str]] = {}
    extras: list[str] = []
    for m in meetings:
        day = (m.get("day") or "").strip().upper()
        start, end = m.get("start", ""), m.get("end", "")
        if not day or not start:
            mode = (m.get("mode") or "").upper()
            label = "Online / asynchronous" if "ONLINE" in mode else "Time TBA"
            if label not in extras:
                extras.append(label)
            continue
        place = "-".join(p for p in (m.get("building", ""), m.get("room", "")) if p.strip())
        campus = m.get("campus", "").strip()
        where = f"{place} ({campus})" if place and campus else place or campus
        groups.setdefault((start, end, where), []).append("H" if day == "TH" else day)
    parts: list[str] = []
    for (start, end, where), days in groups.items():
        ordered = sorted(set(days), key=_DAY_ORDER.index)
        text = f"{'/'.join(DAY_NAMES[d] for d in ordered)} {_fmt_time(start)}-{_fmt_time(end)}"
        parts.append(f"{text} @ {where}" if where else text)
    return "; ".join(parts + extras)


def _reduce_course(course: dict[str, Any]) -> dict[str, Any]:
    sections = []
    for sec in course.get("sections") or []:
        sections.append(
            {
                "index": str(sec.get("index", "")),
                "number": str(sec.get("number", "")),
                "open": bool(sec.get("openStatus")),
                "instructors": [
                    i.get("name", "") for i in sec.get("instructors") or [] if i.get("name")
                ],
                "meetings": [
                    {
                        "day": (m.get("meetingDay") or "").strip(),
                        "start": m.get("startTimeMilitary") or "",
                        "end": m.get("endTimeMilitary") or "",
                        "building": (m.get("buildingCode") or "").strip(),
                        "room": (m.get("roomNumber") or "").strip(),
                        "campus": (m.get("campusAbbrev") or "").strip(),
                        "mode": (m.get("meetingModeDesc") or "").strip(),
                    }
                    for m in sec.get("meetingTimes") or []
                ],
                "cross": [
                    str(c["registrationIndex"])
                    for c in sec.get("crossListedSections") or []
                    if c.get("registrationIndex")
                ],
            }
        )
    return {
        "title": course.get("expandedTitle") or course.get("title") or "",
        "courseString": course.get("courseString", ""),
        "subject": course.get("subject", ""),
        "subjectDescription": course.get("subjectDescription", ""),
        "credits": course.get("credits"),
        "level": course.get("level", ""),
        "sections": sections,
    }


def reduce_catalog(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise CatalogUnavailable(
            "Course catalog is empty or malformed (check the year/term/campus combination)."
        )
    return [_reduce_course(c) for c in raw if isinstance(c, dict)]


@dataclass(frozen=True)
class CourseHit:
    course_string: str
    title: str
    subject_description: str
    credits: float | None
    sections: tuple[SectionInfo, ...]


class Catalog:
    def __init__(self, semester: Semester, courses: list[dict[str, Any]], fetched_at: float):
        self.semester = semester
        self.fetched_at = fetched_at
        self._courses = courses
        self._sections: dict[str, SectionInfo] = {}
        self._aliases: dict[str, str] = {}
        self._by_course: dict[str, list[SectionInfo]] = {}
        for course in courses:
            for sec in course["sections"]:
                info = SectionInfo(
                    index=sec["index"],
                    number=sec["number"],
                    course_title=course["title"],
                    course_string=course["courseString"],
                    subject=course["subject"],
                    subject_description=course["subjectDescription"],
                    credits=course["credits"],
                    level=course["level"],
                    instructors=tuple(sec["instructors"]),
                    meetings=tuple(sec["meetings"]),
                    catalog_open=sec["open"],
                    cross_listed=tuple(sec["cross"]),
                )
                self._sections[info.index] = info
                self._by_course.setdefault(course["courseString"], []).append(info)
        for info in list(self._sections.values()):
            for alias in info.cross_listed:
                if alias not in self._sections:
                    self._aliases.setdefault(alias, info.index)

    def __len__(self) -> int:
        return len(self._sections)

    def has(self, index: str) -> bool:
        return index in self._sections or index in self._aliases

    def get(self, index: str) -> SectionInfo | None:
        info = self._sections.get(index)
        if info is not None:
            return info
        primary = self._sections.get(self._aliases.get(index, ""))
        if primary is None:
            return None
        return replace(primary, index=index, via_cross_list=True)

    def search(self, query: str, limit: int = 25) -> list[CourseHit]:
        """Search by 5-digit index, course code (``198:112``, ``01:198:112``, ``198``), or title."""
        q = query.strip()
        if not q:
            return []
        if _INDEX_RE.match(q):
            info = self.get(q)
            if info is None:
                return []
            return [self._hit(info.course_string)]
        code = _normalize_code(q)
        if code is not None:
            hits = [cs for cs in self._by_course if _code_matches(cs, code)]
        else:
            tokens = [t for t in re.split(r"\s+", q.lower()) if t]
            hits = [
                c["courseString"]
                for c in self._courses
                if all(
                    t in f"{c['title']} {c['subjectDescription']} {c['courseString']}".lower()
                    for t in tokens
                )
            ]
        hits = sorted(dict.fromkeys(hits))
        return [self._hit(cs) for cs in hits[:limit]]

    def _hit(self, course_string: str) -> CourseHit:
        sections = tuple(sorted(self._by_course[course_string], key=lambda s: s.number))
        first = sections[0]
        return CourseHit(
            course_string=course_string,
            title=first.course_title,
            subject_description=first.subject_description,
            credits=first.credits,
            sections=sections,
        )


def _normalize_code(q: str) -> list[str] | None:
    """Turn ``198:112`` / ``01 198 112`` / ``198112`` / ``198`` into ['198','112'] style parts."""
    cleaned = re.sub(r"[\s:.\-/]+", ":", q.strip())
    if re.fullmatch(r"\d{6}", cleaned):
        cleaned = f"{cleaned[:3]}:{cleaned[3:]}"
    elif re.fullmatch(r"\d{8}", cleaned):
        cleaned = f"{cleaned[:2]}:{cleaned[2:5]}:{cleaned[5:]}"
    parts = cleaned.split(":")
    if parts and all(p.isdigit() for p in parts) and 1 <= len(parts) <= 3:
        return parts
    return None


def _code_matches(course_string: str, parts: list[str]) -> bool:
    school, subject, number = [*course_string.split(":"), "", "", ""][:3]
    if len(parts) == 3:
        return (school, subject, number) == tuple(parts)
    if len(parts) == 2:
        if len(parts[0]) == 3:
            return subject == parts[0] and number.startswith(parts[1])
        return school == parts[0] and subject == parts[1]
    return subject == parts[0] if len(parts[0]) == 3 else school == parts[0]


class CatalogStore:
    """Fetches, caches (memory + disk) and serves catalogs, one per semester."""

    def __init__(
        self,
        client: RutgersClient,
        cache_dir: Path,
        ttl_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._dir = cache_dir
        self._ttl = ttl_seconds
        self._clock = clock
        self._mem: dict[str, Catalog] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _path(self, semester: Semester) -> Path:
        return self._dir / f"{semester.key}.json.gz"

    def _fresh(self, catalog: Catalog) -> bool:
        return self._clock() - catalog.fetched_at < self._ttl

    def peek(self, semester: Semester) -> Catalog | None:
        """Whatever is in memory, regardless of age; never does I/O."""
        return self._mem.get(semester.key)

    def _load_disk(self, semester: Semester) -> Catalog | None:
        path = self._path(semester)
        try:
            payload = json.loads(gzip.decompress(path.read_bytes()))
            return Catalog(semester, payload["courses"], float(payload["fetched_at"]))
        except (OSError, ValueError, KeyError, TypeError, EOFError):
            return None

    def _save_disk(self, catalog: Catalog) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            data = json.dumps({"fetched_at": catalog.fetched_at, "courses": catalog._courses})
            tmp = self._path(catalog.semester).with_suffix(".tmp")
            tmp.write_bytes(gzip.compress(data.encode("utf-8")))
            tmp.replace(self._path(catalog.semester))
        except OSError as exc:
            log.warning("could not write catalog cache: %s", exc)

    async def get(self, semester: Semester, *, force: bool = False) -> Catalog:
        lock = self._locks.setdefault(semester.key, asyncio.Lock())
        async with lock:
            cached = self._mem.get(semester.key)
            if cached is None:
                cached = self._load_disk(semester)
                if cached is not None:
                    self._mem[semester.key] = cached
            if cached is not None and self._fresh(cached) and not force:
                return cached
            try:
                log.info("downloading course catalog for %s", semester.label)
                raw = await self._client.courses(semester)
                courses = await asyncio.to_thread(reduce_catalog, raw)
            except (FetchError, CatalogUnavailable) as exc:
                if cached is not None:
                    log.warning("catalog refresh failed (%s); using cached copy", exc)
                    return cached
                if isinstance(exc, CatalogUnavailable):
                    raise
                raise CatalogUnavailable(f"Could not download the course catalog: {exc}") from exc
            catalog = Catalog(semester, courses, self._clock())
            self._mem[semester.key] = catalog
            await asyncio.to_thread(self._save_disk, catalog)
            return catalog
