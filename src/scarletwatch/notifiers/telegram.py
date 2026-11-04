"""Telegram bot notifier (bot token + chat id)."""

from __future__ import annotations

import httpx

from .base import Notification, Notifier, NotifyError


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(
        self,
        client: httpx.AsyncClient,
        bot_token: str,
        chat_id: str,
        api_base: str = "https://api.telegram.org",
    ) -> None:
        self._client = client
        self._url = f"{api_base.rstrip('/')}/bot{bot_token}/sendMessage"
        self._chat_id = chat_id

    async def send(self, notification: Notification) -> None:
        payload: dict[str, object] = {
            "chat_id": self._chat_id,
            "text": f"{notification.title}\n\n{notification.body}"[:4000],
            "disable_web_page_preview": True,
        }
        if notification.link:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": notification.link_label, "url": notification.link}]]
            }
        try:
            response = await self._client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            # The URL contains the bot token, so only report the exception type.
            raise NotifyError(f"Telegram request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise NotifyError(f"Telegram returned HTTP {response.status_code}")
