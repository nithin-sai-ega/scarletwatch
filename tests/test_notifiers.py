import json
import logging
import smtplib
from datetime import timedelta
from typing import ClassVar

import httpx
import pytest

from scarletwatch import logging_setup
from scarletwatch.catalog import Catalog, reduce_catalog
from scarletwatch.config import Settings
from scarletwatch.messages import (
    humanize,
    make_test_notification,
    open_notification,
    register_link,
)
from scarletwatch.notifiers import build_notifiers, dispatch
from scarletwatch.notifiers.base import Notification, Notifier, NotifyError
from scarletwatch.notifiers.desktop import build_command
from scarletwatch.notifiers.discord import DiscordNotifier, validate_webhook_url
from scarletwatch.notifiers.ntfy import NtfyNotifier, validate_topic
from scarletwatch.notifiers.smtp import SmtpNotifier
from scarletwatch.notifiers.telegram import TelegramNotifier
from scarletwatch.notifiers.webhook import WebhookNotifier
from scarletwatch.semesters import Semester

SEM = Semester(2026, 9, "NB")
NOTE = Notification(
    title="OPEN: Data Structures",
    body="line one\nline two",
    link="https://sims.rutgers.edu/webreg/editSchedule.htm?login=cas&semesterSelection=92026&indexList=11425",
)
DISCORD_URL = "https://discord.com/api/webhooks/123456789/abc-DEF_ghi"


