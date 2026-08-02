"""Google onboarding tests for request-scoped TrainingPeaks credentials."""

from __future__ import annotations

import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp.server.auth.provider import AuthorizationParams
from pydantic import AnyUrl

from tp_mcp.auth import AuthResult
from tp_mcp.cloud.oauth import GoogleIdentity, document_key, email_key
from tp_mcp.cloud.storage import (
    ALLOWLIST,
    BROWSER_FLOWS,
    ENROLLMENTS,
    OAUTH_CONSENTS,
    OAUTH_GRANTS,
    TRAININGPEAKS_IDENTITIES,
    USERS,
)
from tp_mcp.cloud.web import create_http_app

from .conftest import TEST_CALLBACK, TEST_RESOURCE_URL


class FakeGoogleIdentityProvider:
    def __init__(self, identity: GoogleIdentity) -> None:
        self.identity = identity
        self.calls: list[tuple[str, str, str]] = []

    async def authenticate(self, code: str, *, redirect_uri: str, nonce_digest: str) -> GoogleIdentity:
        self.calls.append((code, redirect_uri, nonce_digest))
        return self.identity


class _ConsentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.value: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name") == "consent":
            self.value = attributes.get("value")


def _consent_value(response: httpx.Response) -> str:
    parser = _ConsentParser()
    parser.feed(response.text)
    assert parser.value is not None
    return parser.value


async def _start_authorization(provider, oauth_client, *, scopes: list[str] | None = None) -> str:
    await provider.register_client(oauth_client)
    google_redirect = await provider.authorize(
        oauth_client,
        AuthorizationParams(
            state="client-state",
            scopes=scopes or ["trainingpeaks:read"],
            code_challenge="C" * 43,
            redirect_uri=AnyUrl(TEST_CALLBACK),
            redirect_uri_provided_explicitly=True,
            resource=TEST_RESOURCE_URL,
        ),
    )
    return parse_qs(urlsplit(google_redirect).query)["state"][0]


@pytest.mark.asyncio
async def test_google_callback_issues_grant_without_trainingpeaks_credential(
    cloud_config,
    store,
    cipher,
    oauth_client,
) -> None:
    identity = GoogleIdentity(subject="google-subject-1", email="athlete@example.com", name="Athlete")
    google = FakeGoogleIdentityProvider(identity)
    await store.put(ALLOWLIST, email_key(identity.email), {"email": identity.email, "enabled": True})
    validation_calls: list[str] = []

    async def validator(credential: str) -> AuthResult:
        validation_calls.append(credential)
        raise AssertionError("TrainingPeaks credentials must not be requested during MCP OAuth")

    app = create_http_app(cloud_config, store=store, cipher=cipher, google=google, validator=validator)
    provider = app.state.cloud_provider
    state = await _start_authorization(provider, oauth_client)
    google_code = "google-code-must-not-be-persisted"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=cloud_config.base_url,
    ) as client:
        callback_response = await client.post(
            "/oauth/google/callback",
            data={"state": state, "code": google_code},
        )
        assert callback_response.status_code == 200
        assert "trainingpeaks:read" in callback_response.text
        assert await store.scan(OAUTH_GRANTS) == []
        consent_value = _consent_value(callback_response)
        assert consent_value not in repr(store._documents)
        response = await client.post("/oauth/confirm", data={"consent": consent_value})

    assert response.status_code == 302
    location = response.headers["location"]
    query = parse_qs(urlsplit(location).query)
    assert location.startswith(TEST_CALLBACK)
    assert query["state"] == ["client-state"]
    authorization_code_value = query["code"][0]
    authorization_code = await provider.load_authorization_code(oauth_client, authorization_code_value)
    assert authorization_code is not None
    tokens = await provider.exchange_authorization_code(oauth_client, authorization_code)
    assert tokens.access_token.startswith("tpa_")
    assert tokens.refresh_token is not None

    assert validation_calls == []
    assert google.calls and google.calls[0][0] == google_code
    assert "set-cookie" not in callback_response.headers
    assert "set-cookie" not in response.headers
    user = await store.get(USERS, identity.subject)
    assert user is not None
    assert user["email"] == identity.email
    assert user["google_name"] == identity.name
    assert await store.get(TRAININGPEAKS_IDENTITIES, identity.subject) is None
    assert await store.scan(ENROLLMENTS) == []
    assert await store.scan(BROWSER_FLOWS) == []
    serialized_store = repr(store._documents)
    assert google_code not in serialized_store
    assert "credential_ciphertext" not in serialized_store
    assert "trainingpeaks_auth" not in serialized_store.casefold()
    assert consent_value not in serialized_store


