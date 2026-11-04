from contextlib import asynccontextmanager

import pytest
from typer.testing import CliRunner

from scarletwatch import cli
from scarletwatch.semesters import Semester
from scarletwatch.store import Store

from .harness import FakeWorld, make_service

runner = CliRunner()
FALL = Semester(2026, 9, "NB")


@pytest.fixture
def world(courses_data, open_data):
    return FakeWorld(courses_data, open_data)


@pytest.fixture
def env(tmp_path, world, monkeypatch):
    monkeypatch.setenv("SCARLETWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # so no stray .env is picked up
    service, _ = make_service(tmp_path, world, monkeypatch, store_path=tmp_path / "scarletwatch.db")

    @asynccontextmanager
    async def fake_open(settings=None):
        yield service

    monkeypatch.setattr(cli, "open_service", fake_open)
    return service, tmp_path


def invoke(*args):
    return runner.invoke(cli.app, list(args))


def test_help_lists_commands():
    r = invoke("--help")
    assert r.exit_code == 0
    for cmd in ("add", "remove", "list", "run", "status", "test-notify", "search", "done"):
        assert cmd in r.output


def test_add_list_status_remove_flow(env):
    r = invoke("add", "11425", "10052", "--year", "2026", "--term", "fall", "--campus", "NB")
    assert r.exit_code == 0, r.output
    assert "Data Structures" in r.output
    assert "ALREADY OPEN" in r.output
    assert "semesterSelection=92026&indexList=10052" in r.output

    r = invoke("list")
    assert "11425" in r.output
    assert "closed" in r.output
    assert "open" in r.output

    r = invoke("status")
    assert r.exit_code == 0
    assert "Fall 2026" in r.output

    r = invoke("remove", "11425")
    assert r.exit_code == 0
    assert "removed" in r.output
    assert "11425" not in invoke("list").output


def test_add_with_semester_text_and_repeat(env):
    service, _ = env
    r = invoke("add", "11425", "--semester", "fall 2026 New Brunswick", "--repeat", "15")
    assert r.exit_code == 0, r.output
    watch = service.store.find_watch(FALL, "11425")
    assert watch.repeat_minutes == 15


def test_add_bad_index_fails_clearly(env):
    r = invoke("add", "77777", "01", "--year", "2026", "--term", "fall")
    assert r.exit_code == 1
    assert "does not exist" in r.output
    assert "5-digit" in r.output


def test_add_requires_semester(env):
    r = invoke("add", "11425")
    assert r.exit_code == 1
    assert "--year" in r.output


def test_add_bad_term_is_a_clean_error(env):
    r = invoke("add", "11425", "--year", "2026", "--term", "autumnal")
    assert r.exit_code == 1
    assert "Unknown term" in r.output


def test_done_hides_from_polling_but_keeps_in_list(env):
    service, _ = env
    invoke("add", "11425", "--year", "2026", "--term", "fall")
    r = invoke("done", "11425")
    assert r.exit_code == 0
    assert service.store.active_semesters() == set()
    assert "done" in invoke("list").output


def test_remove_unknown_and_ambiguous(env):
    service, _ = env
    assert invoke("remove", "11425").exit_code == 1
    service.store.add_watch(FALL, "11425")
    service.store.add_watch(Semester(2027, 1, "NB"), "11425")
    r = invoke("remove", "11425")
    assert r.exit_code == 1
    assert "several semesters" in r.output
    r = invoke("remove", "11425", "--year", "2027", "--term", "spring")
    assert r.exit_code == 0


def test_search_prints_indexes(env):
    r = invoke("search", "198:112", "--year", "2026", "--term", "fall")
    assert r.exit_code == 0, r.output
    assert "01:198:112" in r.output
    assert "11425" in r.output
    r = invoke("search", "nonexistentcourse", "-s", "fall 2026 NB")
    assert "No courses matched" in r.output


def test_test_notify(env, world):
    r = invoke("test-notify")
    assert r.exit_code == 0, r.output
    assert "ntfy: sent" in r.output
    assert len(world.ntfy_posts()) == 1
    world.ntfy_status = 500
    r = invoke("test-notify")
    assert r.exit_code == 1
    assert "FAILED" in r.output


def test_test_notify_without_config(tmp_path, world, monkeypatch):
    monkeypatch.setenv("SCARLETWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    service, _ = make_service(tmp_path, world, monkeypatch, ntfy_topic=None)

    @asynccontextmanager
    async def fake_open(settings=None):
        yield service

    monkeypatch.setattr(cli, "open_service", fake_open)
    r = invoke("test-notify")
    assert r.exit_code == 2
    assert "No notifier configured" in r.output


def test_invalid_config_is_reported(env, monkeypatch):
    monkeypatch.setenv("SCARLETWATCH_POLL_INTERVAL", "5")
    r = invoke("list")
    assert r.exit_code == 1
    assert "Invalid configuration" in r.output


def test_empty_list_message(env):
    assert "No watches yet" in invoke("list").output
    assert "No watches yet" in invoke("status").output


def test_store_is_closed_cleanly_between_commands(env):
    _, path = env
    invoke("list")
    Store(path / "scarletwatch.db").close()
