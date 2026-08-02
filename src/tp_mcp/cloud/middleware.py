"""Bounded public-request guards for the internet-facing ASGI application."""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict, deque

from starlette.types import ASGIApp, Message, Receive, Scope, Send

PUBLIC_BODY_LIMIT_BYTES = 64 * 1024
MAX_RATE_BUCKETS = 4096
# Per-instance limits complement Cloud Run's instance/concurrency caps. Durable
# records also expire through Firestore TTL policies.
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "/register": (10, 60 * 60),
    "/authorize": (30, 10 * 60),
    "/token": (60, 10 * 60),
    "/revoke": (60, 10 * 60),
}
GLOBAL_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "/register": (50, 60 * 60),
    "/authorize": (120, 10 * 60),
    "/token": (300, 10 * 60),
    "/revoke": (300, 10 * 60),
}


async def _json_error(send: Send, status_code: int, error: str, *, retry_after: int | None = None) -> None:
    body = json.dumps({"error": error}, separators=(",", ":")).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
    ]
    if retry_after is not None:
        headers.append((b"retry-after", str(retry_after).encode()))
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class RequestGuardMiddleware:
    """Cap unauthenticated bodies and rate-limit state-creating endpoints."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._buckets: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def _rate_limited(self, path: str, client_ip: str) -> tuple[bool, int]:
        now = time.monotonic()
        async with self._lock:
            checks = [
                ((path, client_ip), RATE_LIMITS[path]),
                ((path, "*"), GLOBAL_RATE_LIMITS[path]),
            ]
            buckets: list[tuple[tuple[str, str], deque[float]]] = []
            for key, (limit, window) in checks:
                attempts = self._buckets.setdefault(key, deque())
                cutoff = now - window
                while attempts and attempts[0] <= cutoff:
                    attempts.popleft()
                if len(attempts) >= limit:
                    retry_after = max(1, int(window - (now - attempts[0])))
                    self._buckets.move_to_end(key)
                    return True, retry_after
                buckets.append((key, attempts))
            for key, attempts in buckets:
                attempts.append(now)
                self._buckets.move_to_end(key)
            while len(self._buckets) > MAX_RATE_BUCKETS:
                self._buckets.popitem(last=False)
        return False, 0

    @staticmethod
    async def _bounded_body(receive: Receive, limit: int) -> bytes | None:
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return b""
            chunk = message.get("body", b"")
            body.extend(chunk)
            if len(body) > limit:
                return None
            if not message.get("more_body", False):
                return bytes(body)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        client = scope.get("client")
        client_ip = str(client[0]) if client else "unknown"
        if path in RATE_LIMITS:
            limited, retry_after = await self._rate_limited(path, client_ip)
            if limited:
                await _json_error(send, 429, "rate_limited", retry_after=retry_after)
                return

        method = str(scope.get("method", "GET")).upper()
        if path != "/mcp" and method in {"POST", "PUT", "PATCH"}:
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            raw_content_length = headers.get(b"content-length")
            if raw_content_length is not None:
                try:
                    content_length = int(raw_content_length)
                except ValueError:
                    await _json_error(send, 400, "invalid_content_length")
                    return
                if content_length < 0 or content_length > PUBLIC_BODY_LIMIT_BYTES:
                    await _json_error(send, 413, "request_too_large")
                    return

            body = await self._bounded_body(receive, PUBLIC_BODY_LIMIT_BYTES)
            if body is None:
                await _json_error(send, 413, "request_too_large")
                return
            delivered = False

            async def replay_receive() -> Message:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.request", "body": b"", "more_body": False}

            receive = replay_receive

        await self.app(scope, receive, send)
