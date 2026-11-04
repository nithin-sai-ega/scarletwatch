"""Generic JSON webhook notifier (for Home Assistant, n8n, Zapier, your own script, ...)."""

from __future__ import annotations

import httpx

from .base import Notification, Notifier, NotifyError


class WebhookNotifier(Notifier):
    name = "webhook"

    def __init__(self, client: httpx.AsyncClient, url: str) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("webhook URL must start with http:// or https://")
        self._client = client
        self._url = url

    async def send(self, notification: Notification) -> None:
        payload = {
            "event": notification.kind,
            "title": notification.title,
            "message": notification.body,
            "link": notification.link,
            "data": notification.data,
        }
        try:
            response = await self._client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise NotifyError(f"webhook request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise NotifyError(f"webhook returned HTTP {response.status_code}")
