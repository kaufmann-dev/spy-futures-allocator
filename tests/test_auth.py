from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client  # type: ignore[import-untyped]
from joserfc import jwt
from joserfc.jwk import RSAKey

from app.auth.models import LoginResult, OidcIdentity
from app.auth.provider import OidcProtocolError, OidcProvider
from app.auth.security import pkce_challenge, sanitize_return_to, token_hash
from app.auth.service import AuthService, InvalidAuthRequest
from app.auth.settings import AuthSettings
from app.auth.store import SqliteAuthStore

TEST_ID_TOKEN = ".".join(("signed", "id", "token"))
TEST_CLIENT_SECRET = "-".join(("test", "only", "secret"))


@dataclass
class FakeProvider:
    authorization: dict[str, str] = field(default_factory=dict)
    exchange: dict[str, str] = field(default_factory=dict)
    logout: dict[str, str] = field(default_factory=dict)

    async def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        self.authorization = {
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
        }
        return f"https://id.example/authorize?state={state}"

    async def exchange_code(
        self, *, code: str, code_verifier: str, expected_nonce: str
    ) -> OidcIdentity:
        self.exchange = {
            "code": code,
            "code_verifier": code_verifier,
            "expected_nonce": expected_nonce,
        }
        return OidcIdentity(subject="provider-subject", id_token=TEST_ID_TOKEN)

    async def logout_url(self, *, id_token_hint: str, state: str) -> str:
        self.logout = {"id_token_hint": id_token_hint, "state": state}
        return f"https://id.example/logout?state={state}"


AuthFixture = tuple[AuthService, SqliteAuthStore, FakeProvider, list[int]]


@pytest.fixture
def auth(tmp_path: Path) -> AuthFixture:
    clock = [1_800_000_000]
    settings = AuthSettings(
        issuer_url="https://id.example",
        client_id="client-id",
        client_secret=TEST_CLIENT_SECRET,
        app_origin="https://app.example",
        encryption_key=bytes(range(32)),
        database_path=str(tmp_path / "auth.sqlite3"),
    )
    store = SqliteAuthStore(settings.database_path)
    provider = FakeProvider()
    service = AuthService(settings, store, provider, clock=lambda: clock[0])
    return service, store, provider, clock


@pytest.mark.parametrize(
    "candidate",
    [
        "https://evil.example/steal",
        "//evil.example/steal",
        "/%2f%2fevil.example/steal",
        "/\\evil.example/steal",
        "relative/path",
        "/login",
        "/auth/oidc/start",
        "/safe/../auth/oidc/callback",
        "/%61uth/oidc/callback",
        "/line\nbreak",
    ],
)
def test_return_target_rejects_cross_origin_and_auth_loops(candidate: str) -> None:
    assert sanitize_return_to(candidate) == "/"


def test_return_target_preserves_a_same_origin_path_query_and_fragment() -> None:
    assert sanitize_return_to("/history?page=2#latest") == "/history?page=2#latest"


@pytest.mark.asyncio
async def test_login_uses_one_time_state_nonce_and_pkce_s256(auth: AuthFixture) -> None:
    service, store, provider, _ = auth

    redirect = await service.begin_login("/history?page=2")

    state = provider.authorization["state"]
    assert redirect.endswith(state)
    assert len(provider.authorization["nonce"]) >= 43
    assert len(provider.authorization["code_challenge"]) == 43

    flow = store.consume_login_flow(token_hash(state), 1_800_000_001)
    assert flow is not None
    assert flow.return_to == "/history?page=2"
    assert len(flow.code_verifier) >= 43
    assert provider.authorization["code_challenge"] == pkce_challenge(flow.code_verifier)
    assert provider.authorization["nonce"] == flow.nonce


@pytest.mark.asyncio
async def test_callback_passes_stored_nonce_and_verifier_and_rejects_replay(
    auth: AuthFixture,
) -> None:
    service, _, provider, _ = auth
    await service.begin_login("/")
    state = provider.authorization["state"]
    expected_nonce = provider.authorization["nonce"]

    result = await service.finish_login(code="authorization-code", state=state)

    assert result.return_to == "/"
    assert provider.exchange["code"] == "authorization-code"
    assert provider.exchange["expected_nonce"] == expected_nonce
    assert (
        pkce_challenge(provider.exchange["code_verifier"])
        == provider.authorization["code_challenge"]
    )
    with pytest.raises(InvalidAuthRequest, match="already used"):
        await service.finish_login(code="authorization-code", state=state)


@pytest.mark.asyncio
async def test_expired_login_flow_is_consumed_and_rejected(auth: AuthFixture) -> None:
    service, _, provider, clock = auth
    await service.begin_login("/")
    state = provider.authorization["state"]
    clock[0] += service.settings.flow_timeout_seconds + 1

    with pytest.raises(InvalidAuthRequest, match="expired"):
        await service.finish_login(code="authorization-code", state=state)
    with pytest.raises(InvalidAuthRequest, match="already used"):
        await service.finish_login(code="authorization-code", state=state)


async def _logged_in(auth: AuthFixture) -> LoginResult:
    service, _, provider, _ = auth
    await service.begin_login("/")
    return await service.finish_login(
        code="authorization-code", state=provider.authorization["state"]
    )


