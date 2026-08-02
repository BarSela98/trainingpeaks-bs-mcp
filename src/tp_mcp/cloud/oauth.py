"""Durable OAuth 2.1 provider and Google OIDC identity bridge."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode, urlsplit

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from tp_mcp.cloud.config import DEFAULT_SCOPES, LOOPBACK_HOSTS, CloudConfig
from tp_mcp.cloud.crypto import RecordCipher, encryption_context
from tp_mcp.cloud.storage import (
    ALLOWLIST,
    ALLOWLIST_SUBJECTS,
    OAUTH_ACCESS_TOKENS,
    OAUTH_CLIENTS,
    OAUTH_CODES,
    OAUTH_CONSENTS,
    OAUTH_GRANTS,
    OAUTH_REFRESH_TOKENS,
    OAUTH_TRANSACTIONS,
    USERS,
    CloudStore,
    Document,
    ttl_timestamp,
)

AUTHORIZATION_TTL_SECONDS = 10 * 60
CONSENT_TTL_SECONDS = 10 * 60
AUTHORIZATION_CODE_TTL_SECONDS = 5 * 60
ACCESS_TOKEN_TTL_SECONDS = 60 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
DYNAMIC_CLIENT_TTL_SECONDS = 7 * 24 * 60 * 60
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def opaque_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def document_key(kind: str, value: str) -> str:
    """Hash a high-entropy OAuth value before using it in Firestore."""
    return hashlib.sha256(f"{kind}\0{value}".encode()).hexdigest()


def email_key(email: str) -> str:
    return hashlib.sha256(email.strip().casefold().encode("utf-8")).hexdigest()


class StoredAuthorizationCode(AuthorizationCode):
    grant_id: str
    email: str


class StoredRefreshToken(RefreshToken):
    grant_id: str
    resource: str
    email: str
    access_token_id: str | None = None


class StoredAccessToken(AccessToken):
    grant_id: str
    email: str


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str
    name: str | None = None
    issuer: str = "https://accounts.google.com"


class GoogleIdentityProvider(Protocol):
    async def authenticate(self, code: str, *, redirect_uri: str, nonce_digest: str) -> GoogleIdentity: ...


class GoogleOIDCClient:
    """Exchange and verify a Google authorization code using the official verifier."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret

    async def authenticate(self, code: str, *, redirect_uri: str, nonce_digest: str) -> GoogleIdentity:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                GOOGLE_TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        response.raise_for_status()
        payload = response.json()
        encoded_id_token = payload.get("id_token")
        if not isinstance(encoded_id_token, str):
            raise ValueError("Google did not return an ID token")

        try:
            from google.auth.transport.requests import Request as GoogleRequest
            from google.oauth2 import id_token
        except ImportError as exc:  # pragma: no cover - base-only installation
            raise RuntimeError("Cloud support is not installed; install tp-mcp[cloud]") from exc

        claims = await asyncio.to_thread(
            id_token.verify_oauth2_token,
            encoded_id_token,
            GoogleRequest(),
            self._client_id,
        )
        claimed_nonce = str(claims.get("nonce", ""))
        if not hmac.compare_digest(document_key("google-nonce", claimed_nonce), nonce_digest):
            raise ValueError("Google nonce did not match")
        if claims.get("email_verified") is not True:
            raise ValueError("Google email is not verified")
        subject = claims.get("sub")
        email = claims.get("email")
        issuer = claims.get("iss")
        if not isinstance(subject, str) or not subject or not isinstance(email, str) or not email:
            raise ValueError("Google identity is missing required claims")
        if issuer not in {"accounts.google.com", "https://accounts.google.com"}:
            raise ValueError("Google issuer is invalid")
        name = claims.get("name")
        return GoogleIdentity(
            subject=subject,
            email=email.strip().casefold(),
            name=name if isinstance(name, str) else None,
            issuer=str(issuer),
        )


