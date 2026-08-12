from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecretBox:
    """Small AES-256-GCM envelope used for settings and logout hints."""

    VERSION = b"\x01"

    def __init__(self, secret: str) -> None:
        self._key = hashlib.sha256(secret.encode("utf-8")).digest()

    def encrypt(self, plaintext: str, *, purpose: str) -> str:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, plaintext.encode(), purpose.encode())
        return base64.urlsafe_b64encode(self.VERSION + nonce + ciphertext).decode()

    def decrypt(self, envelope: str, *, purpose: str) -> str:
        raw = base64.urlsafe_b64decode(envelope.encode())
        if len(raw) < 30 or raw[:1] != self.VERSION:
            raise ValueError("unsupported encrypted value")
        plaintext = AESGCM(self._key).decrypt(raw[1:13], raw[13:], purpose.encode())
        return plaintext.decode()