@pytest.mark.asyncio
async def test_google_callback_rejects_uninvited_identity_without_creating_grant(
    cloud_config,
    store,
    cipher,
    oauth_client,
) -> None:
    identity = GoogleIdentity(subject="uninvited-subject", email="uninvited@example.com")
    google = FakeGoogleIdentityProvider(identity)
    app = create_http_app(cloud_config, store=store, cipher=cipher, google=google)
    state = await _start_authorization(app.state.cloud_provider, oauth_client)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=cloud_config.base_url,
    ) as client:
        response = await client.post(
            "/oauth/google/callback",
            data={"state": state, "code": "uninvited-google-code"},
        )

    assert response.status_code == 403
    assert "Invitation required" in response.text
    assert await store.get(USERS, identity.subject) is None
    assert await store.scan(OAUTH_GRANTS) == []
    assert await store.scan(ENROLLMENTS) == []
    assert await store.scan(BROWSER_FLOWS) == []


@pytest.mark.asyncio
async def test_google_callback_state_is_single_use(cloud_config, store, cipher, oauth_client) -> None:
    identity = GoogleIdentity(subject="google-subject-1", email="athlete@example.com")
    google = FakeGoogleIdentityProvider(identity)
    await store.put(ALLOWLIST, email_key(identity.email), {"email": identity.email, "enabled": True})
    app = create_http_app(cloud_config, store=store, cipher=cipher, google=google)
    state = await _start_authorization(app.state.cloud_provider, oauth_client)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=cloud_config.base_url,
    ) as client:
        first = await client.post(
            "/oauth/google/callback",
            data={"state": state, "code": "first-google-code"},
        )
        replay = await client.post(
            "/oauth/google/callback",
            data={"state": state, "code": "replayed-google-code"},
        )

    assert first.status_code == 200
    assert replay.status_code == 400
    assert "expired or was already used" in replay.text
    assert [call[0] for call in google.calls] == ["first-google-code"]


@pytest.mark.asyncio
async def test_write_scope_requires_explicit_one_time_confirmation(
    cloud_config,
    store,
    cipher,
    oauth_client,
) -> None:
    identity = GoogleIdentity(subject="google-subject-1", email="athlete@example.com")
    google = FakeGoogleIdentityProvider(identity)
    await store.put(ALLOWLIST, email_key(identity.email), {"email": identity.email, "enabled": True})
    app = create_http_app(cloud_config, store=store, cipher=cipher, google=google)
    state = await _start_authorization(
        app.state.cloud_provider,
        oauth_client,
        scopes=["trainingpeaks:read", "trainingpeaks:write"],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=cloud_config.base_url,
    ) as client:
        callback = await client.post(
            "/oauth/google/callback",
            data={"state": state, "code": "google-code"},
        )
        consent_value = _consent_value(callback)
        assert await store.scan(OAUTH_GRANTS) == []
        approved = await client.post("/oauth/confirm", data={"consent": consent_value})
        replay = await client.post("/oauth/confirm", data={"consent": consent_value})

    assert callback.status_code == 200
    assert "trainingpeaks:write" in callback.text
    assert "Create, update, and delete" in callback.text
    assert approved.status_code == 302
    assert replay.status_code == 400
    assert "expired or was already used" in replay.text
    assert len(await store.scan(OAUTH_GRANTS)) == 1


@pytest.mark.asyncio
async def test_authorization_confirmation_expires_without_issuing_grant(
    cloud_config,
    store,
    cipher,
    oauth_client,
) -> None:
    identity = GoogleIdentity(subject="google-subject-1", email="athlete@example.com")
    google = FakeGoogleIdentityProvider(identity)
    await store.put(ALLOWLIST, email_key(identity.email), {"email": identity.email, "enabled": True})
    app = create_http_app(cloud_config, store=store, cipher=cipher, google=google)
    state = await _start_authorization(app.state.cloud_provider, oauth_client)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=cloud_config.base_url,
    ) as client:
        callback = await client.post(
            "/oauth/google/callback",
            data={"state": state, "code": "google-code"},
        )
        consent_value = _consent_value(callback)
        await store.put(
            OAUTH_CONSENTS,
            document_key("authorization-consent", consent_value),
            {"expires_at": time.time() - 1},
            merge=True,
        )
        expired = await client.post("/oauth/confirm", data={"consent": consent_value})

    assert expired.status_code == 400
    assert await store.scan(OAUTH_GRANTS) == []


@pytest.mark.asyncio
async def test_google_callback_rejects_query_string_codes(cloud_config, store, cipher) -> None:
    app = create_http_app(cloud_config, store=store, cipher=cipher)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=cloud_config.base_url,
    ) as client:
        response = await client.get(
            "/oauth/google/callback",
            params={"state": "must-not-be-logged", "code": "must-not-be-logged"},
        )
        confirmation = await client.get("/oauth/confirm", params={"consent": "must-not-be-logged"})

    assert response.status_code == 405
    assert confirmation.status_code == 405
