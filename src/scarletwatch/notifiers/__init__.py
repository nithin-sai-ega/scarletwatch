"""Notifier registry: builds the configured notifiers from env settings plus UI overrides."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

import httpx

from ..config import Settings
from .base import DeliveryResult, Notification, Notifier, NotifyError, dispatch
from .desktop import DesktopNotifier
from .discord import DiscordNotifier
from .ntfy import NtfyNotifier
from .smtp import SmtpFactory, SmtpNotifier
from .telegram import TelegramNotifier
from .webhook import WebhookNotifier

log = logging.getLogger(__name__)

__all__ = [
    "DeliveryResult",
    "Notification",
    "Notifier",
    "NotifyError",
    "build_notifiers",
    "dispatch",
]

# Settings editable from the dashboard (stored in SQLite). Everything else is env-only.
UI_SETTING_KEYS = ("ntfy_topic", "discord_webhook_url")


def effective_value(settings: Settings, overrides: Mapping[str, str], key: str) -> str:
    """UI-saved value wins over the environment value."""
    override = overrides.get(key, "")
    if override:
        return override
    value = getattr(settings, key)
    return value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)


def build_notifiers(
    settings: Settings,
    overrides: Mapping[str, str],
    client: httpx.AsyncClient,
    smtp_factory: SmtpFactory | None = None,
) -> list[Notifier]:
    """Build every notifier that has enough configuration. Misconfigured ones are skipped."""
    notifiers: list[Notifier] = []

    def add(factory_name: str, build: Callable[[], Notifier]) -> None:
        try:
            notifiers.append(build())
        except ValueError as exc:
            log.warning("%s notifier disabled: %s", factory_name, exc)

    topic = effective_value(settings, overrides, "ntfy_topic")
    if topic:
        add(
            "ntfy",
            lambda: NtfyNotifier(
                client, topic, settings.ntfy_server, settings.ntfy_token.get_secret_value()
            ),
        )
    discord = effective_value(settings, overrides, "discord_webhook_url")
    if discord:
        add("discord", lambda: DiscordNotifier(client, discord))
    if settings.smtp_host and settings.smtp_to:
        add(
            "email",
            lambda: SmtpNotifier(
                settings.smtp_host,
                settings.smtp_port,
                settings.smtp_username,
                settings.smtp_password.get_secret_value(),
                settings.smtp_from,
                settings.smtp_to,
                settings.smtp_security,
                smtp_factory,
            ),
        )
    if settings.telegram_bot_token.get_secret_value() and settings.telegram_chat_id:
        add(
            "telegram",
            lambda: TelegramNotifier(
                client, settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id
            ),
        )
    if settings.webhook_url.get_secret_value():
        add("webhook", lambda: WebhookNotifier(client, settings.webhook_url.get_secret_value()))
    if settings.desktop_notifications:
        notifiers.append(DesktopNotifier())
    return notifiers
