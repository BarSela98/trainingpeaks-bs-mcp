"""Starlette application for authenticated Streamable HTTP on Cloud Run."""

from __future__ import annotations

import html
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import ProviderTokenVerifier, TokenError
from mcp.server.auth.routes import create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from tp_mcp.auth import AuthResult, AuthStatus, validate_auth
from tp_mcp.client.context import (
    MAX_TRAININGPEAKS_AUTH_BYTES,
    TRAININGPEAKS_AUTH_HEADER,
    cloud_request_context,
    is_safe_trainingpeaks_credential,
)
from tp_mcp.cloud.config import DEFAULT_SCOPES, CloudConfig
from tp_mcp.cloud.crypto import CloudKMSCipher, RecordCipher
from tp_mcp.cloud.middleware import RequestGuardMiddleware
from tp_mcp.cloud.oauth import REFRESH_TOKEN_TTL_SECONDS, FirestoreOAuthProvider, GoogleIdentityProvider
from tp_mcp.cloud.storage import (
    TRAININGPEAKS_IDENTITIES,
    CloudStore,
    FirestoreCloudStore,
)
from tp_mcp.server import server

logger = logging.getLogger("tp-mcp.cloud")
MAX_MCP_REQUEST_BYTES = 6 * 1024 * 1024
TRAININGPEAKS_AUTH_HEADER_BYTES = TRAININGPEAKS_AUTH_HEADER.lower().encode("ascii")
Validator = Callable[[str], Awaitable[AuthResult]]


class PerRequestTrainingPeaksAuthMiddleware:
    """Require a bounded TrainingPeaks credential on authenticated MCP calls.

    Unauthenticated requests continue to the MCP bearer middleware so clients
    receive the standard OAuth challenge. The credential is inspected only for
    presence and size here; it is never logged, persisted, or copied into the
    ASGI scope.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") != "/mcp":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        authorization_values = [value for name, value in headers if name.lower() == b"authorization"]
        if len(authorization_values) > 1:
            response = JSONResponse(
                {"error": "ambiguous_authorization"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        has_bearer = bool(authorization_values and authorization_values[0].lstrip().lower().startswith(b"bearer "))
        if not has_bearer:
            await self.app(scope, receive, send)
            return

        credential_values = [value for name, value in headers if name.lower() == TRAININGPEAKS_AUTH_HEADER_BYTES]
        if len(credential_values) != 1 or not credential_values[0]:
            # The bearer is already present, so this is a malformed dual-auth
            # request rather than another MCP OAuth challenge. A second 401 can
            # send clients through Google OAuth repeatedly without fixing the
            # missing TrainingPeaks header.
            response = JSONResponse(
                {"error": "trainingpeaks_auth_required"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        if len(credential_values[0]) > MAX_TRAININGPEAKS_AUTH_BYTES:
            response = JSONResponse(
                {"error": "trainingpeaks_auth_too_large"},
                status_code=431,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        if not is_safe_trainingpeaks_credential(credential_values[0]):
            response = JSONResponse(
                {"error": "trainingpeaks_auth_invalid_format"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class PerRequestTrainingPeaksIdentityMiddleware:
    """Validate and bind the request cookie after MCP OAuth authentication.

    The first valid request atomically binds a Google subject to non-secret
    TrainingPeaks identity fields. Later requests must present a cookie for the
    same athlete. Only the identity is durable; the cookie remains in a
    ContextVar for the lifetime of this ASGI request.
    """

    def __init__(self, app: Any, provider: FirestoreOAuthProvider, validator: Validator) -> None:
        self.app = app
        self.provider = provider
        self.validator = validator

    @staticmethod
    async def _error(scope: dict[str, Any], receive: Any, send: Any, status_code: int, error: str) -> None:
        response = JSONResponse({"error": error}, status_code=status_code, headers={"Cache-Control": "no-store"})
        await response(scope, receive, send)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        user = scope.get("user")
        if scope.get("type") != "http" or not isinstance(user, AuthenticatedUser):
            await self.app(scope, receive, send)
            return
        subject = user.access_token.subject
        if not subject:
            await self._error(scope, receive, send, 403, "oauth_subject_required")
            return

        credential_values = [
            value for name, value in scope.get("headers", []) if name.lower() == TRAININGPEAKS_AUTH_HEADER_BYTES
        ]
        if len(credential_values) != 1 or not is_safe_trainingpeaks_credential(credential_values[0]):
            # The outer presence/syntax guard normally handles this; keep the
            # identity boundary fail-closed if middleware order changes.
            await self._error(scope, receive, send, 400, "trainingpeaks_auth_required")
            return
        credential = credential_values[0].decode("ascii")
        try:
            result = await self.validator(credential)
        except Exception:
            # Do not log exception text: a third-party failure could echo the
            # caller-supplied cookie into its message.
            logger.warning("TrainingPeaks request credential validation failed unexpectedly")
            await self._error(scope, receive, send, 503, "trainingpeaks_validation_unavailable")
            return
        if result.status is AuthStatus.NETWORK_ERROR:
            await self._error(scope, receive, send, 503, "trainingpeaks_validation_unavailable")
            return
        if not result.is_valid:
            await self._error(scope, receive, send, 403, "trainingpeaks_auth_invalid")
            return
        if result.athlete_id is None or not result.email:
            await self._error(scope, receive, send, 503, "trainingpeaks_identity_unavailable")
            return

        identity: dict[str, Any] = {
            "athlete_id": str(result.athlete_id),
            "email": result.email.strip().casefold(),
            "user_id": str(result.user_id) if result.user_id is not None else None,
            "bound_at": time.time(),
        }
        binding: dict[str, Any] | None
        if await self.provider.store.create(TRAININGPEAKS_IDENTITIES, subject, identity):
            binding = identity
        else:
            binding = await self.provider.store.get(TRAININGPEAKS_IDENTITIES, subject)
        if binding is None or str(binding.get("athlete_id", "")) != identity["athlete_id"]:
            await self._error(scope, receive, send, 403, "trainingpeaks_identity_mismatch")
            return

        with cloud_request_context(subject, credential):
            await self.app(scope, receive, send)


def _security_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
    return response


def _page(title: str, body: str, *, status_code: int = 200) -> HTMLResponse:
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font: 16px system-ui, sans-serif; max-width: 44rem; margin: 4rem auto;
            padding: 0 1.25rem; color: #18212f; }}
    main {{ border: 1px solid #d9dee8; border-radius: 12px; padding: 1.5rem; box-shadow: 0 4px 20px #1d2a3a12; }}
    code, input {{ font: 0.95rem ui-monospace, monospace; }}
    code {{ overflow-wrap: anywhere; }}
    input {{ box-sizing: border-box; width: 100%; padding: .8rem; margin: .5rem 0 1rem; }}
    button {{ font: inherit; padding: .65rem 1rem; cursor: pointer; }}
    .muted {{ color: #596579; }}
  </style>
</head>
<body><main><h1>{html.escape(title)}</h1>{body}</main></body>
</html>"""
    return _security_headers(HTMLResponse(document, status_code=status_code))  # type: ignore[return-value]


