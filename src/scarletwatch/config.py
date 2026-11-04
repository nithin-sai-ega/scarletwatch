"""Runtime configuration, loaded from SCARLETWATCH_* environment variables and ``.env``."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_POLL_INTERVAL = 30.0
MIN_CATALOG_TTL_MINUTES = 15.0

DEFAULT_REGISTER_URL_TEMPLATE = (
    "https://sims.rutgers.edu/webreg/editSchedule.htm"
    "?login=cas&semesterSelection={semester}&indexList={indexes}"
)
FALLBACK_REGISTER_URL = "https://sims.rutgers.edu/webreg/"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCARLETWATCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # polling
    poll_interval: float = Field(60.0, ge=MIN_POLL_INTERVAL)
    # storage
    data_dir: Path = Path("./data")
    catalog_ttl_minutes: float = Field(60.0, ge=MIN_CATALOG_TTL_MINUTES)
    # web
    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1, le=65535)
    # registration link; empty string disables it (falls back to the WebReg home page)
    register_url_template: str = DEFAULT_REGISTER_URL_TEMPLATE
    # ntfy
    ntfy_topic: SecretStr = SecretStr("")
    ntfy_server: str = "https://ntfy.sh"
    ntfy_token: SecretStr = SecretStr("")
    # discord
    discord_webhook_url: SecretStr = SecretStr("")
    # smtp
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""
    smtp_to: str = ""
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    # telegram
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    # generic webhook
    webhook_url: SecretStr = SecretStr("")
    # desktop
    desktop_notifications: bool = False
    # logging
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scarletwatch.db"

    @property
    def catalog_dir(self) -> Path:
        return self.data_dir / "catalog"

    def secret_values(self) -> list[str]:
        """Every secret currently configured, for log redaction."""
        candidates = [
            self.ntfy_topic,
            self.ntfy_token,
            self.discord_webhook_url,
            self.smtp_password,
            self.telegram_bot_token,
            self.webhook_url,
        ]
        return [s.get_secret_value() for s in candidates if s.get_secret_value()]


def load_settings() -> Settings:
    return Settings()