@pytest.mark.asyncio
async def test_session_is_opaque_encrypted_and_passive_reads_do_not_slide(
    auth: AuthFixture,
) -> None:
    service, store, _, clock = auth
    result = await _logged_in(auth)
    session_hash = hashlib.sha256(result.session_token.encode()).hexdigest()
    stored = store.get_session(session_hash)

    assert stored is not None
    assert stored.token_hash != result.session_token
    assert b"signed.id.token" not in stored.encrypted_id_token
    first_active = stored.last_active_at

    clock[0] += 600
    passive = service.current_session(result.session_token, user_activity=False)
    assert passive is not None
    refreshed = store.get_session(session_hash)
    assert refreshed is not None
    assert refreshed.last_active_at == first_active

    active = service.current_session(result.session_token, user_activity=True)
    assert active is not None
    refreshed = store.get_session(session_hash)
    assert refreshed is not None
    assert refreshed.last_active_at == clock[0]


@pytest.mark.asyncio
async def test_session_expires_at_idle_and_absolute_limits(auth: AuthFixture) -> None:
    service, store, _, clock = auth
    idle_result = await _logged_in(auth)
    idle_hash = token_hash(idle_result.session_token)
    clock[0] += service.settings.idle_timeout_seconds
    assert service.current_session(idle_result.session_token) is None
    assert store.get_session(idle_hash) is None

    clock[0] += 1
    absolute_result = await _logged_in(auth)
    for _ in range(6):
        clock[0] += service.settings.idle_timeout_seconds - 1
        assert (
            service.current_session(absolute_result.session_token, user_activity=True) is not None
        )
    clock[0] = absolute_result.absolute_expires_at
    assert service.current_session(absolute_result.session_token, user_activity=True) is None


@pytest.mark.asyncio
async def test_logout_deletes_local_session_and_validates_one_time_state(
    auth: AuthFixture,
) -> None:
    service, store, provider, _ = auth
    login = await _logged_in(auth)
    session_hash = token_hash(login.session_token)

    logout = await service.begin_logout(login.session_token)

    assert store.get_session(session_hash) is None
    assert provider.logout["id_token_hint"] == TEST_ID_TOKEN
    assert logout.redirect_url.startswith("https://id.example/logout")
    state = provider.logout["state"]
    service.finish_logout(state)
    with pytest.raises(InvalidAuthRequest, match="already used"):
        service.finish_logout(state)


@pytest.mark.asyncio
async def test_expired_logout_state_is_rejected(auth: AuthFixture) -> None:
    service, _, provider, clock = auth
    login = await _logged_in(auth)
    await service.begin_logout(login.session_token)
    clock[0] += service.settings.flow_timeout_seconds + 1

    with pytest.raises(InvalidAuthRequest, match="expired"):
        service.finish_logout(provider.logout["state"])


@pytest.mark.asyncio
async def test_concrete_provider_uses_confidential_post_and_validates_nonce(
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    signing_key = RSAKey.generate_key(auto_kid=True)
    nonce = "expected-nonce"
    observed_form: dict[str, list[str]] = {}
    settings = AuthSettings(
        issuer_url="https://id.example",
        client_id="client-id",
        client_secret=TEST_CLIENT_SECRET,
        app_origin="https://app.example",
        encryption_key=bytes(range(32)),
        database_path=str(tmp_path / "provider.sqlite3"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://id.example",
                    "authorization_endpoint": "https://id.example/authorize",
                    "token_endpoint": "https://id.example/token",
                    "jwks_uri": "https://id.example/jwks",
                    "end_session_endpoint": "https://id.example/logout",
                    "token_endpoint_auth_methods_supported": ["client_secret_post"],
                    "code_challenge_methods_supported": ["S256"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if path == "/jwks":
            return httpx.Response(200, json={"keys": [signing_key.as_dict(private=False)]})
        if path == "/token":
            observed_form.update(parse_qs(request.content.decode()))
            id_token = jwt.encode(
                {"alg": "RS256", "kid": signing_key.kid},
                {
                    "iss": "https://id.example",
                    "aud": "client-id",
                    "sub": "subject",
                    "nonce": nonce,
                    "iat": now,
                    "exp": now + 300,
                },
                signing_key,
            )
            return httpx.Response(
                200,
                json={"access_token": "ephemeral", "token_type": "Bearer", "id_token": id_token},
            )
        raise AssertionError(f"unexpected OIDC request: {request.url}")

    transport = httpx.MockTransport(handler)
    provider = OidcProvider(
        settings,
        http_client_factory=lambda: httpx.AsyncClient(transport=transport),
        oauth_client_factory=lambda **kwargs: AsyncOAuth2Client(transport=transport, **kwargs),
        clock=lambda: now,
    )
    authorization_url = await provider.authorization_url(
        state="state", nonce=nonce, code_challenge="challenge"
    )
    authorization_query = parse_qs(urlsplit(authorization_url).query)
    assert authorization_query["code_challenge_method"] == ["S256"]
    assert authorization_query["code_challenge"] == ["challenge"]
    assert authorization_query["nonce"] == [nonce]
    assert "client_secret" not in authorization_query

    identity = await provider.exchange_code(
        code="authorization-code", code_verifier="verifier", expected_nonce=nonce
    )
    assert identity.subject == "subject"
    assert observed_form["client_id"] == ["client-id"]
    assert observed_form["client_secret"] == [TEST_CLIENT_SECRET]
    assert observed_form["code_verifier"] == ["verifier"]

    with pytest.raises(OidcProtocolError, match="claims"):
        await provider.exchange_code(
            code="authorization-code", code_verifier="verifier", expected_nonce="wrong-nonce"
        )
