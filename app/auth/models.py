"""Value objects kept deliberately independent from FastAPI and SQLite."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LoginFlow:
    state_hash: str
    nonce: str
    code_verifier: str
    return_to: str
    created_at: int
    consumed_at: int | None = None


@dataclass(frozen=True, slots=True)
class AuthSession:
    token_hash: str
    subject: str
    created_at: int
    last_active_at: int
    absolute_expires_at: int
    encrypted_id_token: bytes
    id_token_nonce: bytes


@dataclass(frozen=True, slots=True)
class LogoutFlow:
    state_hash: str
    created_at: int
    consumed_at: int | None = None


@dataclass(frozen=True, slots=True)
class OidcIdentity:
    """The only token material allowed to leave the OIDC exchange implementation."""

    subject: str
    id_token: str


@dataclass(frozen=True, slots=True)
class LoginResult:
    session_token: str
    return_to: str
    absolute_expires_at: int


@dataclass(frozen=True, slots=True)
class LogoutResult:
    redirect_url: str