class CloudWebHandlers:
    def __init__(self, config: CloudConfig, provider: FirestoreOAuthProvider) -> None:
        self.config = config
        self.provider = provider

    async def health(self, request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "transport": "streamable-http"})

    async def home(self, request: Request) -> HTMLResponse:
        return _page(
            "TrainingPeaks MCP",
            "<p>This service exposes an invite-only MCP endpoint at <code>/mcp</code>.</p>",
        )

    @staticmethod
    def _consent_body(*, consent_value: str, email: str, client_id: str, scopes: list[str]) -> str:
        descriptions = {
            "trainingpeaks:read": "Read your TrainingPeaks profile, workouts, and training data.",
            "trainingpeaks:write": "Create, update, and delete data in your TrainingPeaks account.",
        }
        permissions = "".join(
            "<li><code>"
            f"{html.escape(scope)}</code> — {html.escape(descriptions.get(scope, 'Access this MCP permission.'))}</li>"
            for scope in scopes
        )
        return (
            f"<p>Signed in as <strong>{html.escape(email)}</strong>.</p>"
            f"<p>The MCP client <code>{html.escape(client_id)}</code> is requesting:</p>"
            f"<ul>{permissions}</ul>"
            '<p class="muted">Approve only if you initiated this connection and trust the MCP client.</p>'
            '<form method="post" action="/oauth/confirm">'
            '<input type="hidden" name="consent" value="'
            f'{html.escape(consent_value, quote=True)}">'
            '<button type="submit">Approve and connect</button>'
            "</form>"
        )

    async def google_callback(self, request: Request) -> Response:
        parameters = await request.form()
        state = str(parameters.get("state", ""))
        code = str(parameters.get("code", ""))
        if parameters.get("error") or not state or not code:
            return _page("Authorization cancelled", "<p>Google sign-in was not completed.</p>", status_code=400)

        consumed = await self.provider.consume_google_transaction(state)
        if consumed is None:
            return _page(
                "Authorization expired",
                "<p>This authorization link expired or was already used. "
                "Start the connection again in your MCP client.</p>",
                status_code=400,
            )
        transaction_id, transaction = consumed
        try:
            identity = await self.provider.google.authenticate(
                code,
                redirect_uri=self.config.google_redirect_uri,
                nonce_digest=str(transaction["google_nonce_digest"]),
            )
        except (httpx.HTTPError, ValueError, RuntimeError):
            logger.warning("Google OIDC callback validation failed")
            return _page(
                "Sign-in failed",
                "<p>Google identity validation failed. Please try again.</p>",
                status_code=401,
            )

        if not await self.provider.bind_invited_identity(identity):
            return _page(
                "Invitation required",
                "<p>This Google account is not currently invited to use the TrainingPeaks MCP.</p>",
                status_code=403,
            )
        try:
            consent_value = await self.provider.create_authorization_consent(transaction_id, identity.subject)
        except TokenError:
            return _page("Authorization failed", "<p>Could not complete authorization.</p>", status_code=400)
        scopes = [str(scope) for scope in transaction.get("scopes", [])]
        return _page(
            "Approve TrainingPeaks access",
            self._consent_body(
                consent_value=consent_value,
                email=identity.email,
                client_id=str(transaction.get("client_id", "unknown client")),
                scopes=scopes,
            ),
        )

    async def confirm_authorization(self, request: Request) -> Response:
        parameters = await request.form()
        consent_value = str(parameters.get("consent", ""))
        if not consent_value:
            return _page("Authorization failed", "<p>Consent confirmation is missing.</p>", status_code=400)
        try:
            redirect_url = await self.provider.complete_authorization(consent_value)
        except TokenError:
            return _page(
                "Authorization expired",
                "<p>This confirmation expired or was already used. Start the connection again in your MCP client.</p>",
                status_code=400,
            )
        return RedirectResponse(redirect_url, status_code=302, headers={"Cache-Control": "no-store"})


