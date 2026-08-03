"""Validated configuration for the remote TrainingPeaks MCP service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

DEFAULT_SCOPES = ("trainingpeaks:read", "trainingpeaks:write")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _canonical_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower()
    if not parsed.scheme or not parsed.netloc or not host:
        raise ValueError("TP_MCP_BASE_URL must be an absolute URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("TP_MCP_BASE_URL must not contain credentials, a query, or a fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("TP_MCP_BASE_URL must be an origin without a path")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and host in LOOPBACK_HOSTS):
        raise ValueError("TP_MCP_BASE_URL must use HTTPS outside loopback development")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _required(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


@dataclass(frozen=True)
class CloudConfig:
    """Cloud-only settings loaded from environment variables.

    The Google OAuth secret is injected into an environment variable by Cloud
    Run's Secret Manager integration. It is never accepted as a CLI flag, which
    keeps it out of process listings and deployment logs.
    """

    base_url: str
    project_id: str
    kms_key_name: str
    google_client_id: str
    google_client_secret: str
    firestore_database: str = "(default)"
    port: int = 8080
    debug: bool = False
    bootstrap: bool = False

    @property
    def issuer_url(self) -> str:
        return self.base_url

    @property
    def resource_url(self) -> str:
        return f"{self.base_url}/mcp"

    @property
    def google_redirect_uri(self) -> str:
        return f"{self.base_url}/oauth/google/callback"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> CloudConfig:
        env = dict(os.environ if environ is None else environ)
        bootstrap = env.get("TP_MCP_BOOTSTRAP", "0") == "1"

        # Bootstrap revisions only exist to obtain the permanent run.app URL.
        # They expose the health endpoint but deliberately do not expose an unauthenticated MCP.
        if bootstrap:
            base_url = _canonical_base_url(env.get("TP_MCP_BASE_URL", "http://localhost"))
            return cls(
                base_url=base_url,
                project_id=env.get("GOOGLE_CLOUD_PROJECT", "bootstrap"),
                kms_key_name=env.get("TP_MCP_KMS_KEY", "bootstrap"),
                google_client_id=env.get("GOOGLE_OAUTH_CLIENT_ID", "bootstrap"),
                google_client_secret=env.get("GOOGLE_OAUTH_CLIENT_SECRET", "bootstrap"),
                firestore_database=env.get("TP_MCP_FIRESTORE_DATABASE", "(default)"),
                port=int(env.get("PORT", "8080")),
                bootstrap=True,
            )

        base_url = _canonical_base_url(_required(env, "TP_MCP_BASE_URL"))
        port = int(env.get("PORT", "8080"))
        if not 1 <= port <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
        return cls(
            base_url=base_url,
            project_id=_required(env, "GOOGLE_CLOUD_PROJECT"),
            firestore_database=env.get("TP_MCP_FIRESTORE_DATABASE", "(default)"),
            kms_key_name=_required(env, "TP_MCP_KMS_KEY"),
            google_client_id=_required(env, "GOOGLE_OAUTH_CLIENT_ID"),
            google_client_secret=_required(env, "GOOGLE_OAUTH_CLIENT_SECRET"),
            port=port,
            debug=env.get("TP_MCP_DEBUG", "0") == "1",
        )
