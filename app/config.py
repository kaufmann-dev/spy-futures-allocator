from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    app_origin: AnyHttpUrl = Field(alias="APP_ORIGIN")
    database_path: Path = Field(
        default=Path("/data/arl5-simplified.sqlite3"), alias="DATABASE_PATH"
    )
    session_secret: SecretStr = Field(alias="SESSION_SECRET")
    settings_encryption_key: SecretStr = Field(alias="SETTINGS_ENCRYPTION_KEY")
    oidc_issuer_url: AnyHttpUrl = Field(alias="OIDC_ISSUER_URL")
    oidc_client_id: str = Field(min_length=1, alias="OIDC_CLIENT_ID")
    oidc_client_secret: SecretStr = Field(alias="OIDC_CLIENT_SECRET")
    session_cookie_name: str = Field(default="arl5_session", alias="SESSION_COOKIE_NAME")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("app_origin", "oidc_issuer_url")
    @classmethod
    def require_https_outside_localhost(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.scheme != "https" and value.host not in {"localhost", "127.0.0.1"}:
            raise ValueError("HTTPS is required outside localhost")
        return value

    @field_validator("session_secret")
    @classmethod
    def validate_session_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("SESSION_SECRET must contain at least 32 characters")
        return value

    @field_validator("settings_encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("SETTINGS_ENCRYPTION_KEY must contain at least 32 characters")
        return value

    @field_validator("session_cookie_name")
    @classmethod
    def validate_cookie_name(cls, value: str) -> str:
        if not value or any(character.isspace() for character in value):
            raise ValueError("SESSION_COOKIE_NAME is invalid")
        return value

    @property
    def origin(self) -> str:
        return str(self.app_origin).rstrip("/")

    @property
    def issuer(self) -> str:
        return str(self.oidc_issuer_url).rstrip("/")


@lru_cache
def get_config() -> AppConfig:
    return AppConfig()