class FirestoreOAuthProvider(
    OAuthAuthorizationServerProvider[StoredAuthorizationCode, StoredRefreshToken, StoredAccessToken]
):
    """MCP OAuth provider whose durable records contain no plaintext tokens."""

    def __init__(
        self,
        config: CloudConfig,
        store: CloudStore,
        cipher: RecordCipher,
        google: GoogleIdentityProvider | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.cipher = cipher
        self.google = google or GoogleOIDCClient(config.google_client_id, config.google_client_secret)

    @staticmethod
    def _valid_redirect_uri(uri: str) -> bool:
        parsed = urlsplit(uri)
        host = (parsed.hostname or "").lower()
        if uri == CLAUDE_CALLBACK:
            return True
        return (
            parsed.scheme == "http"
            and host in LOOPBACK_HOSTS
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        document = await self.store.get(OAUTH_CLIENTS, client_id)
        if document is None:
            return None
        now = time.time()
        if float(document.get("expires_at", 0)) <= now:
            await self.store.delete(OAUTH_CLIENTS, client_id)
            return None
        renewed_expiry = now + DYNAMIC_CLIENT_TTL_SECONDS
        await self.store.put(
            OAUTH_CLIENTS,
            client_id,
            {"expires_at": renewed_expiry, "expires_at_timestamp": ttl_timestamp(renewed_expiry)},
            merge=True,
        )
        metadata = dict(document.get("metadata") or {})
        encrypted_secret = document.get("client_secret_ciphertext")
        if isinstance(encrypted_secret, str):
            metadata["client_secret"] = await self.cipher.decrypt(
                encrypted_secret,
                context=encryption_context("oauth-client", client_id),
            )
        return OAuthClientInformationFull.model_validate(metadata)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        redirect_uris = [str(uri) for uri in client_info.redirect_uris or []]
        if not redirect_uris or any(not self._valid_redirect_uri(uri) for uri in redirect_uris):
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="Redirect URIs must be Claude's MCP callback or an HTTP loopback URI",
            )
        metadata = client_info.model_dump(mode="json")
        client_secret = metadata.pop("client_secret", None)
        now = time.time()
        expires_at = now + DYNAMIC_CLIENT_TTL_SECONDS
        document: Document = {
            "metadata": metadata,
            "created_at": now,
            "expires_at": expires_at,
            "expires_at_timestamp": ttl_timestamp(expires_at),
        }
        if isinstance(client_secret, str) and client_secret:
            document["client_secret_ciphertext"] = await self.cipher.encrypt(
                client_secret,
                context=encryption_context("oauth-client", client_info.client_id),
            )
        if not await self.store.create(OAUTH_CLIENTS, client_info.client_id, document):
            raise RegistrationError(error="invalid_client_metadata", error_description="Client ID already exists")

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.config.resource_url:
            raise AuthorizeError(error="invalid_target", error_description="Unknown MCP resource")
        scopes = params.scopes or ["trainingpeaks:read"]
        if "trainingpeaks:write" in scopes and "trainingpeaks:read" not in scopes:
            scopes = ["trainingpeaks:read", *scopes]
        if not set(scopes).issubset(DEFAULT_SCOPES):
            raise AuthorizeError(error="invalid_scope", error_description="Unsupported scope")

        state = opaque_token("tps")
        nonce = opaque_token("tpn")
        transaction_id = document_key("state", state)
        expires_at = time.time() + AUTHORIZATION_TTL_SECONDS
        transaction: Document = {
            "client_id": client.client_id,
            "scopes": scopes,
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": self.config.resource_url,
            "google_nonce_digest": document_key("google-nonce", nonce),
            "created_at": time.time(),
            "expires_at": expires_at,
            "expires_at_timestamp": ttl_timestamp(expires_at),
        }
        if params.state is not None:
            transaction["client_state_ciphertext"] = await self.cipher.encrypt(
                params.state,
                context=encryption_context("oauth-transaction", transaction_id),
            )
        created = await self.store.create(OAUTH_TRANSACTIONS, transaction_id, transaction)
        if not created:  # pragma: no cover - cryptographically implausible
            raise AuthorizeError(error="server_error", error_description="Could not create authorization session")

        query = urlencode(
            {
                "client_id": self.config.google_client_id,
                "redirect_uri": self.config.google_redirect_uri,
                "response_type": "code",
                "response_mode": "form_post",
                "scope": "openid email profile",
                "state": state,
                "nonce": nonce,
                "prompt": "select_account",
            }
        )
        return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{query}"

    async def consume_google_transaction(self, state: str) -> tuple[str, Document] | None:
        transaction_id = document_key("state", state)
        document = await self.store.consume(OAUTH_TRANSACTIONS, transaction_id)
        if document is None:
            return None
        return transaction_id, document

    async def bind_invited_identity(self, identity: GoogleIdentity) -> bool:
        allowlist_id = email_key(identity.email)
        invite = await self.store.get(ALLOWLIST, allowlist_id)
        if invite is None or invite.get("enabled") is not True:
            return False
        existing_subject = invite.get("subject")
        if existing_subject and not hmac.compare_digest(str(existing_subject), identity.subject):
            return False

        await self.store.put(
            ALLOWLIST,
            allowlist_id,
            {"subject": identity.subject, "last_login_at": time.time()},
            merge=True,
        )
        await self.store.put(
            ALLOWLIST_SUBJECTS,
            identity.subject,
            {"enabled": True, "allowlist_id": allowlist_id, "email": identity.email, "updated_at": time.time()},
        )
        await self.store.put(
            USERS,
            identity.subject,
            {
                "email": identity.email,
                "google_name": identity.name,
                "google_issuer": identity.issuer,
                "updated_at": time.time(),
            },
            merge=True,
        )
        return True

    async def subject_is_allowed(self, subject: str) -> bool:
        binding = await self.store.get(ALLOWLIST_SUBJECTS, subject)
        if binding is None or binding.get("enabled") is not True:
            return False
        allowlist_id = binding.get("allowlist_id")
        if not isinstance(allowlist_id, str):
            return False
        invite = await self.store.get(ALLOWLIST, allowlist_id)
        return bool(invite and invite.get("enabled") is True and invite.get("subject") == subject)

    async def create_authorization_consent(self, transaction_id: str, subject: str) -> str:
        """Create a short-lived capability for the app's scope confirmation page."""
        transaction = await self.store.get(OAUTH_TRANSACTIONS, transaction_id)
        user = await self.store.get(USERS, subject)
        now = time.time()
        if (
            transaction is None
            or user is None
            or not transaction.get("consumed_at")
            or float(transaction.get("expires_at", 0)) <= now
            or not await self.subject_is_allowed(subject)
        ):
            raise TokenError(error="invalid_grant", error_description="Authorization session is invalid")

        consent_value = opaque_token("tpcf")
        expires_at = min(float(transaction["expires_at"]), now + CONSENT_TTL_SECONDS)
        created = await self.store.create(
            OAUTH_CONSENTS,
            document_key("authorization-consent", consent_value),
            {
                "transaction_id": transaction_id,
                "subject": subject,
                "created_at": now,
                "expires_at": expires_at,
                "expires_at_timestamp": ttl_timestamp(expires_at),
            },
        )
        if not created:  # pragma: no cover - cryptographically implausible
            raise TokenError(error="server_error", error_description="Could not create authorization consent")
        return consent_value

    async def complete_authorization(self, consent_value: str) -> str:
        """Atomically consume app consent and issue an MCP authorization code."""
        consent = await self.store.consume(
            OAUTH_CONSENTS,
            document_key("authorization-consent", consent_value),
        )
        if consent is None:
            raise TokenError(error="invalid_grant", error_description="Authorization consent is invalid")
        transaction_id = consent.get("transaction_id")
        subject = consent.get("subject")
        if not isinstance(transaction_id, str) or not isinstance(subject, str):
            raise TokenError(error="invalid_grant", error_description="Authorization consent is invalid")

        transaction = await self.store.get(OAUTH_TRANSACTIONS, transaction_id)
        user = await self.store.get(USERS, subject)
        if (
            transaction is None
            or user is None
            or not transaction.get("consumed_at")
            or float(transaction.get("expires_at", 0)) <= time.time()
            or not await self.subject_is_allowed(subject)
        ):
            raise TokenError(error="invalid_grant", error_description="Authorization session is invalid")
        email = user.get("email")
        if not isinstance(email, str):
            raise TokenError(error="invalid_grant", error_description="Google identity is incomplete")

        code_value = opaque_token("tpc")
        grant_id = secrets.token_hex(20)
        grant_expires_at = time.time() + REFRESH_TOKEN_TTL_SECONDS
        if not await self.store.create(
            OAUTH_GRANTS,
            grant_id,
            {
                "subject": subject,
                "client_id": str(transaction["client_id"]),
                "created_at": time.time(),
                "expires_at": grant_expires_at,
                "expires_at_timestamp": ttl_timestamp(grant_expires_at),
            },
        ):
            raise TokenError(error="invalid_grant", error_description="Could not create OAuth grant")
        code = StoredAuthorizationCode(
            code=code_value,
            scopes=list(transaction["scopes"]),
            expires_at=time.time() + AUTHORIZATION_CODE_TTL_SECONDS,
            client_id=str(transaction["client_id"]),
            code_challenge=str(transaction["code_challenge"]),
            redirect_uri=str(transaction["redirect_uri"]),
            redirect_uri_provided_explicitly=bool(transaction["redirect_uri_provided_explicitly"]),
            resource=self.config.resource_url,
            subject=subject,
            grant_id=grant_id,
            email=email,
        )
        document = code.model_dump(mode="json", exclude={"code"})
        document["created_at"] = time.time()
        document["expires_at_timestamp"] = ttl_timestamp(code.expires_at)
        if not await self.store.create(OAUTH_CODES, document_key("authorization-code", code_value), document):
            await self.store.delete(OAUTH_GRANTS, grant_id)
            raise TokenError(error="invalid_grant", error_description="Could not issue authorization code")
        client_state = None
        client_state_ciphertext = transaction.get("client_state_ciphertext")
        if isinstance(client_state_ciphertext, str):
            client_state = await self.cipher.decrypt(
                client_state_ciphertext,
                context=encryption_context("oauth-transaction", transaction_id),
            )
        return construct_redirect_uri(
            str(transaction["redirect_uri"]),
            code=code_value,
            state=client_state,
            iss=self.config.issuer_url,
        )

    @staticmethod
    def _authorization_code(value: str, document: Document) -> StoredAuthorizationCode:
        return StoredAuthorizationCode(code=value, **document)

    @staticmethod
    def _refresh_token(value: str, document: Document) -> StoredRefreshToken:
        return StoredRefreshToken(token=value, **document)

    @staticmethod
    def _access_token(value: str, document: Document) -> StoredAccessToken:
        return StoredAccessToken(token=value, **document)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> StoredAuthorizationCode | None:
        document = await self.store.get(OAUTH_CODES, document_key("authorization-code", authorization_code))
        if document is None or document.get("client_id") != client.client_id:
            return None
        if document.get("consumed_at"):
            # Return the record so the SDK can verify PKCE before the exchange
            # path treats this as replay and revokes the grant family.
            return self._authorization_code(authorization_code, document)
        if float(document.get("expires_at", 0)) <= time.time():
            return None
        subject = document.get("subject")
        grant_id = document.get("grant_id")
        if (
            not isinstance(subject, str)
            or not isinstance(grant_id, str)
            or not await self.subject_is_allowed(subject)
            or not await self._grant_is_active(grant_id)
        ):
            return None
        return self._authorization_code(authorization_code, document)

    async def _issue_token_pair(
        self,
        *,
        client_id: str,
        subject: str,
        email: str,
        scopes: list[str],
        grant_id: str,
    ) -> OAuthToken:
        if not await self._grant_is_active(grant_id):
            raise TokenError(error="invalid_grant", error_description="OAuth grant has been revoked")
        access_value = opaque_token("tpa")
        refresh_value = opaque_token("tpr")
        now = int(time.time())
        access_id = document_key("access-token", access_value)
        access = StoredAccessToken(
            token=access_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + ACCESS_TOKEN_TTL_SECONDS,
            resource=self.config.resource_url,
            subject=subject,
            claims={"iss": self.config.issuer_url, "email": email},
            grant_id=grant_id,
            email=email,
        )
        refresh = StoredRefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + REFRESH_TOKEN_TTL_SECONDS,
            subject=subject,
            grant_id=grant_id,
            resource=self.config.resource_url,
            email=email,
            access_token_id=access_id,
        )
        if not await self.store.create(
            OAUTH_ACCESS_TOKENS,
            access_id,
            {
                **access.model_dump(mode="json", exclude={"token"}),
                "created_at": now,
                # Keep the access-token-to-grant mapping for as long as its
                # refresh family can live. Bearer authentication still uses
                # the numeric one-hour expires_at field above.
                "expires_at_timestamp": ttl_timestamp(now + REFRESH_TOKEN_TTL_SECONDS),
            },
        ):
            raise TokenError(error="invalid_grant", error_description="Could not issue access token")
        if not await self.store.create(
            OAUTH_REFRESH_TOKENS,
            document_key("refresh-token", refresh_value),
            {
                **refresh.model_dump(mode="json", exclude={"token"}),
                "created_at": now,
                "expires_at_timestamp": ttl_timestamp(refresh.expires_at or now),
            },
        ):
            await self.store.delete(OAUTH_ACCESS_TOKENS, access_id)
            raise TokenError(error="invalid_grant", error_description="Could not issue refresh token")
        grant_expiry = now + REFRESH_TOKEN_TTL_SECONDS
        await self.store.put(
            OAUTH_GRANTS,
            grant_id,
            {"expires_at": grant_expiry, "expires_at_timestamp": ttl_timestamp(grant_expiry)},
            merge=True,
        )
        return OAuthToken(
            access_token=access_value,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh_value,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: StoredAuthorizationCode
    ) -> OAuthToken:
        document = await self.store.consume(
            OAUTH_CODES,
            document_key("authorization-code", authorization_code.code),
        )
        if document is None:
            existing = await self.store.get(OAUTH_CODES, document_key("authorization-code", authorization_code.code))
            grant_id = existing.get("grant_id") if existing else None
            if isinstance(grant_id, str):
                await self.revoke_grant(grant_id)
            raise TokenError(error="invalid_grant", error_description="Authorization code was already used")
        if document.get("client_id") != client.client_id:
            raise TokenError(error="invalid_grant", error_description="Authorization code was already used")
        subject = str(document.get("subject", ""))
        if not subject or not await self.subject_is_allowed(subject):
            raise TokenError(error="invalid_grant", error_description="Athlete access has been revoked")
        return await self._issue_token_pair(
            client_id=client.client_id,
            subject=subject,
            email=str(document["email"]),
            scopes=list(document["scopes"]),
            grant_id=str(document["grant_id"]),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> StoredRefreshToken | None:
        document = await self.store.get(OAUTH_REFRESH_TOKENS, document_key("refresh-token", refresh_token))
        if document is None or document.get("client_id") != client.client_id:
            return None
        if document.get("consumed_at"):
            grant_id = document.get("grant_id")
            if isinstance(grant_id, str):
                await self.revoke_grant(grant_id)
            return None
        if document.get("resource") != self.config.resource_url or float(document.get("expires_at", 0)) <= time.time():
            return None
        subject = document.get("subject")
        if (
            not isinstance(subject, str)
            or not await self.subject_is_allowed(subject)
            or not await self._grant_is_active(str(document.get("grant_id", "")))
        ):
            return None
        return self._refresh_token(refresh_token, document)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: StoredRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        document = await self.store.consume(
            OAUTH_REFRESH_TOKENS,
            document_key("refresh-token", refresh_token.token),
        )
        if document is None:
            existing = await self.store.get(OAUTH_REFRESH_TOKENS, document_key("refresh-token", refresh_token.token))
            grant_id = existing.get("grant_id") if existing else None
            if isinstance(grant_id, str):
                await self.revoke_grant(grant_id)
            raise TokenError(error="invalid_grant", error_description="Refresh token was already used")
        if document.get("client_id") != client.client_id:
            raise TokenError(error="invalid_grant", error_description="Refresh token was already used")
        subject = str(document.get("subject", ""))
        if not subject or not await self.subject_is_allowed(subject):
            raise TokenError(error="invalid_grant", error_description="Athlete access has been revoked")
        previous_access_id = document.get("access_token_id")
        if isinstance(previous_access_id, str):
            await self.store.delete(OAUTH_ACCESS_TOKENS, previous_access_id)
        return await self._issue_token_pair(
            client_id=client.client_id,
            subject=subject,
            email=str(document["email"]),
            scopes=scopes,
            grant_id=str(document["grant_id"]),
        )

    async def load_access_token(self, token: str) -> StoredAccessToken | None:
        document = await self.store.get(OAUTH_ACCESS_TOKENS, document_key("access-token", token))
        if document is None or document.get("resource") != self.config.resource_url:
            return None
        subject = document.get("subject")
        if (
            not isinstance(subject, str)
            or not await self.subject_is_allowed(subject)
            or not await self._grant_is_active(str(document.get("grant_id", "")))
        ):
            return None
        return self._access_token(token, document)

    async def revoke_token(self, token: StoredAccessToken | StoredRefreshToken) -> None:
        await self.revoke_grant(token.grant_id)

    async def _grant_is_active(self, grant_id: str) -> bool:
        grant = await self.store.get(OAUTH_GRANTS, grant_id)
        return bool(grant and not grant.get("revoked_at") and float(grant.get("expires_at", 0)) > time.time())

    async def revoke_grant(self, grant_id: str) -> None:
        expires_at = time.time() + REFRESH_TOKEN_TTL_SECONDS
        await self.store.put(
            OAUTH_GRANTS,
            grant_id,
            {
                "revoked_at": time.time(),
                "expires_at": expires_at,
                "expires_at_timestamp": ttl_timestamp(expires_at),
            },
            merge=True,
        )
        await self.store.delete_where(OAUTH_ACCESS_TOKENS, "grant_id", grant_id)
        await self.store.delete_where(OAUTH_REFRESH_TOKENS, "grant_id", grant_id)
        await self.store.delete_where(OAUTH_CODES, "grant_id", grant_id)

    async def revoke_subject(self, subject: str) -> None:
        for grant_id, _ in await self.store.query(OAUTH_GRANTS, "subject", subject):
            await self.revoke_grant(grant_id)
        await self.store.delete_where(OAUTH_ACCESS_TOKENS, "subject", subject)
        await self.store.delete_where(OAUTH_REFRESH_TOKENS, "subject", subject)
        await self.store.delete_where(OAUTH_CODES, "subject", subject)
