"""HTTP transport protection, discovery, and remote tool filtering tests."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolRequestParams

from tp_mcp.cloud.web import create_http_app
from tp_mcp.server import _on_call_tool, _on_list_tools


@pytest.mark.asyncio
async def test_health_and_oauth_discovery_are_public(cloud_config, store, cipher) -> None:
    app = create_http_app(cloud_config, store=store, cipher=cipher)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=cloud_config.base_url,
    ) as client:
        health = await client.get("/healthz")
        authorization_metadata = await client.get("/.well-known/oauth-authorization-server")
        resource_metadata = await client.get("/.well-known/oauth-protected-resource/mcp")

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "transport": "streamable-http"}
    assert authorization_metadata.status_code == 200
    assert authorization_metadata.json()["issuer"] == cloud_config.issuer_url
    assert authorization_metadata.json()["registration_endpoint"].endswith("/register")
    assert resource_metadata.status_code == 200
    assert resource_metadata.json()["resource"] == cloud_config.resource_url
    assert resource_metadata.json()["authorization_servers"] == [cloud_config.issuer_url]


@pytest.mark.asyncio
async def test_unauthenticated_mcp_returns_oauth_metadata_challenge(cloud_config, store, cipher) -> None:
    app = create_http_app(cloud_config, store=store, cipher=cipher)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=cloud_config.base_url,
    ) as client:
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1"},
                },
            },
        )

    assert response.status_code == 401
    challenge = response.headers.get("www-authenticate", "")
    assert challenge.startswith("Bearer")
    assert "resource_metadata=" in challenge
    assert "/.well-known/oauth-protected-resource" in challenge


@pytest.mark.asyncio
async def test_authenticated_mcp_requires_per_request_trainingpeaks_auth(cloud_config, store, cipher) -> None:
    app = create_http_app(cloud_config, store=store, cipher=cipher)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=cloud_config.base_url,
    ) as client:
        missing = await client.post("/mcp", headers={"Authorization": "Bearer invalid"}, json={})
        oversized = await client.post(
            "/mcp",
            headers={"Authorization": "Bearer invalid", "X-TrainingPeaks-Auth": "x" * (16 * 1024 + 1)},
            json={},
        )
        supplied = await client.post(
            "/mcp",
            headers={"Authorization": "Bearer invalid", "X-TrainingPeaks-Auth": "request-only-cookie"},
            json={},
        )
        unsafe = await client.post(
            "/mcp",
            headers={"Authorization": "Bearer invalid", "X-TrainingPeaks-Auth": "one;OtherCookie=two"},
            json={},
        )
        duplicate_authorization = await client.post(
            "/mcp",
            headers=[
                ("Authorization", "Bearer invalid-one"),
                ("Authorization", "Bearer invalid-two"),
                ("X-TrainingPeaks-Auth", "request-only-cookie"),
            ],
            json={},
        )

    assert missing.status_code == 400
    assert missing.json() == {"error": "trainingpeaks_auth_required"}
    assert oversized.status_code == 431
    assert oversized.json() == {"error": "trainingpeaks_auth_too_large"}
    assert unsafe.status_code == 400
    assert unsafe.json() == {"error": "trainingpeaks_auth_invalid_format"}
    assert duplicate_authorization.status_code == 400
    assert duplicate_authorization.json() == {"error": "ambiguous_authorization"}
    # A bounded per-request credential reaches the normal OAuth verifier; the
    # guard neither persists nor treats it as an MCP bearer token.
    assert supplied.status_code == 401
    assert supplied.json() != {"error": "trainingpeaks_auth_required"}


@pytest.mark.asyncio
async def test_remote_read_only_grant_hides_refresh_and_write_tools() -> None:
    access_token = AccessToken(
        token="test-token",
        client_id="test-client",
        scopes=["trainingpeaks:read"],
        expires_at=4_000_000_000,
        resource="https://training.example/mcp",
        subject="google-subject-1",
    )
    context = SimpleNamespace(
        request=SimpleNamespace(scope={"user": AuthenticatedUser(access_token)}),
    )

    result = await _on_list_tools(context, None)

    names = {tool.name for tool in result.tools}
    assert "tp_refresh_auth" not in names
    assert "tp_create_workout" not in names
    assert "tp_delete_workout" not in names
    assert "tp_get_workouts" in names
    assert all(tool.annotations and tool.annotations.read_only_hint is True for tool in result.tools)


@pytest.mark.asyncio
async def test_remote_write_grant_still_hides_local_refresh_tool() -> None:
    access_token = AccessToken(
        token="test-token",
        client_id="test-client",
        scopes=["trainingpeaks:read", "trainingpeaks:write"],
        expires_at=4_000_000_000,
        resource="https://training.example/mcp",
        subject="google-subject-1",
    )
    context = SimpleNamespace(
        request=SimpleNamespace(scope={"user": AuthenticatedUser(access_token)}),
    )

    result = await _on_list_tools(context, None)
    names = {tool.name for tool in result.tools}

    assert "tp_refresh_auth" not in names
    assert "tp_get_workouts" in names
    assert "tp_create_workout" in names


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "expected_error"),
    [
        ("tp_create_workout", "INSUFFICIENT_SCOPE"),
        ("tp_refresh_auth", "REMOTE_TOOL_DISABLED"),
    ],
)
async def test_remote_direct_calls_cannot_bypass_scope_or_local_tool_policy(
    tool_name: str,
    expected_error: str,
) -> None:
    access_token = AccessToken(
        token="test-token",
        client_id="test-client",
        scopes=["trainingpeaks:read"],
        expires_at=4_000_000_000,
        resource="https://training.example/mcp",
        subject="google-subject-1",
    )
    context = SimpleNamespace(
        request=SimpleNamespace(scope={"user": AuthenticatedUser(access_token)}),
    )

    result = await _on_call_tool(context, CallToolRequestParams(name=tool_name, arguments={}))

    assert result.is_error is True
    assert expected_error in result.content[0].text


@pytest.mark.asyncio
async def test_stdio_tool_listing_remains_unchanged() -> None:
    result = await _on_list_tools(SimpleNamespace(request=None), None)

    assert "tp_refresh_auth" in {tool.name for tool in result.tools}
