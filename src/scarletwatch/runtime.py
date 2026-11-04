"""Wiring: builds a Service (store, HTTP client, catalog cache) and tears it down cleanly."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from . import USER_AGENT
from .catalog import CatalogStore
from .config import Settings, load_settings
from .logging_setup import register_secrets, setup_logging
from .rutgers import RutgersClient
from .service import Service
from .store import Store

log = logging.getLogger(__name__)


def configure_logging(settings: Settings) -> None:
    register_secrets(settings.secret_values())
    setup_logging(settings.log_level, settings.log_format)


@asynccontextmanager
async def open_service(settings: Settings | None = None) -> AsyncIterator[Service]:
    settings = settings or load_settings()
    store = Store(settings.db_path)
    http = httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
    )
    rutgers = RutgersClient(http)
    catalogs = CatalogStore(
        rutgers, settings.catalog_dir, max(settings.catalog_ttl_minutes, 15.0) * 60
    )
    service = Service(settings, store, rutgers, catalogs, http)
    try:
        yield service
    finally:
        await http.aclose()
        store.close()