def _bootstrap_app() -> Starlette:
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "mode": "bootstrap"})

    async def unavailable(request: Request) -> JSONResponse:
        return JSONResponse({"error": "bootstrap_revision"}, status_code=503)

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/mcp", unavailable),
            Route("/", unavailable),
        ]
    )


def create_http_app(
    config: CloudConfig | None = None,
    *,
    store: CloudStore | None = None,
    cipher: RecordCipher | None = None,
    google: GoogleIdentityProvider | None = None,
    validator: Validator = validate_auth,
) -> Starlette:
    """Create the production app or a dependency-injected test app."""
    settings = config or CloudConfig.from_env()
    if settings.bootstrap:
        return _bootstrap_app()

    cloud_store = store or FirestoreCloudStore(settings.project_id, settings.firestore_database)
    record_cipher = cipher or CloudKMSCipher(settings.kms_key_name)
    provider = FirestoreOAuthProvider(settings, cloud_store, record_cipher, google=google)
    handlers = CloudWebHandlers(settings, provider)

    auth_settings = AuthSettings(
        issuer_url=settings.issuer_url,
        resource_server_url=settings.resource_url,
        required_scopes=["trainingpeaks:read"],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            client_secret_expiry_seconds=REFRESH_TOKEN_TTL_SECONDS,
            valid_scopes=list(DEFAULT_SCOPES),
            default_scopes=["trainingpeaks:read"],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_MCP_REQUEST_BYTES,
        host="0.0.0.0",
        auth=auth_settings,
        token_verifier=ProviderTokenVerifier(provider),
        auth_server_provider=provider,
        custom_starlette_routes=[
            Route("/", handlers.home, methods=["GET"]),
            Route("/health", handlers.health, methods=["GET"]),
            Route("/oauth/google/callback", handlers.google_callback, methods=["POST"]),
            Route("/oauth/confirm", handlers.confirm_authorization, methods=["POST"]),
        ],
        debug=settings.debug,
    )
    for route in app.router.routes:
        if getattr(route, "path", None) == "/mcp":
            # App-level bearer authentication has populated scope["user"] by
            # the time this route wrapper runs. Validate the independent
            # TrainingPeaks credential before MCP dispatch.
            route_app = getattr(route, "app", None)
            if route_app is None:  # pragma: no cover - SDK route contract
                raise RuntimeError("The MCP route is missing its ASGI application")
            route_with_app: Any = route
            route_with_app.app = PerRequestTrainingPeaksIdentityMiddleware(route_app, provider, validator)
            break
    # The SDK derives protected-resource metadata from required_scopes, while
    # this service requires read globally and authorizes writes per tool. Make
    # the metadata advertise both scopes without making write globally required.
    protected_route = create_protected_resource_routes(
        resource_url=auth_settings.resource_server_url,
        authorization_servers=[auth_settings.issuer_url],
        scopes_supported=list(DEFAULT_SCOPES),
        resource_name="TrainingPeaks MCP",
    )[0]
    protected_path = "/.well-known/oauth-protected-resource/mcp"
    app.router.routes = [
        protected_route if getattr(route, "path", None) == protected_path else route for route in app.router.routes
    ]
    app.state.cloud_provider = provider
    app.add_middleware(RequestGuardMiddleware)
    app.add_middleware(PerRequestTrainingPeaksAuthMiddleware)
    return app


def run_http_server() -> int:
    """Run the Cloud Run-compatible ASGI server."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - base-only installation
        raise RuntimeError("HTTP support is not installed; install tp-mcp[cloud]") from exc

    config = CloudConfig.from_env()
    app = create_http_app(config)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=config.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        # OAuth callbacks carry one-use secrets. Cloud Run emits its own request
        # logs; do not duplicate URLs in Uvicorn logs.
        access_log=False,
    )
    return 0
