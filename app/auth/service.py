"""OIDC transactions and app-owned session lifecycle."""

from __future__ import annotations

import time
from collections.abc import Callable

from .models import AuthSession, LoginFlow, LoginResult, LogoutFlow, LogoutResult
from .provider import OidcClient
from .security import IdTokenCipher, pkce_challenge, random_token, sanitize_return_to, token_hash
from .settings import AuthSettings
from .store import AuthStore


class AuthError(RuntimeError):
    """Safe, user-displayable authentication failure."""


class InvalidAuthRequest(AuthError):
    pass


class Unauthenticated(AuthError):
    pass


class AuthService:
    def __init__(
        self,
        settings: AuthSettings,
        store: AuthStore,
        provider: OidcClient,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.store = store
        self.provider = provider
        self._clock = clock
        self._cipher = IdTokenCipher(settings.encryption_key)

    def _now(self) -> int:
        return int(self._clock())

    async def begin_login(self, return_to: str | None) -> str:
        now = self._now()
        state = random_token()
        nonce = random_token()
        code_verifier = random_token(48)
        self.store.create_login_flow(
            LoginFlow(
                state_hash=token_hash(state),
                nonce=nonce,
                code_verifier=code_verifier,
                return_to=sanitize_return_to(return_to),
                created_at=now,
            )
        )
        try:
            return await self.provider.authorization_url(
                state=state,
                nonce=nonce,
                code_challenge=pkce_challenge(code_verifier),
            )
        except Exception:
            # The flow stays unusable without its unreturned state and is pruned later.
            raise AuthError("Unable to start sign-in") from None

    async def finish_login(self, *, code: str | None, state: str | None) -> LoginResult:
        now = self._now()
        if not code or not state:
            raise InvalidAuthRequest("The sign-in response is incomplete")
        flow = self.store.consume_login_flow(token_hash(state), now)
        if flow is None:
            raise InvalidAuthRequest("The sign-in response is invalid or was already used")
        if now - flow.created_at > self.settings.flow_timeout_seconds:
            raise InvalidAuthRequest("The sign-in response has expired")
        try:
            identity = await self.provider.exchange_code(
                code=code,
                code_verifier=flow.code_verifier,
                expected_nonce=flow.nonce,
            )
        except Exception:
            raise AuthError("Pocket ID could not verify the sign-in") from None

        raw_session_token = random_token()
        session_token_hash = token_hash(raw_session_token)
        encrypted_id_token, id_token_nonce = self._cipher.encrypt(
            identity.id_token, session_token_hash
        )
        absolute_expires_at = now + self.settings.absolute_timeout_seconds
        self.store.create_session(
            AuthSession(
                token_hash=session_token_hash,
                subject=identity.subject,
                created_at=now,
                last_active_at=now,
                absolute_expires_at=absolute_expires_at,
                encrypted_id_token=encrypted_id_token,
                id_token_nonce=id_token_nonce,
            )
        )
        return LoginResult(
            session_token=raw_session_token,
            return_to=flow.return_to,
            absolute_expires_at=absolute_expires_at,
        )

    def current_session(
        self, raw_session_token: str | None, *, user_activity: bool = False
    ) -> AuthSession | None:
        if not raw_session_token:
            return None
        now = self._now()
        hashed = token_hash(raw_session_token)
        session = self.store.get_session(hashed)
        if session is None:
            return None
        idle_expires_at = session.last_active_at + self.settings.idle_timeout_seconds
        if now >= idle_expires_at or now >= session.absolute_expires_at:
            self.store.delete_session(hashed)
            return None
        if user_activity and now > session.last_active_at:
            self.store.touch_session(hashed, now)
            session = AuthSession(
                token_hash=session.token_hash,
                subject=session.subject,
                created_at=session.created_at,
                last_active_at=now,
                absolute_expires_at=session.absolute_expires_at,
                encrypted_id_token=session.encrypted_id_token,
                id_token_nonce=session.id_token_nonce,
            )
        return session

    async def begin_logout(self, raw_session_token: str | None) -> LogoutResult:
        session = self.current_session(raw_session_token, user_activity=False)
        if session is None:
            raise Unauthenticated("No active session")
        try:
            id_token_hint = self._cipher.decrypt(
                session.encrypted_id_token,
                session.id_token_nonce,
                session.token_hash,
            )
        except Exception:
            self.store.delete_session(session.token_hash)
            raise AuthError(
                "The local session was ended, but provider logout could not start"
            ) from None

        # End local access and remove the stored hint before contacting the provider.
        self.store.delete_session(session.token_hash)
        state = random_token()
        now = self._now()
        self.store.create_logout_flow(LogoutFlow(state_hash=token_hash(state), created_at=now))
        try:
            redirect_url = await self.provider.logout_url(id_token_hint=id_token_hint, state=state)
        except Exception:
            raise AuthError(
                "The local session was ended, but provider logout could not start"
            ) from None
        return LogoutResult(redirect_url=redirect_url)

    def finish_logout(self, state: str | None) -> None:
        now = self._now()
        if not state:
            raise InvalidAuthRequest("The logout response is incomplete")
        flow = self.store.consume_logout_flow(token_hash(state), now)
        if flow is None:
            raise InvalidAuthRequest("The logout response is invalid or was already used")
        if now - flow.created_at > self.settings.flow_timeout_seconds:
            raise InvalidAuthRequest("The logout response has expired")
