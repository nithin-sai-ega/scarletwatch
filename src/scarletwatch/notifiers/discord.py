"""Discord webhook notifier."""

from __future__ import annotations

import re

import httpx

from .base import Notification, Notifier, NotifyError

_WEBHOOK_RE = re.compile(
    r"^https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+/?$"
)


def validate_webhook_url(url: str) -> str:
    url = url.strip()
    if not _WEBHOOK_RE.match(url):
        raise ValueError(
            "Discord webhook URL should look like https://discord.com/api/webhooks/<id>/<token>."
        )
    return url


class DiscordNotifier(Notifier):
    name = "discord"

    def __init__(self, client: httpx.AsyncClient, webhook_url: str) -> None:
        self._client = client
        self._url = validate_webhook_url(webhook_url)

    async def send(self, notification: Notification) -> None:
        embed: dict[str, object] = {
            "title": notification.title[:256],
            "description": notification.body[:4000],
            "color": 0xCC0033 if notification.urgent else 0x2ECC71,
        }
        if notification.link:
            embed["url"] = notification.link
        payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}
        try:
            response = await self._client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise NotifyError(f"Discord request failed: {type(exc).__name__}") from exc
        if response.status_code == 429:
            raise NotifyError("Discord rate limit hit (HTTP 429)")
        if response.status_code >= 400:
            raise NotifyError(f"Discord returned HTTP {response.status_code}")
