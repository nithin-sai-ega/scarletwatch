"""ntfy (https://ntfy.sh or self-hosted): free phone push, no account required."""

from __future__ import annotations

import re

import httpx

from .base import Notification, Notifier, NotifyError

_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_topic(topic: str) -> str:
    topic = topic.strip()
    if not _TOPIC_RE.match(topic):
        raise ValueError("ntfy topic may only contain letters, digits, '-' and '_' (max 64).")
    return topic


class NtfyNotifier(Notifier):
    name = "ntfy"

    def __init__(
        self,
        client: httpx.AsyncClient,
        topic: str,
        server: str = "https://ntfy.sh",
        token: str = "",
    ) -> None:
        self._client = client
        self._topic = validate_topic(topic)
        self._server = server.rstrip("/")
        self._token = token

    async def send(self, notification: Notification) -> None:
        # JSON publishing keeps non-ASCII text out of HTTP headers.
        payload: dict[str, object] = {
            "topic": self._topic,
            "title": notification.title,
            "message": notification.body,
            "priority": 5 if notification.urgent else 3,
            "tags": ["rotating_light"] if notification.urgent else ["white_check_mark"],
        }
        if notification.link:
            payload["click"] = notification.link
            payload["actions"] = [
                {"action": "view", "label": notification.link_label, "url": notification.link}
            ]
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            response = await self._client.post(self._server + "/", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise NotifyError(f"ntfy request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise NotifyError(f"ntfy returned HTTP {response.status_code}")
