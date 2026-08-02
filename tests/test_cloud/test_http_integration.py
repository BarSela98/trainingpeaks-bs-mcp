"""End-to-end authenticated Streamable HTTP transport coverage."""

from __future__ import annotations

from mcp.types import LATEST_PROTOCOL_VERSION
from starlette.testclient import TestClient

from tp_mcp.auth import AuthResult, AuthStatus
from tp_mcp.cloud.storage import TRAININGPEAKS_IDENTITIES
from tp_mcp.cloud.web import create_http_app

REQUEST_COOKIE = "request-only-trainingpeaks-cookie"


def _rpc(client: TestClient, token: str, method: str, params: dict | None = None, *, request_id: int = 1):
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "X-TrainingPeaks-Auth": REQUEST_COOKIE,
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            **({"params": params} if params is not None else {}),
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == request_id
    assert "error" not in payload
    return payload["result"]


async def test_authenticated_http_initialize_catalogs_and_offline_tool(
    cloud_config,
    store,
    cipher,
    issue_grant,
) -> None:
    _, _, tokens = await issue_grant(scopes=["trainingpeaks:read"])
    validated_cookies: list[str] = []

    async def validate_request_cookie(cookie: str) -> AuthResult:
        validated_cookies.append(cookie)
        return AuthResult(
            status=AuthStatus.VALID,
            athlete_id=12345,
            user_id=98765,
            email="trainingpeaks@example.com",
            message="valid",
        )

    app = create_http_app(
        cloud_config,
        store=store,
        cipher=cipher,
        validator=validate_request_cookie,
    )

    with TestClient(app, base_url=cloud_config.base_url) as client:
        initialized = _rpc(
            client,
            tokens.access_token,
            "initialize",
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "cloud-integration-test", "version": "1"},
            },
            request_id=1,
        )
        tools = _rpc(client, tokens.access_token, "tools/list", {}, request_id=2)
        resources = _rpc(client, tokens.access_token, "resources/list", {}, request_id=3)
        fitness_tool = next(tool for tool in tools["tools"] if tool["name"] == "tp_get_fitness")
        app_resource_uri = fitness_tool["_meta"]["ui"]["resourceUri"]
        app_resource = _rpc(
            client,
            tokens.access_token,
            "resources/read",
            {"uri": app_resource_uri},
            request_id=4,
        )
        called = _rpc(
            client,
            tokens.access_token,
            "tools/call",
            {"name": "tp_search_exercises", "arguments": {"query": "squat", "limit": 1}},
            request_id=5,
        )

    assert initialized["protocolVersion"]
    assert initialized["serverInfo"]["name"] == "trainingpeaks-mcp"
    tool_names = {tool["name"] for tool in tools["tools"]}
    assert "tp_search_exercises" in tool_names
    assert "tp_refresh_auth" not in tool_names
    assert "tp_create_workout" not in tool_names
    assert resources["resources"]
    assert app_resource_uri in {resource["uri"] for resource in resources["resources"]}
    assert app_resource["contents"][0]["mimeType"] == "text/html;profile=mcp-app"
    assert "<!doctype html>" in app_resource["contents"][0]["text"].lower()
    assert called["isError"] is False
    assert called["content"]
    assert validated_cookies == [REQUEST_COOKIE] * 5
    binding = await store.get(TRAININGPEAKS_IDENTITIES, "google-subject-1")
    assert binding is not None
    assert binding["athlete_id"] == "12345"
    assert binding["email"] == "trainingpeaks@example.com"
    assert binding["user_id"] == "98765"
    assert REQUEST_COOKIE not in repr(store._documents)


async def test_authenticated_http_rejects_cookie_for_another_trainingpeaks_identity(
    cloud_config,
    store,
    cipher,
    issue_grant,
) -> None:
    _, _, tokens = await issue_grant(scopes=["trainingpeaks:read"])
    await store.put(
        TRAININGPEAKS_IDENTITIES,
        "google-subject-1",
        {"athlete_id": "111", "email": "bound@example.com"},
    )

    async def validate_other_identity(cookie: str) -> AuthResult:
        assert cookie == REQUEST_COOKIE
        return AuthResult(
            status=AuthStatus.VALID,
            athlete_id=222,
            user_id=333,
            email="other@example.com",
            message="valid",
        )

    app = create_http_app(
        cloud_config,
        store=store,
        cipher=cipher,
        validator=validate_other_identity,
    )
    with TestClient(app, base_url=cloud_config.base_url) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {tokens.access_token}",
                "X-TrainingPeaks-Auth": REQUEST_COOKIE,
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "identity-mismatch-test", "version": "1"},
                },
            },
        )

    assert response.status_code == 403
    assert response.json() == {"error": "trainingpeaks_identity_mismatch"}
    assert REQUEST_COOKIE not in repr(store._documents)


async def test_authenticated_http_sanitizes_unexpected_validation_failure(
    cloud_config,
    store,
    cipher,
    issue_grant,
    caplog,
) -> None:
    _, _, tokens = await issue_grant(scopes=["trainingpeaks:read"])

    async def broken_validator(cookie: str) -> AuthResult:
        assert cookie == REQUEST_COOKIE
        raise RuntimeError(f"upstream echoed secret: {cookie}")

    app = create_http_app(
        cloud_config,
        store=store,
        cipher=cipher,
        validator=broken_validator,
    )
    with TestClient(app, base_url=cloud_config.base_url) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {tokens.access_token}",
                "X-TrainingPeaks-Auth": REQUEST_COOKIE,
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "validator-failure-test", "version": "1"},
                },
            },
        )

    assert response.status_code == 503
    assert response.json() == {"error": "trainingpeaks_validation_unavailable"}
    assert REQUEST_COOKIE not in response.text
    assert REQUEST_COOKIE not in repr(store._documents)
    assert REQUEST_COOKIE not in caplog.text
