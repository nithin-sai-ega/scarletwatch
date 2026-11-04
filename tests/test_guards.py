import pytest

from scarletwatch.guards import (
    COLLAPSE_CONFIRMATIONS,
    OpenListGuard,
    SuspiciousData,
    parse_open_list,
)


def idx(n: int, start: int = 10000) -> list[str]:
    return [f"{start + i:05d}" for i in range(n)]


def test_valid_list(open_data):
    assert parse_open_list(open_data) == frozenset(open_data)


def test_empty_list_is_suspicious():
    with pytest.raises(SuspiciousData, match="empty"):
        parse_open_list([])


@pytest.mark.parametrize("data", [{"a": 1}, "oops", None, 5])
def test_wrong_shape(data):
    with pytest.raises(SuspiciousData):
        parse_open_list(data)


@pytest.mark.parametrize("bad", [123, "1234", "123456", "abcde", None])
def test_malformed_entries(bad):
    with pytest.raises(SuspiciousData, match="malformed"):
        parse_open_list(["10001", bad])


def test_leading_zero_indexes_ok():
    assert parse_open_list(["00008", "00009"]) == {"00008", "00009"}


def test_size_collapse_is_rejected_then_state_preserved():
    guard = OpenListGuard()
    guard.check(idx(8000))
    with pytest.raises(SuspiciousData, match="shrank"):
        guard.check(idx(100))
    assert guard.last_good_size == 8000
    # normal list afterwards is fine and resets pending state
    guard.check(idx(7900))
    assert guard.last_good_size == 7900


def test_small_drop_is_fine():
    guard = OpenListGuard()
    guard.check(idx(8000))
    assert len(guard.check(idx(6000))) == 6000


def test_persistent_collapse_is_eventually_accepted():
    guard = OpenListGuard()
    guard.check(idx(8000))
    for _ in range(COLLAPSE_CONFIRMATIONS - 1):
        with pytest.raises(SuspiciousData):
            guard.check(idx(500))
    assert len(guard.check(idx(500))) == 500
    assert guard.last_good_size == 500


def test_erratic_collapses_never_accepted():
    guard = OpenListGuard()
    guard.check(idx(8000))
    for size in (500, 100, 2000, 300, 900):
        with pytest.raises(SuspiciousData):
            guard.check(idx(size))


def test_collapse_not_judged_on_small_baseline():
    guard = OpenListGuard()
    guard.check(idx(40))
    assert len(guard.check(idx(3))) == 3
