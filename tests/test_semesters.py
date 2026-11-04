import pytest

from scarletwatch.semesters import (
    Semester,
    SemesterError,
    parse_campus,
    parse_term,
    parse_year,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("fall", 9), ("Fall", 9), ("9", 9), (9, 9), ("spring", 1), ("winter", 0), ("summer", 7)],
)
def test_parse_term(raw, expected):
    assert parse_term(raw) == expected


@pytest.mark.parametrize("raw", ["", "autumnal", "5", "10"])
def test_parse_term_rejects(raw):
    with pytest.raises(SemesterError):
        parse_term(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NB", "NB"),
        ("nb", "NB"),
        ("New Brunswick", "NB"),
        ("new-brunswick", "NB"),
        ("Newark", "NK"),
        ("camden", "CM"),
        ("ONLINE_NB", "ONLINE_NB"),
    ],
)
def test_parse_campus(raw, expected):
    assert parse_campus(raw) == expected


def test_parse_campus_rejects_garbage():
    with pytest.raises(SemesterError):
        parse_campus("not a campus!!")


def test_parse_year_bounds():
    assert parse_year("2026") == 2026
    with pytest.raises(SemesterError):
        parse_year("26")
    with pytest.raises(SemesterError):
        parse_year("abc")


@pytest.mark.parametrize(
    "text",
    ["fall 2026 NB", "Fall 2026 New Brunswick", "2026 fall nb", "9 2026 NB", "NB fall 2026"],
)
def test_parse_free_text(text):
    assert Semester.parse(text) == Semester(2026, 9, "NB")


@pytest.mark.parametrize("text", ["fall NB", "2026 NB", "fall 2026", "hello"])
def test_parse_free_text_errors(text):
    with pytest.raises(SemesterError):
        Semester.parse(text)


def test_derived_values():
    sem = Semester.of(2026, "fall", "New Brunswick")
    assert sem.semester_code == "92026"
    assert sem.params == {"year": "2026", "term": "9", "campus": "NB"}
    assert sem.key == "2026-9-NB"
    assert sem.label == "Fall 2026 (New Brunswick)"
    assert Semester.of(2027, "spring", "NK").semester_code == "12027"
