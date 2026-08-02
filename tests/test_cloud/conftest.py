"""Fixtures for cloud tests that never require Google Cloud access."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from tp_mcp.cloud.config import CloudConfig
from tp_mcp.cloud.crypto import LocalAesGcmCipher
from tp_mcp.cloud.oauth import FirestoreOAuthProvider, GoogleIdentity, email_key
from tp_mcp.cloud.storage import ALLOWLIST, InMemoryCloudStore

TEST_BASE_URL = "https://training.example"
TEST_RESOURCE_URL = f"{TEST_BASE_URL}/mcp"
TEST_CALLBACK = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def cloud_config() -> CloudConfig:
    return CloudConfig(
        base_url=TEST_BASE_URL,
        project_id="trainingpeaks-test",
        kms_key_name=(
            "projects/trainingpeaks-test/locations/me-west1/keyRings/trainingpeaks-mcp/cryptoKeys/tp-mcp-oauth"
        ),
        google_client_id="google-client.apps.googleusercontent.com",
        google_client_secret="google-client-secret",
    )


@pytest.fixture
def store() -> InMemoryCloudStore:
    return InMemoryCloudStore()


@pytest.fixture
def cipher() -> LocalAesGcmCipher:
    return LocalAesGcmCipher("local-test-key-material")


@pytest.fixture
def provider(
    cloud_config: CloudConfig,
    store: InMemoryCloudStore,
    cipher: LocalAesGcmCipher,
) -> FirestoreOAuthProvider:
    return FirestoreOAuthProvider(cloud_config, store, cipher)


@pytest.fixture
def oauth_client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="claude-test-client",
        client_secret="client-secret-value",
        client_name="Claude test client",
        redirect_uris=[AnyUrl(TEST_CALLBACK)],
        scope="trainingpeaks:read trainingpeaks:write",
        token_endpoint_auth_method="client_secret_post",
    )


@pytest.fixture
def enroll_subject(
    provider: FirestoreOAuthProvider,
    store: InMemoryCloudStore,
) -> Callable[..., Awaitable[GoogleIdentity]]:
    async def enroll(
        *,
        subject: str = "google-subject-1",
        email: str = "athlete@example.com",
    ) -> GoogleIdentity:
        await store.put(
            ALLOWLIST,
            email_key(email),
            {"email": email, "enabled": True},
        )
        identity = GoogleIdentity(subject=subject, email=email, name="Test Athlete")
        assert await provider.bind_invited_identity(identity)
        return identity

    return enroll


@pytest.fixture
def issue_grant(
    provider: FirestoreOAuthProvider,
    oauth_client: OAuthClientInformationFull,
    enroll_subject: Callable[..., Awaitable[GoogleIdentity]],
):
    async def issue(
        *,
        scopes: list[str] | None = None,
        subject: str = "google-subject-1",
        email: str = "athlete@example.com",
    ):
        identity = await enroll_subject(subject=subject, email=email)
        await provider.register_client(oauth_client)
        challenge = "M" * 43
        google_redirect = await provider.authorize(
            oauth_client,
            AuthorizationParams(
                state="mcp-client-state",
                scopes=scopes or ["trainingpeaks:read", "trainingpeaks:write"],
                code_challenge=challenge,
                redirect_uri=AnyUrl(TEST_CALLBACK),
                redirect_uri_provided_explicitly=True,
                resource=TEST_RESOURCE_URL,
            ),
        )
        state = parse_qs(urlsplit(google_redirect).query)["state"][0]
        consumed = await provider.consume_google_transaction(state)
        assert consumed is not None
        transaction_id, _ = consumed
        consent = await provider.create_authorization_consent(transaction_id, identity.subject)
        client_redirect = await provider.complete_authorization(consent)
        authorization_code_value = parse_qs(urlsplit(client_redirect).query)["code"][0]
        authorization_code = await provider.load_authorization_code(oauth_client, authorization_code_value)
        assert authorization_code is not None
        tokens = await provider.exchange_authorization_code(oauth_client, authorization_code)
        return identity, authorization_code, tokens

    return issue
