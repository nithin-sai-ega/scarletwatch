import pytest
from fastapi.testclient import TestClient

from scarletwatch.semesters import Semester
from scarletwatch.web import create_app

from .harness import FakeWorld, make_service

FALL = Semester(2026, 9, "NB")
FORM = {"year": "2026", "term": "9", "campus": "NB"}


@pytest.fixture
def world(courses_data, open_data):
    return FakeWorld(courses_data, open_data)


@pytest.fixture
def env(tmp_path, world, monkeypatch):
    service, store = make_service(tmp_path, world, monkeypatch)
    client = TestClient(create_app(service.settings, service), base_url="http://localhost:8000")
    with client:
        yield client, service, store


def test_index_page_renders_and_is_safe_to_load(env):
    client, _, _ = env
    r = client.get("/")
    assert r.status_code == 200
    assert "ScarletWatch" in r.text
    assert 'hx-post="/watches"' in r.text
    assert "not affiliated with Rutgers" in r.text
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_health_endpoint(env):
    client, _, _ = env
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["active_watches"] == 0
    assert body["notifiers"] == ["ntfy"]


def test_add_by_index_shows_row_and_status(env):
    client, _, store = env
    r = client.post("/watches", data={**FORM, "index": "11425"})
    assert r.status_code == 200
    assert "Now watching" in r.text
    assert "Data Structures" in r.text
    assert store.find_watch(FALL, "11425") is not None
    page = client.get("/partials/watches").text
    assert "closed" in page
    assert "11425" in page


def test_add_multiple_indexes_pasted_together(env, world):
    client, _, store = env
    r = client.post("/watches", data={**FORM, "index": "11425, 11426 11427"})
    assert r.status_code == 200
    assert {w.index for w in store.list_watches()} == {"11425", "11426", "11427"}


def test_add_unknown_or_malformed_index_shows_error(env):
    client, _, store = env
    r = client.post("/watches", data={**FORM, "index": "77777"})
    assert "does not exist" in r.text
    r = client.post("/watches", data={**FORM, "index": "01"})
    assert "5-digit" in r.text
    r = client.post("/watches", data={"year": "abc", "term": "9", "campus": "NB", "index": "11425"})
    assert "Year" in r.text
    assert store.list_watches() == []


def test_add_already_open_is_called_out(env):
    client, _, _ = env
    r = client.post("/watches", data={**FORM, "index": "10052"})
    assert "ALREADY OPEN" in r.text
    assert "Register" in client.get("/partials/watches").text


def test_remove_and_done_buttons(env):
    client, _, store = env
    client.post("/watches", data={**FORM, "index": "11425"})
    wid = store.find_watch(FALL, "11425").id
    r = client.post(f"/watches/{wid}/done")
    assert "Resume" in r.text
    assert store.get_watch(wid).done
    r = client.post(f"/watches/{wid}/done")
    assert not store.get_watch(wid).done
    r = client.post(f"/watches/{wid}/remove")
    assert store.get_watch(wid) is None
    assert "Nothing watched yet" in r.text


def test_search_by_code_and_title_with_watch_buttons(env):
    client, _, _ = env
    r = client.get("/search", params={**FORM, "q": "198:112"})
    assert r.status_code == 200
    assert "Data Structures" in r.text
    assert "11425" in r.text
    assert "hx-vals" in r.text
    r = client.get("/search", params={**FORM, "q": "data struct"})
    assert "11426" in r.text
    r = client.get("/search", params={**FORM, "q": "zzzzzz"})
    assert "No courses matched" in r.text
    assert client.get("/search", params={**FORM, "q": ""}).text.strip() == ""


def test_search_marks_already_watched(env):
    client, _, _ = env
    client.post("/watches", data={**FORM, "index": "11425"})
    r = client.get("/search", params={**FORM, "q": "198:112"})
    assert "watching" in r.text


def test_notification_settings_save_mask_and_clear(env):
    client, _, store = env
    r = client.post("/settings/notifications", data={"ntfy_topic": "my-new-topic-123456"})
    assert r.status_code == 200
    assert store.get_setting("ntfy_topic") == "my-new-topic-123456"
    assert "my-new-topic-123456" not in r.text, "topic must be masked in the UI"
    assert "3456" in r.text
    r = client.post("/settings/notifications", data={"ntfy_topic": "bad topic!"})
    assert "ntfy topic may only contain" in r.text
    assert store.get_setting("ntfy_topic") == "my-new-topic-123456"
    r = client.post("/settings/notifications", data={"clear": "ntfy_topic"})
    assert store.get_setting("ntfy_topic") is None


def test_discord_webhook_validated_and_saved(env):
    client, _, store = env
    url = "https://discord.com/api/webhooks/123456789/abcDEF-ghi"
    r = client.post("/settings/notifications", data={"discord_webhook_url": "https://evil.test/x"})
    assert "Discord webhook URL" in r.text
    assert store.get_setting("discord_webhook_url") is None
    client.post("/settings/notifications", data={"discord_webhook_url": url})
    assert store.get_setting("discord_webhook_url") == url
    assert url not in client.get("/").text


def test_send_test_button_reports_per_notifier(env, world):
    client, _, _ = env
    r = client.post("/notifications/test")
    assert "ntfy" in r.text
    assert "sent" in r.text
    assert len(world.ntfy_posts()) == 1
    world.ntfy_status = 500
    r = client.post("/notifications/test")
    assert "FAILED" in r.text


def test_send_test_without_notifier(tmp_path, world, monkeypatch):
    service, _ = make_service(tmp_path, world, monkeypatch, ntfy_topic=None)
    with TestClient(create_app(service.settings, service), base_url="http://localhost") as client:
        r = client.post("/notifications/test")
        assert "No notifier is configured" in r.text


def test_cross_origin_posts_are_blocked(env, world):
    client, _, store = env
    r = client.post(
        "/watches", data={**FORM, "index": "11425"}, headers={"Origin": "https://evil.example"}
    )
    assert r.status_code == 403
    r = client.post(
        "/watches", data={**FORM, "index": "11425"}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert r.status_code == 403
    assert store.list_watches() == []
    ok = client.post(
        "/watches",
        data={**FORM, "index": "11425"},
        headers={"Origin": "http://localhost:8000"},
    )
    assert ok.status_code == 200


def test_foreign_host_header_rejected_when_bound_to_loopback(env):
    client, _, _ = env
    r = client.get("/", headers={"Host": "attacker.example:8000"})
    assert r.status_code == 400
    assert client.get("/", headers={"Host": "127.0.0.1:8000"}).status_code == 200
    assert client.get("/", headers={"Host": "[::1]:8000"}).status_code == 200


def test_html_is_escaped(env, world):
    client, _, _ = env
    world.courses[0]["title"] = "<script>alert(1)</script>"
    # reduce_catalog happens at download time, so force a fresh catalog
    client.post("/watches", data={**FORM, "index": "10052"})
    page = client.get("/partials/watches").text
    assert "<script>alert(1)</script>" not in page