def mock_client(status: int = 200, seen: list | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---- ntfy ---------------------------------------------------------------------------------


async def test_ntfy_posts_json_with_click_and_action():
    seen: list[httpx.Request] = []
    await NtfyNotifier(mock_client(200, seen), "my-topic_123").send(NOTE)
    req = seen[0]
    assert str(req.url) == "https://ntfy.sh/"
    body = json.loads(req.content)
    assert body["topic"] == "my-topic_123"
    assert body["title"] == NOTE.title
    assert body["click"] == NOTE.link
    assert body["actions"][0]["url"] == NOTE.link
    assert "authorization" not in req.headers


async def test_ntfy_self_hosted_with_token():
    seen: list[httpx.Request] = []
    n = NtfyNotifier(mock_client(200, seen), "t", "https://ntfy.example.org/", "tk_secret")
    await n.send(NOTE)
    assert str(seen[0].url) == "https://ntfy.example.org/"
    assert seen[0].headers["authorization"] == "Bearer tk_secret"


async def test_ntfy_http_error_does_not_leak_topic():
    with pytest.raises(NotifyError) as info:
        await NtfyNotifier(mock_client(403), "very-secret-topic").send(NOTE)
    assert "403" in str(info.value)
    assert "very-secret-topic" not in str(info.value)


def test_ntfy_topic_validation():
    assert validate_topic(" abc-123 ") == "abc-123"
    for bad in ("", "has space", "a/b", "x" * 65):
        with pytest.raises(ValueError):
            validate_topic(bad)


# ---- discord ------------------------------------------------------------------------------


async def test_discord_embed_payload_and_no_mentions():
    seen: list[httpx.Request] = []
    await DiscordNotifier(mock_client(204, seen), DISCORD_URL).send(NOTE)
    body = json.loads(seen[0].content)
    assert body["embeds"][0]["title"] == NOTE.title
    assert body["embeds"][0]["url"] == NOTE.link
    assert body["allowed_mentions"] == {"parse": []}


async def test_discord_rate_limit_and_error_messages():
    with pytest.raises(NotifyError, match="429"):
        await DiscordNotifier(mock_client(429), DISCORD_URL).send(NOTE)
    with pytest.raises(NotifyError, match="404"):
        await DiscordNotifier(mock_client(404), DISCORD_URL).send(NOTE)


def test_discord_url_validation():
    assert validate_webhook_url(DISCORD_URL) == DISCORD_URL
    for bad in (
        "https://evil.example.com/api/webhooks/1/x",
        "http://discord.com/api/webhooks/1/x",
        "",
    ):
        with pytest.raises(ValueError):
            validate_webhook_url(bad)


# ---- telegram / webhook -------------------------------------------------------------------


async def test_telegram_payload_and_token_not_leaked():
    seen: list[httpx.Request] = []
    await TelegramNotifier(mock_client(200, seen), "123:TOKEN", "555").send(NOTE)
    assert seen[0].url.path == "/bot123:TOKEN/sendMessage"
    body = json.loads(seen[0].content)
    assert body["chat_id"] == "555"
    assert body["reply_markup"]["inline_keyboard"][0][0]["url"] == NOTE.link

    def boom(request):
        raise httpx.ConnectError("failed for https://api.telegram.org/bot123:TOKEN/sendMessage")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    with pytest.raises(NotifyError) as info:
        await TelegramNotifier(client, "123:TOKEN", "555").send(NOTE)
    assert "TOKEN" not in str(info.value)


async def test_generic_webhook_payload():
    seen: list[httpx.Request] = []
    await WebhookNotifier(mock_client(200, seen), "https://example.org/hook").send(NOTE)
    body = json.loads(seen[0].content)
    assert body["event"] == "section_opened"
    assert body["link"] == NOTE.link
    with pytest.raises(NotifyError):
        await WebhookNotifier(mock_client(500), "https://example.org/hook").send(NOTE)
    with pytest.raises(ValueError):
        WebhookNotifier(mock_client(), "ftp://nope")


# ---- smtp ---------------------------------------------------------------------------------


class FakeSMTP:
    instances: ClassVar[list["FakeSMTP"]] = []

    def __init__(self, fail_auth=False, fail_send=False):
        self.fail_auth, self.fail_send = fail_auth, fail_send
        self.log: list = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.log.append("starttls")

    def login(self, user, password):
        if self.fail_auth:
            raise smtplib.SMTPAuthenticationError(535, b"bad creds")
        self.log.append(("login", user, password))

    def send_message(self, msg):
        if self.fail_send:
            raise smtplib.SMTPServerDisconnected("gone")
        self.log.append(("send", msg))


def smtp_notifier(**kw) -> SmtpNotifier:
    FakeSMTP.instances.clear()
    return SmtpNotifier(
        "smtp.example.org",
        587,
        "me@example.org",
        "app-password",
        "",
        "me@example.org, you@example.org",
        factory=lambda host, port: FakeSMTP(**kw),
    )


async def test_smtp_sends_with_starttls_login_and_link():
    await smtp_notifier().send(NOTE)
    log = FakeSMTP.instances[0].log
    assert log[0] == "starttls"
    assert log[1] == ("login", "me@example.org", "app-password")
    msg = log[2][1]
    assert msg["Subject"] == NOTE.title
    assert msg["To"] == "me@example.org, you@example.org"
    assert msg["From"] == "me@example.org"
    assert NOTE.link in msg.get_content()


async def test_smtp_failures_are_clean_errors():
    with pytest.raises(NotifyError, match="authentication"):
        await smtp_notifier(fail_auth=True).send(NOTE)
    with pytest.raises(NotifyError, match="SMTPServerDisconnected"):
        await smtp_notifier(fail_send=True).send(NOTE)


def test_smtp_requires_recipient():
    with pytest.raises(ValueError):
        SmtpNotifier("h", 587, "u", "p", "", " , ")


# ---- desktop ------------------------------------------------------------------------------


def test_desktop_commands_never_embed_text_in_scripts():
    evil = "x'; Remove-Item C:\\ -Recurse; '"
    argv, env = build_command("win32", evil, evil)
    assert all(evil not in part for part in argv)
    assert env == {"SW_TITLE": evil, "SW_BODY": evil}
    argv, env = build_command("darwin", "t", "b")
    assert argv[0] == "osascript"
    assert argv[-2:] == ["t", "b"]


# ---- dispatcher ---------------------------------------------------------------------------


class OkNotifier(Notifier):
    name = "ok"

    def __init__(self):
        self.sent = []

    async def send(self, notification):
        self.sent.append(notification)


class FailingNotifier(Notifier):
    name = "failing"

    async def send(self, notification):
        raise NotifyError("nope")


class CrashingNotifier(Notifier):
    name = "crashing"

    async def send(self, notification):
        raise RuntimeError("bug inside the notifier")


async def test_one_failure_does_not_block_others():
    ok = OkNotifier()
    results = await dispatch([FailingNotifier(), ok, CrashingNotifier()], NOTE)
    by_name = {r.notifier: r for r in results}
    assert by_name["ok"].ok
    assert not by_name["failing"].ok
    assert not by_name["crashing"].ok
    assert "RuntimeError" in by_name["crashing"].error
    assert ok.sent == [NOTE]


async def test_dispatch_with_no_notifiers():
    assert await dispatch([], NOTE) == []


async def test_errors_are_redacted_and_secrets_never_logged(caplog):
    logging_setup.register_secrets(["supersecret-topic-1234"])

    class Leaky(Notifier):
        name = "leaky"

        async def send(self, notification):
            raise NotifyError("failed posting to https://ntfy.sh/supersecret-topic-1234")

    with caplog.at_level(logging.WARNING):
        results = await dispatch([Leaky()], NOTE)
    assert "supersecret-topic-1234" not in (results[0].error or "")
    assert "supersecret-topic-1234" not in logging_setup.redact(caplog.text)
    logging_setup._secrets.discard("supersecret-topic-1234")


# ---- factory ------------------------------------------------------------------------------


def settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(f"SCARLETWATCH_{k.upper()}", v)
    return Settings(_env_file=None)


def test_factory_builds_only_configured_notifiers(monkeypatch):
    s = settings(monkeypatch)
    assert build_notifiers(s, {}, mock_client()) == []
    s = settings(monkeypatch, ntfy_topic="envtopic", webhook_url="https://example.org/h")
    names = {n.name for n in build_notifiers(s, {}, mock_client())}
    assert names == {"ntfy", "webhook"}


def test_factory_ui_overrides_win_and_bad_values_are_skipped(monkeypatch):
    s = settings(monkeypatch, ntfy_topic="envtopic")
    notifiers = build_notifiers(
        s, {"ntfy_topic": "uitopic", "discord_webhook_url": DISCORD_URL}, mock_client()
    )
    assert {n.name for n in notifiers} == {"ntfy", "discord"}
    assert next(n for n in notifiers if n.name == "ntfy")._topic == "uitopic"
    bad = build_notifiers(s, {"discord_webhook_url": "https://evil.example.com/x"}, mock_client())
    assert {n.name for n in bad} == {"ntfy"}


def test_factory_smtp_requires_host_and_recipient(monkeypatch):
    s = settings(monkeypatch, smtp_host="smtp.example.org")
    assert build_notifiers(s, {}, mock_client()) == []
    s = settings(monkeypatch, smtp_host="smtp.example.org", smtp_to="a@b.c")
    assert [n.name for n in build_notifiers(s, {}, mock_client())] == ["email"]


# ---- message content / register link ------------------------------------------------------


def test_register_link_default_and_custom_and_fallback():
    from scarletwatch.config import DEFAULT_REGISTER_URL_TEMPLATE

    assert register_link(DEFAULT_REGISTER_URL_TEMPLATE, SEM, ["11425"]) == (
        "https://sims.rutgers.edu/webreg/editSchedule.htm"
        "?login=cas&semesterSelection=92026&indexList=11425"
    )
    assert register_link(
        "https://x/?t={term}&y={year}&i={index}&all={indexes}", SEM, ["1", "2"]
    ) == ("https://x/?t=9&y=2026&i=1&all=1,2")
    assert register_link("", SEM, ["11425"]) == "https://sims.rutgers.edu/webreg/"
    assert register_link("   ", SEM, ["11425"]) == "https://sims.rutgers.edu/webreg/"


def test_open_notification_contains_everything(courses_data):
    catalog = Catalog(SEM, reduce_catalog(courses_data), 0.0)
    info = catalog.get("11390")
    n = open_notification(SEM, "11390", info, timedelta(seconds=2))
    text = n.title + "\n" + n.body
    assert "Intro Computer Sci" in text
    assert "01:198:111" in text
    assert "section 02" in text
    assert "11390" in text
    assert "Instructor:" in text
    assert "Meets:" in text
    assert "just now" in text
    assert n.link and "indexList=11390" in n.link
    assert n.title.startswith("OPEN:")


def test_open_notification_without_catalog_info_still_useful():
    n = open_notification(SEM, "11390", None, None)
    assert "11390" in n.body
    assert n.link and "indexList=11390" in n.link


def test_repeat_notification_is_labelled():
    n = open_notification(SEM, "11390", None, timedelta(minutes=12), repeat=True)
    assert n.title.startswith("STILL OPEN")
    assert "12 minutes ago" in n.body


def test_humanize():
    assert humanize(timedelta(seconds=1)) == "just now"
    assert humanize(timedelta(seconds=30)) == "30 seconds ago"
    assert humanize(timedelta(minutes=1)) == "1 minute ago"
    assert humanize(timedelta(minutes=125)) == "2 h 5 min ago"


def test_test_notification_is_not_urgent():
    n = make_test_notification()
    assert n.kind == "test"
    assert not n.urgent
