"""Builds notification content and the (configurable) WebReg registration link."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from .catalog import SectionInfo
from .config import DEFAULT_REGISTER_URL_TEMPLATE, FALLBACK_REGISTER_URL
from .notifiers.base import Notification
from .semesters import Semester


def register_link(template: str, semester: Semester, indexes: Sequence[str]) -> str:
    """Fill the registration URL template; an empty template falls back to the WebReg home page.

    Placeholders: {term} {year} {campus} {semester} (term digit + year, e.g. 92026),
    {index} (first index) and {indexes} (comma-separated).
    """
    if not template.strip():
        return FALLBACK_REGISTER_URL
    values = {
        "{term}": str(semester.term),
        "{year}": str(semester.year),
        "{campus}": semester.campus,
        "{semester}": semester.semester_code,
        "{index}": indexes[0] if indexes else "",
        "{indexes}": ",".join(indexes),
    }
    url = template
    for key, value in values.items():
        url = url.replace(key, value)
    return url


def humanize(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds} seconds ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min ago"


def duration_text(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds} seconds"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def open_notification(
    semester: Semester,
    index: str,
    info: SectionInfo | None,
    open_for: timedelta | None,
    template: str = DEFAULT_REGISTER_URL_TEMPLATE,
    *,
    repeat: bool = False,
) -> Notification:
    link = register_link(template, semester, [index])
    when = humanize(open_for) if open_for is not None else "just now"
    if info is not None:
        label = f"{info.course_title.title()} ({info.course_string}) section {info.number}"
        lines = [
            f"{label} - index {index}",
            f"Instructor: {info.instructor_text}",
            f"Meets: {info.meeting_text}",
        ]
        title_core = f"{info.course_title.title()} {info.course_string} §{info.number}"
    else:
        lines = [f"Section index {index}"]
        title_core = f"Index {index}"
    lines.append(f"{semester.label}")
    lines.append(
        f"Opened {when} (first seen by this watcher)."
        if not repeat
        else f"Still open - opened {when}."
    )
    lines.append("Open the link, log in yourself, and register fast.")
    prefix = "STILL OPEN" if repeat else "OPEN"
    return Notification(
        title=f"{prefix}: {title_core}",
        body="\n".join(lines),
        link=link,
        kind="section_still_open" if repeat else "section_opened",
        data={
            "index": index,
            "semester": semester.key,
            "course_string": info.course_string if info else None,
            "section": info.number if info else None,
        },
    )


def make_test_notification() -> Notification:
    return Notification(
        title="ScarletWatch test notification",
        body="If you can read this, this notifier works. No section has opened.",
        link=None,
        urgent=False,
        kind="test",
    )
