"""OAuth 2.1 persistence, rotation, and allowlist enforcement tests."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.auth.provider import AuthorizationParams, ProviderTokenVerifier, RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.requests import HTTPConnection

from tp_mcp.cloud.oauth import REFRESH_TOKEN_TTL_SECONDS, document_key, email_key
from tp_mcp.cloud.storage import (
    ALLOWLIST,
    OAUTH_ACCESS_TOKENS,
    OAUTH_CLIENTS,
    OAUTH_CODES,
    OAUTH_GRANTS,
    OAUTH_REFRESH_TOKENS,
    TRAININGPEAKS_IDENTITIES,
    USERS,
)
from tp_mcp.cloud.web import create_http_app

from .conftest import TEST_CALLBACK, TEST_RESOURCE_URL


@pytest.mark.asyncio
async def test_dynamic_client_registration_encrypts_client_secret(provider, store, oauth_client) -> None:
    await provider.register_client(oauth_client)

    document = await store.get(OAUTH_CLIENTS, oauth_client.client_id)
    assert document is not None
    assert document["metadata"].get("client_secret") is None
    assert "client-secret-value" not in repr(document)
    assert str(document["client_secret_ciphertext"]).startswith("local:v1:")

    loaded = await provider.get_client(oauth_client.client_id)
    assert loaded is not None
    assert loaded.client_secret == "client-secret-value"
    assert [str(uri) for uri in loaded.redirect_uris or []] == [TEST_CALLBACK]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://attacker.example/callback",
        "https://localhost/callback",
        "http://attacker.example/callback",
        "http://127.0.0.1/callback#fragment",
        "http://attacker@127.0.0.1/callback",
    ],
)
async def test_dynamic_client_registration_rejects_unapproved_redirects(provider, redirect_uri: str) -> None:
    client = OAuthClientInformationFull(
        client_id="untrusted-client",
        redirect_uris=[AnyUrl(redirect_uri)],
    )

    with pytest.raises(RegistrationError) as error:
        await provider.register_client(client)

    assert error.value.error == "invalid_redirect_uri"


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_uri", [TEST_CALLBACK, "http://localhost:43123/callback", "http://[::1]:43123/cb"])
async def test_dynamic_client_registration_allows_claude_and_loopback(provider, redirect_uri: str) -> None:
    client = OAuthClientInformationFull(
        client_id=f"client-{abs(hash(redirect_uri))}",
        redirect_uris=[AnyUrl(redirect_uri)],
    )

    await provider.register_client(client)

    assert await provider.get_client(client.client_id) is not None


@pytest.mark.asyncio
async def test_authorization_code_preserves_pkce_and_is_single_use(provider, oauth_client, issue_grant) -> None:
    identity, authorization_code, tokens = await issue_grant()

    assert authorization_code.code_challenge == "M" * 43
    assert authorization_code.subject == identity.subject
    assert authorization_code.resource == TEST_RESOURCE_URL
    assert tokens.access_token.startswith("tpa_")
    assert tokens.refresh_token is not None and tokens.refresh_token.startswith("tpr_")

    # A consumed record is returned so the SDK can verify PKCE before replay
    # handling revokes the issued grant family.
    replay = await provider.load_authorization_code(oauth_client, authorization_code.code)
    assert replay is not None
    with pytest.raises(TokenError) as error:
        await provider.exchange_authorization_code(oauth_client, replay)
    assert error.value.error == "invalid_grant"
    assert error.value.error_description == "Authorization code was already used"
    assert await provider.load_access_token(tokens.access_token) is None


@pytest.mark.asyncio
async def test_authorization_code_expiry_is_enforced(
    provider,
    store,
    oauth_client,
    enroll_subject,
) -> None:
    identity = await enroll_subject()
    await provider.register_client(oauth_client)
    google_redirect = await provider.authorize(
        oauth_client,
        AuthorizationParams(
            state="client-state",
            scopes=["trainingpeaks:read"],
            code_challenge="P" * 43,
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
    code = parse_qs(urlsplit(client_redirect).query)["code"][0]
    await store.put(
        OAUTH_CODES,
        document_key("authorization-code", code),
        {"expires_at": time.time() - 1},
        merge=True,
    )

    assert await provider.load_authorization_code(oauth_client, code) is None


@pytest.mark.asyncio
async def test_refresh_rotation_revokes_previous_pair_and_rejects_replay(
    provider,
    oauth_client,
    issue_grant,
) -> None:
    _, _, initial = await issue_grant()
    assert initial.refresh_token is not None
    refresh = await provider.load_refresh_token(oauth_client, initial.refresh_token)
    assert refresh is not None

    rotated = await provider.exchange_refresh_token(oauth_client, refresh, list(refresh.scopes))

    assert rotated.access_token != initial.access_token
    assert rotated.refresh_token != initial.refresh_token
    assert await provider.load_access_token(initial.access_token) is None
    assert await provider.load_access_token(rotated.access_token) is not None

    # Presenting a consumed refresh token is replay detection and revokes the
    # entire rotated family, including an attacker's possible successor.
    assert await provider.load_refresh_token(oauth_client, initial.refresh_token) is None
    assert await provider.load_access_token(rotated.access_token) is None
    assert rotated.refresh_token is not None
    assert await provider.load_refresh_token(oauth_client, rotated.refresh_token) is None
    with pytest.raises(TokenError) as error:
        await provider.exchange_refresh_token(oauth_client, refresh, list(refresh.scopes))
    assert error.value.error == "invalid_grant"
    assert error.value.error_description == "Refresh token was already used"


@pytest.mark.asyncio
async def test_expired_access_token_is_rejected_by_bearer_but_remains_revocable(
    provider,
    store,
    oauth_client,
    issue_grant,
) -> None:
    _, _, tokens = await issue_grant()
    assert tokens.refresh_token is not None
    access_id = document_key("access-token", tokens.access_token)
    access_document = await store.get(OAUTH_ACCESS_TOKENS, access_id)
    assert access_document is not None
    cleanup_at = access_document["expires_at_timestamp"]
    assert cleanup_at.timestamp() > time.time() + REFRESH_TOKEN_TTL_SECONDS - 60
    await store.put(
        OAUTH_ACCESS_TOKENS,
        access_id,
        {"expires_at": int(time.time()) - 1},
        merge=True,
    )

    expired_access = await provider.load_access_token(tokens.access_token)
    assert expired_access is not None
    backend = BearerAuthBackend(ProviderTokenVerifier(provider))
    connection = HTTPConnection(
        {
            "type": "http",
            "headers": [(b"authorization", f"Bearer {tokens.access_token}".encode())],
        }
    )
    assert await backend.authenticate(connection) is None

    await provider.revoke_token(expired_access)

    assert await provider.load_access_token(tokens.access_token) is None
    assert await provider.load_refresh_token(oauth_client, tokens.refresh_token) is None


@pytest.mark.asyncio
async def test_expired_refresh_token_is_rejected(provider, store, oauth_client, issue_grant) -> None:
    _, _, tokens = await issue_grant()
    assert tokens.refresh_token is not None
    await store.put(
        OAUTH_REFRESH_TOKENS,
        document_key("refresh-token", tokens.refresh_token),
        {"expires_at": time.time() - 1},
        merge=True,
    )

    assert await provider.load_refresh_token(oauth_client, tokens.refresh_token) is None


@pytest.mark.asyncio
async def test_sdk_revoke_endpoint_revokes_refresh_family_from_expired_access_token(
    cloud_config,
    store,
    cipher,
    provider,
    oauth_client,
    issue_grant,
) -> None:
    _, _, tokens = await issue_grant()
    assert tokens.refresh_token is not None
    await store.put(
        OAUTH_ACCESS_TOKENS,
        document_key("access-token", tokens.access_token),
        {"expires_at": int(time.time()) - 1},
        merge=True,
    )
    app = create_http_app(cloud_config, store=store, cipher=cipher)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=cloud_config.base_url,
    ) as client:
        response = await client.post(
            "/revoke",
            data={
                "token": tokens.access_token,
                "token_type_hint": "access_token",
                "client_id": oauth_client.client_id,
                "client_secret": oauth_client.client_secret,
            },
        )

    assert response.status_code == 200
    assert await provider.load_refresh_token(oauth_client, tokens.refresh_token) is None


@pytest.mark.asyncio
async def test_disabling_allowlist_immediately_blocks_existing_tokens(
    provider,
    store,
    oauth_client,
    issue_grant,
) -> None:
    identity, _, tokens = await issue_grant()
    assert tokens.refresh_token is not None
    assert await provider.load_access_token(tokens.access_token) is not None

    await store.put(ALLOWLIST, email_key(identity.email), {"enabled": False}, merge=True)

    assert await provider.load_access_token(tokens.access_token) is None
    assert await provider.load_refresh_token(oauth_client, tokens.refresh_token) is None


@pytest.mark.asyncio
async def test_revocation_removes_entire_grant(provider, oauth_client, issue_grant) -> None:
    _, _, tokens = await issue_grant()
    assert tokens.refresh_token is not None
    access = await provider.load_access_token(tokens.access_token)
    assert access is not None

    await provider.revoke_token(access)

    assert await provider.load_access_token(tokens.access_token) is None
    assert await provider.load_refresh_token(oauth_client, tokens.refresh_token) is None


@pytest.mark.asyncio
async def test_oauth_tokens_are_never_stored_in_plaintext(store, issue_grant) -> None:
    _, authorization_code, tokens = await issue_grant()
    assert tokens.refresh_token is not None

    all_records = {
        collection: await store.scan(collection)
        for collection in (OAUTH_CODES, OAUTH_ACCESS_TOKENS, OAUTH_REFRESH_TOKENS)
    }
    serialized = repr(all_records)
    assert authorization_code.code not in serialized
    assert tokens.access_token not in serialized
    assert tokens.refresh_token not in serialized
    assert document_key("access-token", tokens.access_token) in {
        document_id for document_id, _ in all_records[OAUTH_ACCESS_TOKENS]
    }


@pytest.mark.asyncio
async def test_oauth_issuance_needs_no_trainingpeaks_credential_and_persists_none(store, issue_grant) -> None:
    identity, _, tokens = await issue_grant()

    assert tokens.access_token.startswith("tpa_")
    assert tokens.refresh_token is not None
    user = await store.get(USERS, identity.subject)
    assert user is not None
    assert user["email"] == identity.email
    assert await store.get(TRAININGPEAKS_IDENTITIES, identity.subject) is None
    assert len(await store.scan(OAUTH_GRANTS)) == 1

    serialized_store = repr(store._documents)
    assert "credential_ciphertext" not in serialized_store
    assert "trainingpeaks_auth" not in serialized_store.casefold()
    assert "tp-cookie" not in serialized_store
