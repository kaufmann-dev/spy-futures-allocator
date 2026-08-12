"""Standards-oriented OIDC client using discovery, Authlib, and JOSE verification."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Callable
from typing import Any, Protocol, cast
from urllib.parse import urlencode, urlsplit

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client  # type: ignore[import-untyped]
from joserfc import jwt
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry

from .models import OidcIdentity
from .settings import AuthSettings


class OidcProtocolError(RuntimeError):
    pass


class OidcClient(Protocol):
    async def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str: ...

    async def exchange_code(
        self, *, code: str, code_verifier: str, expected_nonce: str
    ) -> OidcIdentity: ...

    async def logout_url(self, *, id_token_hint: str, state: str) -> str: ...


HttpClientFactory = Callable[[], httpx.AsyncClient]
OAuthClientFactory = Callable[..., AsyncOAuth2Client]


class OidcProvider:
    SAFE_SIGNING_ALGORITHMS = frozenset(
        {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}
    )

    def __init__(
        self,
        settings: AuthSettings,
        *,
        http_client_factory: HttpClientFactory | None = None,
        oauth_client_factory: OAuthClientFactory = AsyncOAuth2Client,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=10, follow_redirects=False)
        )
        self._oauth_client_factory = oauth_client_factory
        self._clock = clock
        self._metadata: dict[str, Any] | None = None
        self._metadata_loaded_at = 0.0
        self._jwks: dict[str, Any] | None = None
        self._jwks_loaded_at = 0.0

    @property
    def discovery_url(self) -> str:
        return f"{self.settings.issuer_url}/.well-known/openid-configuration"

    async def _get_json(self, url: str) -> dict[str, Any]:
        async with self._http_client_factory() as client:
            response = await client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            value = response.json()
        if not isinstance(value, dict):
            raise OidcProtocolError("OIDC endpoint returned a non-object JSON response")
        return value

    def _validate_endpoint(self, metadata: dict[str, Any], name: str) -> str:
        value = metadata.get(name)
        if not isinstance(value, str):
            raise OidcProtocolError(f"OIDC discovery is missing {name}")
        parsed = urlsplit(value)
        allowed_schemes = {"https"} | ({"http"} if self.settings.allow_insecure_http else set())
        if parsed.scheme not in allowed_schemes or not parsed.hostname or parsed.fragment:
            raise OidcProtocolError(f"OIDC discovery contains an invalid {name}")
        return value

    async def metadata(self, *, force: bool = False) -> dict[str, Any]:
        now = self._clock()
        if not force and self._metadata is not None and now - self._metadata_loaded_at < 900:
            return self._metadata
        metadata = await self._get_json(self.discovery_url)
        if metadata.get("issuer") != self.settings.issuer_url:
            raise OidcProtocolError("OIDC discovery issuer does not match OIDC_ISSUER_URL")
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            self._validate_endpoint(metadata, name)
        methods = metadata.get("token_endpoint_auth_methods_supported")
        if isinstance(methods, list) and "client_secret_post" not in methods:
            raise OidcProtocolError("OIDC provider does not advertise client_secret_post")
        challenges = metadata.get("code_challenge_methods_supported")
        if isinstance(challenges, list) and "S256" not in challenges:
            raise OidcProtocolError("OIDC provider does not advertise PKCE S256")
        self._metadata = metadata
        self._metadata_loaded_at = now
        return metadata

    async def _load_jwks(self, metadata: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        now = self._clock()
        if not force and self._jwks is not None and now - self._jwks_loaded_at < 900:
            return self._jwks
        jwks = await self._get_json(self._validate_endpoint(metadata, "jwks_uri"))
        if not isinstance(jwks.get("keys"), list) or not jwks["keys"]:
            raise OidcProtocolError("OIDC JWKS is empty")
        self._jwks = jwks
        self._jwks_loaded_at = now
        return jwks

    async def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        metadata = await self.metadata()
        parameters = {
            "response_type": "code",
            "client_id": self.settings.client_id,
            "redirect_uri": self.settings.callback_url,
            "scope": "openid profile email",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        endpoint = self._validate_endpoint(metadata, "authorization_endpoint")
        return f"{endpoint}?{urlencode(parameters)}"

    async def exchange_code(
        self, *, code: str, code_verifier: str, expected_nonce: str
    ) -> OidcIdentity:
        metadata = await self.metadata()
        client = self._oauth_client_factory(
            client_id=self.settings.client_id,
            client_secret=self.settings.client_secret,
            redirect_uri=self.settings.callback_url,
            scope="openid profile email",
            token_endpoint_auth_method="client_secret_post",  # noqa: S106 -- OIDC method name
            code_challenge_method="S256",
            timeout=10,
        )
        try:
            token = await client.fetch_token(
                self._validate_endpoint(metadata, "token_endpoint"),
                grant_type="authorization_code",
                code=code,
                code_verifier=code_verifier,
            )
        except Exception as exc:
            raise OidcProtocolError("OIDC authorization-code exchange failed") from exc
        finally:
            await client.aclose()

        id_token = token.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise OidcProtocolError("OIDC token response did not include an ID token")
        claims = await self._validate_id_token(
            id_token,
            metadata,
            expected_nonce=expected_nonce,
            authorization_code=code,
            access_token=token.get("access_token"),
        )
        return OidcIdentity(subject=claims["sub"], id_token=id_token)

    async def _validate_id_token(
        self,
        id_token: str,
        metadata: dict[str, Any],
        *,
        expected_nonce: str,
        authorization_code: str,
        access_token: object,
    ) -> dict[str, Any]:
        advertised = metadata.get("id_token_signing_alg_values_supported", ["RS256"])
        algorithms = [
            value
            for value in advertised
            if isinstance(value, str) and value in self.SAFE_SIGNING_ALGORITHMS
        ]
        if not algorithms:
            raise OidcProtocolError(
                "OIDC provider advertises no accepted ID-token signing algorithm"
            )

        last_error: Exception | None = None
        for force in (False, True):
            try:
                serialized_jwks = await self._load_jwks(metadata, force=force)
                key_set = KeySet.import_key_set(cast(Any, serialized_jwks))
                token = jwt.decode(id_token, key_set, algorithms=algorithms)
                break
            except Exception as exc:
                last_error = exc
        else:
            raise OidcProtocolError("OIDC ID-token signature is invalid") from last_error

        claims = token.claims
        registry = JWTClaimsRegistry(
            now=int(self._clock()),
            leeway=self.settings.clock_skew_seconds,
            iss={"essential": True, "value": self.settings.issuer_url},
            aud={"essential": True},
            exp={"essential": True},
            iat={"essential": True},
            sub={"essential": True},
            nonce={"essential": True, "value": expected_nonce},
        )
        try:
            registry.validate(claims)
        except Exception as exc:
            raise OidcProtocolError("OIDC ID-token claims are invalid") from exc

        audience = claims.get("aud")
        audiences = [audience] if isinstance(audience, str) else audience
        if not isinstance(audiences, list) or self.settings.client_id not in audiences:
            raise OidcProtocolError("OIDC ID-token audience is invalid")
        if len(audiences) > 1 and claims.get("azp") != self.settings.client_id:
            raise OidcProtocolError("OIDC ID-token authorized party is invalid")
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise OidcProtocolError("OIDC ID-token subject is invalid")

        algorithm = token.header.get("alg")
        if not isinstance(algorithm, str):
            raise OidcProtocolError("OIDC ID-token signing algorithm is invalid")
        if "c_hash" in claims and not self._validate_hash_claim(
            claims["c_hash"], authorization_code, algorithm
        ):
            raise OidcProtocolError("OIDC ID-token code hash is invalid")
        if "at_hash" in claims and (
            not isinstance(access_token, str)
            or not self._validate_hash_claim(claims["at_hash"], access_token, algorithm)
        ):
            raise OidcProtocolError("OIDC ID-token access-token hash is invalid")
        return claims

    @staticmethod
    def _validate_hash_claim(claim: object, value: str, algorithm: str) -> bool:
        try:
            bits = int(algorithm[-3:])
            digest = hashlib.new(f"sha{bits}", value.encode("ascii")).digest()
        except (ValueError, UnicodeEncodeError):
            return False
        expected = base64.urlsafe_b64encode(digest[: len(digest) // 2]).rstrip(b"=").decode("ascii")
        return isinstance(claim, str) and hmac.compare_digest(claim, expected)

    async def logout_url(self, *, id_token_hint: str, state: str) -> str:
        metadata = await self.metadata()
        endpoint = self._validate_endpoint(metadata, "end_session_endpoint")
        parameters = {
            "id_token_hint": id_token_hint,
            "post_logout_redirect_uri": self.settings.logout_callback_url,
            "state": state,
        }
        return f"{endpoint}?{urlencode(parameters)}"
