"""Semester parsing: "fall 2026 New Brunswick" <-> (year=2026, term=9, campus=NB)."""

from __future__ import annotations

import re
from dataclasses import dataclass

TERM_NAMES: dict[int, str] = {0: "Winter", 1: "Spring", 7: "Summer", 9: "Fall"}
_TERM_ALIASES: dict[str, int] = {
    "winter": 0,
    "spring": 1,
    "summer": 7,
    "fall": 9,
    "autumn": 9,
}

CAMPUS_NAMES: dict[str, str] = {
    "NB": "New Brunswick",
    "NK": "Newark",
    "CM": "Camden",
}
_CAMPUS_ALIASES: dict[str, str] = {
    "nb": "NB",
    "newbrunswick": "NB",
    "brunswick": "NB",
    "nk": "NK",
    "newark": "NK",
    "cm": "CM",
    "camden": "CM",
}
_RAW_CAMPUS_RE = re.compile(r"^[A-Z][A-Z_]{0,15}$")

MIN_YEAR, MAX_YEAR = 2000, 2100


class SemesterError(ValueError):
    """Raised when a semester, term or campus cannot be understood."""


def parse_term(value: str | int) -> int:
    """Accept a name (``fall``) or a raw code (``9``)."""
    text = str(value).strip().lower()
    if text in _TERM_ALIASES:
        return _TERM_ALIASES[text]
    if text.isdigit() and int(text) in TERM_NAMES:
        return int(text)
    valid = ", ".join(f"{n.lower()} ({c})" for c, n in TERM_NAMES.items())
    raise SemesterError(f"Unknown term {value!r}. Use one of: {valid}.")


def parse_campus(value: str) -> str:
    """Accept a friendly name (``New Brunswick``) or a raw code (``NB``, ``ONLINE_NB``)."""
    text = value.strip()
    key = re.sub(r"[\s_\-]+", "", text).lower()
    if key in _CAMPUS_ALIASES:
        return _CAMPUS_ALIASES[key]
    if _RAW_CAMPUS_RE.match(text.upper()):
        # Raw codes are passed through; the server decides whether it has data.
        return text.upper()
    valid = ", ".join(f"{n} ({c})" for c, n in CAMPUS_NAMES.items())
    raise SemesterError(f"Unknown campus {value!r}. Use one of: {valid}, or a raw campus code.")


def parse_year(value: str | int) -> int:
    try:
        year = int(str(value).strip())
    except ValueError:
        raise SemesterError(f"Year must be a number like 2026, got {value!r}.") from None
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise SemesterError(f"Year {year} is outside {MIN_YEAR}-{MAX_YEAR}.")
    return year


@dataclass(frozen=True, order=True)
class Semester:
    """One (year, term, campus) triple; the unit that is polled."""

    year: int
    term: int
    campus: str

    @classmethod
    def of(cls, year: str | int, term: str | int, campus: str) -> Semester:
        return cls(parse_year(year), parse_term(term), parse_campus(campus))

    @classmethod
    def parse(cls, text: str) -> Semester:
        """Parse free text such as ``fall 2026 NB`` or ``2026 9 New Brunswick``."""
        match = re.search(r"\b(20\d\d)\b", text)
        if not match:
            raise SemesterError(f"No year found in {text!r}. Try e.g. 'fall 2026 NB'.")
        year = match.group(1)
        rest = (text[: match.start()] + " " + text[match.end() :]).split()
        term_word = next(
            (w for w in rest if w.lower() in _TERM_ALIASES or (w.isdigit() and len(w) == 1)),
            None,
        )
        if term_word is None:
            raise SemesterError(f"No term found in {text!r}. Try e.g. 'fall 2026 NB'.")
        rest.remove(term_word)
        campus = " ".join(rest)
        if not campus:
            raise SemesterError(f"No campus found in {text!r}. Try e.g. 'fall 2026 NB'.")
        return cls.of(year, term_word, campus)

    @property
    def term_name(self) -> str:
        return TERM_NAMES[self.term]

    @property
    def campus_name(self) -> str:
        return CAMPUS_NAMES.get(self.campus, self.campus)

    @property
    def label(self) -> str:
        return f"{self.term_name} {self.year} ({self.campus_name})"

    @property
    def key(self) -> str:
        return f"{self.year}-{self.term}-{self.campus}"

    @property
    def semester_code(self) -> str:
        """WebReg ``semesterSelection`` value: term digit followed by the year."""
        return f"{self.term}{self.year}"

    @property
    def params(self) -> dict[str, str]:
        return {"year": str(self.year), "term": str(self.term), "campus": self.campus}

    def __str__(self) -> str:
        return self.label
