"""Sanity checks for openSections data.

An invalid term/campus yields HTTP 200 with ``[]``, so an empty list is ambiguous and is
never interpreted as "everything closed". A sudden large drop in size is treated the same
way, unless it persists across several consecutive polls (a real, lasting change).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_INDEX_RE = re.compile(r"^\d{5}$")

COLLAPSE_MIN_BASELINE = 100  # only judge a "collapse" when we had a decent-sized list
COLLAPSE_RATIO = 0.5  # new size below 50% of the last good size is suspicious
COLLAPSE_CONFIRMATIONS = 3  # same-looking collapsed list this many polls in a row => accept


class SuspiciousData(Exception):
    """The poll result should be skipped; no state changes and no notifications."""


def parse_open_list(data: Any) -> frozenset[str]:
    """Validate shape and content. Raises SuspiciousData on anything unexpected."""
    if not isinstance(data, list):
        raise SuspiciousData(f"expected a JSON array, got {type(data).__name__}")
    if not data:
        raise SuspiciousData(
            "empty list (invalid term/campus, or a server glitch); not treating as all closed"
        )
    bad = [v for v in data if not (isinstance(v, str) and _INDEX_RE.match(v))]
    if bad:
        raise SuspiciousData(f"{len(bad)} malformed entries, e.g. {bad[0]!r}")
    return frozenset(data)


@dataclass
class OpenListGuard:
    """Per-semester guard remembering the last accepted list size."""

    last_good_size: int | None = None
    _pending_size: int | None = None
    _pending_count: int = 0

    def check(self, data: Any) -> frozenset[str]:
        """Return the validated set of open indexes or raise SuspiciousData."""
        opened = parse_open_list(data)
        size = len(opened)
        base = self.last_good_size
        if base is not None and base >= COLLAPSE_MIN_BASELINE and size < base * COLLAPSE_RATIO:
            if self._pending_size is not None and abs(size - self._pending_size) <= max(
                5, self._pending_size * 0.1
            ):
                self._pending_count += 1
            else:
                self._pending_count = 1
            self._pending_size = size
            if self._pending_count < COLLAPSE_CONFIRMATIONS:
                raise SuspiciousData(
                    f"list shrank from {base} to {size} entries "
                    f"({self._pending_count}/{COLLAPSE_CONFIRMATIONS} confirmations)"
                )
        self._pending_size = None
        self._pending_count = 0
        self.last_good_size = size
        return opened
