"""Validated, environment-backed authentication configuration."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from urllib.parse import urlsplit


def _required(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _absolute_url(value: str, name: str, *, allow_http: bool = False) -> str:
    parsed = urlsplit(value)
    permitted_schemes = {"https"} | ({"http"} if allow_http else set())
    if (
        parsed.scheme not in permitted_schemes
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be an absolute {'HTTP(S)' if allow_http else 'HTTPS'} URL")
    return value.rstrip("/")


def _derive_key(value: str) -> bytes:
    if len(value) < 32:
        raise ValueError("SETTINGS_ENCRYPTION_KEY must contain at least 32 characters")
    return hashlib.sha256(value.encode("utf-8")).digest()


@dataclass(frozen=True, slots=True)
class AuthSettings:
    issuer_url: str
    client_id: str
    client_secret: str
    app_origin: str
    encryption_key: bytes
    database_path: str
    cookie_name: str = "spy_futures_allocator_session"
    idle_timeout_seconds: int = 24 * 60 * 60
    absolute_timeout_seconds: int = 7 * 24 * 60 * 60
    flow_timeout_seconds: int = 10 * 60
    clock_skew_seconds: int = 60
    allow_insecure_http: bool = False

    def __post_init__(self) -> None:
        if not self.client_id or not self.client_secret:
            raise ValueError("OIDC client credentials are required")
        object.__setattr__(
            self,
            "issuer_url",
            _absolute_url(self.issuer_url, "OIDC_ISSUER_URL", allow_http=self.allow_insecure_http),
        )
        object.__setattr__(
            self,
            "app_origin",
            _absolute_url(self.app_origin, "APP_ORIGIN", allow_http=self.allow_insecure_http),
        )
        if len(self.encryption_key) != 32:
            raise ValueError("encryption_key must be exactly 32 bytes")
        if (
            min(
                self.idle_timeout_seconds,
                self.absolute_timeout_seconds,
                self.flow_timeout_seconds,
            )
            <= 0
        ):
            raise ValueError("authentication timeouts must be positive")
        if self.idle_timeout_seconds > self.absolute_timeout_seconds:
            raise ValueError("idle timeout cannot exceed the absolute timeout")
        if not self.cookie_name or any(character.isspace() for character in self.cookie_name):
            raise ValueError("cookie_name is invalid")

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> AuthSettings:
        env = dict(os.environ if environ is None else environ)
        allow_http = env.get("OIDC_ALLOW_INSECURE_HTTP", "").lower() in {"1", "true", "yes"}
        return cls(
            issuer_url=_required(env, "OIDC_ISSUER_URL"),
            client_id=_required(env, "OIDC_CLIENT_ID"),
            client_secret=_required(env, "OIDC_CLIENT_SECRET"),
            app_origin=_required(env, "APP_ORIGIN"),
            encryption_key=_derive_key(_required(env, "SETTINGS_ENCRYPTION_KEY")),
            database_path=env.get("DATABASE_PATH", "/data/spy-futures-allocator.sqlite3"),
            cookie_name=env.get("SESSION_COOKIE_NAME", "spy_futures_allocator_session"),
            allow_insecure_http=allow_http,
        )

    @property
    def callback_url(self) -> str:
        return f"{self.app_origin}/auth/oidc/callback"

    @property
    def logout_callback_url(self) -> str:
        return f"{self.app_origin}/auth/oidc/logout/callback"
