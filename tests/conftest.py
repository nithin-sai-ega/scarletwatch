from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def courses_data() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads((FIXTURES / "courses_trimmed.json").read_text("utf-8"))
    return data


@pytest.fixture
def open_data() -> list[str]:
    data: list[str] = json.loads((FIXTURES / "open_sections_trimmed.json").read_text("utf-8"))
    return data
