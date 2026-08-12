"""Pocket ID compatible OpenID Connect authentication."""

from .models import AuthSession, LoginResult, OidcIdentity
from .provider import OidcProvider
from .router import AuthGuard, build_auth_router, login_redirect
from .service import AuthError, AuthService, InvalidAuthRequest, Unauthenticated
from .settings import AuthSettings
from .store import SqliteAuthStore

__all__ = [
    "AuthError",
    "AuthGuard",
    "AuthService",
    "AuthSession",
    "AuthSettings",
    "InvalidAuthRequest",
    "LoginResult",
    "OidcIdentity",
    "OidcProvider",
    "SqliteAuthStore",
    "Unauthenticated",
    "build_auth_router",
    "login_redirect",
]
