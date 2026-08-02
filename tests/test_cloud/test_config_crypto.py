"""Configuration and encryption boundary tests."""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from tp_mcp.cloud.config import CloudConfig
from tp_mcp.cloud.crypto import LocalAesGcmCipher, encryption_context


def _environment(**overrides: str) -> dict[str, str]:
    environment = {
        "TP_MCP_BASE_URL": "https://training.example/",
        "GOOGLE_CLOUD_PROJECT": "trainingpeaks-test",
        "TP_MCP_KMS_KEY": "projects/test/locations/me-west1/keyRings/tp/cryptoKeys/tp-mcp-oauth",
        "GOOGLE_OAUTH_CLIENT_ID": "google-client-id",
        "GOOGLE_OAUTH_CLIENT_SECRET": "google-client-secret",
        "PORT": "8080",
    }
    environment.update(overrides)
    return environment


def test_cloud_config_canonicalizes_https_url() -> None:
    config = CloudConfig.from_env(_environment(TP_MCP_BASE_URL="https://Training.Example/"))

    assert config.base_url == "https://Training.Example"
    assert config.resource_url == "https://Training.Example/mcp"
    assert config.google_redirect_uri == "https://Training.Example/oauth/google/callback"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://training.example",
        "ftp://training.example",
        "training.example",
        "https://user:password@training.example",
        "https://training.example?next=elsewhere",
        "https://training.example#fragment",
        "https://training.example/path",
    ],
)
def test_cloud_config_rejects_insecure_or_ambiguous_base_urls(base_url: str) -> None:
    with pytest.raises(ValueError, match="TP_MCP_BASE_URL"):
        CloudConfig.from_env(_environment(TP_MCP_BASE_URL=base_url))


@pytest.mark.parametrize("base_url", ["http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080"])
def test_cloud_config_allows_plain_http_only_for_loopback(base_url: str) -> None:
    assert CloudConfig.from_env(_environment(TP_MCP_BASE_URL=base_url)).base_url == base_url


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("PORT", "0", "between 1 and 65535"),
        ("PORT", "65536", "between 1 and 65535"),
        ("GOOGLE_CLOUD_PROJECT", "", "GOOGLE_CLOUD_PROJECT is required"),
    ],
)
def test_cloud_config_validates_required_security_settings(name: str, value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CloudConfig.from_env(_environment(**{name: value}))


@pytest.mark.asyncio
async def test_local_cipher_binds_ciphertext_to_oauth_record_context() -> None:
    cipher = LocalAesGcmCipher("shared-local-key")
    first_context = encryption_context("oauth-client", "client-one")
    second_context = encryption_context("oauth-client", "client-two")

    ciphertext = await cipher.encrypt("client-secret", context=first_context)

    assert "client-secret" not in ciphertext
    assert await cipher.decrypt(ciphertext, context=first_context) == "client-secret"
    with pytest.raises(InvalidTag):
        await cipher.decrypt(ciphertext, context=second_context)
