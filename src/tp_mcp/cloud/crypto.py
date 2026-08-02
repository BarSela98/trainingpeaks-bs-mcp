"""Envelope interfaces for secrets stored in Firestore."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def encryption_context(kind: str, identifier: str) -> bytes:
    """Build stable authenticated context for KMS/AES-GCM operations."""
    return f"trainingpeaks-mcp:v1:{kind}:{identifier}".encode()


class RecordCipher(Protocol):
    async def encrypt(self, plaintext: str, *, context: bytes) -> str: ...

    async def decrypt(self, ciphertext: str, *, context: bytes) -> str: ...


class CloudKMSCipher:
    """Cloud KMS adapter with required additional authenticated data."""

    def __init__(self, key_name: str) -> None:
        try:
            from google.cloud.kms_v1 import KeyManagementServiceAsyncClient
        except ImportError as exc:  # pragma: no cover - base-only installation
            raise RuntimeError("Cloud support is not installed; install tp-mcp[cloud]") from exc
        self._key_name = key_name
        self._client_class = KeyManagementServiceAsyncClient
        self._client = None
        self._client_loop: asyncio.AbstractEventLoop | None = None

    def _client_for_running_loop(self):
        loop = asyncio.get_running_loop()
        if self._client is None or self._client_loop is not loop:
            self._client = self._client_class()
            self._client_loop = loop
        return self._client

    async def encrypt(self, plaintext: str, *, context: bytes) -> str:
        response = await self._client_for_running_loop().encrypt(
            request={
                "name": self._key_name,
                "plaintext": plaintext.encode("utf-8"),
                "additional_authenticated_data": context,
            }
        )
        return f"kms:v1:{_b64encode(response.ciphertext)}"

    async def decrypt(self, ciphertext: str, *, context: bytes) -> str:
        prefix = "kms:v1:"
        if not ciphertext.startswith(prefix):
            raise ValueError("Unsupported encrypted-record format")
        response = await self._client_for_running_loop().decrypt(
            request={
                "name": self._key_name,
                "ciphertext": _b64decode(ciphertext.removeprefix(prefix)),
                "additional_authenticated_data": context,
            }
        )
        return response.plaintext.decode("utf-8")


class LocalAesGcmCipher:
    """Test/local adapter with the same authenticated-context semantics as KMS."""

    def __init__(self, secret: str | bytes) -> None:
        material = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._key = hashlib.sha256(material).digest()

    async def encrypt(self, plaintext: str, *, context: bytes) -> str:
        nonce = os.urandom(12)
        encrypted = AESGCM(self._key).encrypt(nonce, plaintext.encode("utf-8"), context)
        return f"local:v1:{_b64encode(nonce + encrypted)}"

    async def decrypt(self, ciphertext: str, *, context: bytes) -> str:
        prefix = "local:v1:"
        if not ciphertext.startswith(prefix):
            raise ValueError("Unsupported encrypted-record format")
        payload = _b64decode(ciphertext.removeprefix(prefix))
        plaintext = AESGCM(self._key).decrypt(payload[:12], payload[12:], context)
        return plaintext.decode("utf-8")
