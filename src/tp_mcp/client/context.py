"""Request-scoped context for athlete targeting and cloud credentials.

The HTTP transport authenticates a Google principal before entering
``cloud_request_context``. Keeping the principal and the TrainingPeaks cookie
supplied on that HTTP request in :mod:`contextvars` lets concurrent ASGI tasks
share one process without persisting or sharing authentication state.
"""

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

TRAININGPEAKS_AUTH_HEADER = "X-TrainingPeaks-Auth"
MAX_TRAININGPEAKS_AUTH_BYTES = 16 * 1024
_COOKIE_VALUE_FORBIDDEN_BYTES = {0x22, 0x2C, 0x3B, 0x5C}


def is_safe_trainingpeaks_credential(value: str | bytes) -> bool:
    """Accept one bounded RFC 6265-style cookie value, not a Cookie header."""
    try:
        raw = value.encode("ascii") if isinstance(value, str) else value
    except UnicodeEncodeError:
        return False
    return (
        bool(raw)
        and len(raw) <= MAX_TRAININGPEAKS_AUTH_BYTES
        and all(0x21 <= byte <= 0x7E and byte not in _COOKIE_VALUE_FORBIDDEN_BYTES for byte in raw)
    )


athlete_override: contextvars.ContextVar[str | None] = contextvars.ContextVar("athlete_override", default=None)

cloud_principal: contextvars.ContextVar[str | None] = contextvars.ContextVar("cloud_principal", default=None)
"""Authenticated Google subject for the current remote MCP request."""

cloud_credential: contextvars.ContextVar[str | None] = contextvars.ContextVar("cloud_credential", default=None)
"""TrainingPeaks cookie supplied in the current remote MCP request header."""


@contextmanager
def cloud_request_context(principal: str, credential: str | None) -> Iterator[None]:
    """Bind a cloud principal and its TrainingPeaks credential to this task.

    A missing credential is deliberately representable so the client can fail
    closed in tests and defensive paths. Remote HTTP dispatch requires the
    header before entering this context and never falls back to the host's
    local keyring.

    Args:
        principal: Stable subject from the verified Google/OAuth identity.
        credential: ``Production_tpAuth`` cookie from this HTTP request.

    Raises:
        ValueError: If ``principal`` is empty.
    """
    normalized_principal = principal.strip()
    if not normalized_principal:
        raise ValueError("Cloud principal cannot be empty")

    principal_token = cloud_principal.set(normalized_principal)
    credential_token = cloud_credential.set(credential if credential else None)
    try:
        yield
    finally:
        cloud_credential.reset(credential_token)
        cloud_principal.reset(principal_token)
