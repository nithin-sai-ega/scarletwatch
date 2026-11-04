import pytest
from pydantic import ValidationError

from scarletwatch.config import Settings


def make(monkeypatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(f"SCARLETWATCH_{key.upper()}", value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_defaults(monkeypatch):
    s = make(monkeypatch)
    assert s.poll_interval == 60
    assert s.host == "127.0.0.1"
    assert s.secret_values() == []


def test_poll_interval_minimum_enforced(monkeypatch):
    with pytest.raises(ValidationError):
        make(monkeypatch, poll_interval="10")


def test_catalog_ttl_minimum_enforced(monkeypatch):
    with pytest.raises(ValidationError):
        make(monkeypatch, catalog_ttl_minutes="5")


def test_secrets_are_masked_in_repr_and_listed(monkeypatch):
    s = make(monkeypatch, smtp_password="hunter2hunter2", ntfy_topic="my-secret-topic")
    assert "hunter2hunter2" not in repr(s)
    assert "my-secret-topic" not in repr(s)
    assert set(s.secret_values()) == {"hunter2hunter2", "my-secret-topic"}
