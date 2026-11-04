"""Small HTMX dashboard served by the same process as the scheduler."""

from .app import create_app

__all__ = ["create_app"]
