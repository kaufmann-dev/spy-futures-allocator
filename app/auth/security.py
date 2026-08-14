"""Small security primitives for local sessions and OIDC transactions."""

from __future__ import annotations

import base64
import hashlib
import posixpath
import secrets
from urllib.parse import unquote, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AUTH_PATH_PREFIXES = ("/auth/",)
AUTH_PATHS = {"/login", "/logout"}


def random_token(byte_count: int = 32) -> str:
    return secrets.token_urlsafe(byte_count)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def sanitize_return_to(value: str | None, default: str = "/") -> str:
    """Return a canonical same-origin path, never an auth-loop destination."""

    if not value or len(value) > 2048:
        return default
    candidate = value
    for _ in range(3):
        if any(character in candidate for character in ("\\", "\r", "\n", "\0")):
            return default
        if candidate.startswith("//"):
            return default
        parsed = urlsplit(candidate)
        if (
            parsed.scheme
            or parsed.netloc
            or not parsed.path.startswith("/")
            or parsed.path.startswith("//")
        ):
            return default
        decoded = unquote(candidate)
        if decoded == candidate:
            break
        candidate = decoded

    parsed = urlsplit(candidate)
    normalized_path = posixpath.normpath(parsed.path)
    if not normalized_path.startswith("/"):
        return default
    if normalized_path in AUTH_PATHS or normalized_path.startswith(AUTH_PATH_PREFIXES):
        return default
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{parsed.path}{query}{fragment}"


class IdTokenCipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
        self._cipher = AESGCM(key)

    @staticmethod
    def _aad(session_token_hash: str) -> bytes:
        return f"spy-futures-allocator:oidc-id-token:{session_token_hash}".encode("ascii")

    def encrypt(self, id_token: str, session_token_hash: str) -> tuple[bytes, bytes]:
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(
            nonce,
            id_token.encode("utf-8"),
            self._aad(session_token_hash),
        )
        return ciphertext, nonce

    def decrypt(self, ciphertext: bytes, nonce: bytes, session_token_hash: str) -> str:
        plaintext = self._cipher.decrypt(nonce, ciphertext, self._aad(session_token_hash))
        return plaintext.decode("utf-8")
