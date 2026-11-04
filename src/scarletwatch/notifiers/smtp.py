"""SMTP email notifier (e.g. Gmail with an app password). Credentials come from the environment."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .base import Notification, Notifier, NotifyError

SmtpFactory = Callable[[str, int], smtplib.SMTP]


def _default_factory(security: str) -> SmtpFactory:
    def make(host: str, port: int) -> smtplib.SMTP:
        if security == "ssl":
            return smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
        return smtplib.SMTP(host, port, timeout=20)

    return make


class SmtpNotifier(Notifier):
    name = "email"

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        recipients: str,
        security: str = "starttls",
        factory: SmtpFactory | None = None,
    ) -> None:
        self._host, self._port = host, port
        self._username, self._password = username, password
        self._sender = sender or username
        self._recipients = [r.strip() for r in recipients.split(",") if r.strip()]
        self._security = security
        self._factory = factory or _default_factory(security)
        if not self._recipients:
            raise ValueError("SMTP recipient (SCARLETWATCH_SMTP_TO) is empty")

    def _build(self, n: Notification) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = n.title
        msg["From"] = self._sender
        msg["To"] = ", ".join(self._recipients)
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="scarletwatch.local")
        if n.urgent:
            msg["X-Priority"] = "1"
        body = n.body
        if n.link:
            body += f"\n\n{n.link_label}: {n.link}"
        msg.set_content(body)
        return msg

    def _send_blocking(self, n: Notification) -> None:
        message = self._build(n)
        try:
            with self._factory(self._host, self._port) as server:
                if self._security == "starttls":
                    server.starttls(context=ssl.create_default_context())
                if self._username:
                    server.login(self._username, self._password)
                server.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise NotifyError("SMTP authentication failed (check username / app password)") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise NotifyError(f"SMTP delivery failed: {type(exc).__name__}") from exc

    async def send(self, notification: Notification) -> None:
        await asyncio.to_thread(self._send_blocking, notification)
