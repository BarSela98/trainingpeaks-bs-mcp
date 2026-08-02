"""Small async document-store abstraction backed by Google Cloud Firestore."""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, Protocol

Document = dict[str, Any]


def ttl_timestamp(expires_at: float) -> datetime:
    """Firestore TTL field paired with the numeric deadline checked in code."""
    return datetime.fromtimestamp(expires_at, tz=timezone.utc)


class CloudStore(Protocol):
    """Operations required by OAuth, invite administration, and revocation."""

    async def get(self, collection: str, document_id: str) -> Document | None: ...

    async def put(self, collection: str, document_id: str, data: Document, *, merge: bool = False) -> None: ...

    async def create(self, collection: str, document_id: str, data: Document) -> bool: ...

    async def delete(self, collection: str, document_id: str) -> None: ...

    async def consume(self, collection: str, document_id: str, *, now: float | None = None) -> Document | None: ...

    async def query(self, collection: str, field: str, value: Any) -> list[tuple[str, Document]]: ...

    async def scan(self, collection: str) -> list[tuple[str, Document]]: ...

    async def delete_where(self, collection: str, field: str, value: Any) -> int: ...


class InMemoryCloudStore:
    """Deterministic store for tests and local adapter smoke tests."""

    def __init__(self) -> None:
        self._documents: dict[str, dict[str, Document]] = {}
        self._lock = asyncio.Lock()

    async def get(self, collection: str, document_id: str) -> Document | None:
        async with self._lock:
            document = self._documents.get(collection, {}).get(document_id)
            return copy.deepcopy(document) if document is not None else None

    async def put(self, collection: str, document_id: str, data: Document, *, merge: bool = False) -> None:
        async with self._lock:
            documents = self._documents.setdefault(collection, {})
            if merge and document_id in documents:
                documents[document_id].update(copy.deepcopy(data))
            else:
                documents[document_id] = copy.deepcopy(data)

    async def create(self, collection: str, document_id: str, data: Document) -> bool:
        async with self._lock:
            documents = self._documents.setdefault(collection, {})
            if document_id in documents:
                return False
            documents[document_id] = copy.deepcopy(data)
            return True

    async def delete(self, collection: str, document_id: str) -> None:
        async with self._lock:
            self._documents.get(collection, {}).pop(document_id, None)

    async def consume(self, collection: str, document_id: str, *, now: float | None = None) -> Document | None:
        current_time = time.time() if now is None else now
        async with self._lock:
            document = self._documents.get(collection, {}).get(document_id)
            if document is None or document.get("consumed_at") or document.get("revoked_at"):
                return None
            expires_at = document.get("expires_at")
            if isinstance(expires_at, (int, float)) and expires_at <= current_time:
                return None
            result = copy.deepcopy(document)
            document["consumed_at"] = current_time
            return result

    async def query(self, collection: str, field: str, value: Any) -> list[tuple[str, Document]]:
        async with self._lock:
            return [
                (document_id, copy.deepcopy(document))
                for document_id, document in self._documents.get(collection, {}).items()
                if document.get(field) == value
            ]

    async def scan(self, collection: str) -> list[tuple[str, Document]]:
        async with self._lock:
            return [
                (document_id, copy.deepcopy(document))
                for document_id, document in self._documents.get(collection, {}).items()
            ]

    async def delete_where(self, collection: str, field: str, value: Any) -> int:
        async with self._lock:
            documents = self._documents.get(collection, {})
            ids = [document_id for document_id, document in documents.items() if document.get(field) == value]
            for document_id in ids:
                del documents[document_id]
            return len(ids)


class FirestoreCloudStore:
    """Production adapter using Application Default Credentials.

    Imports are intentionally local so users of the stdio transport do not
    need Google Cloud packages installed.
    """

    def __init__(self, project_id: str, database: str = "(default)") -> None:
        try:
            from google.cloud.firestore_v1.async_client import AsyncClient
        except ImportError as exc:  # pragma: no cover - exercised in base-only installations
            raise RuntimeError("Cloud support is not installed; install tp-mcp[cloud]") from exc
        self._client_class = AsyncClient
        self._project_id = project_id
        self._database = database
        self._client = None
        self._client_loop: asyncio.AbstractEventLoop | None = None

    def _client_for_running_loop(self):
        loop = asyncio.get_running_loop()
        if self._client is None or self._client_loop is not loop:
            self._client = self._client_class(project=self._project_id, database=self._database)
            self._client_loop = loop
        return self._client

    def _document(self, collection: str, document_id: str):
        return self._client_for_running_loop().collection(collection).document(document_id)

    async def get(self, collection: str, document_id: str) -> Document | None:
        snapshot = await self._document(collection, document_id).get()
        if not snapshot.exists:
            return None
        return dict(snapshot.to_dict() or {})

    async def put(self, collection: str, document_id: str, data: Document, *, merge: bool = False) -> None:
        await self._document(collection, document_id).set(data, merge=merge)

    async def create(self, collection: str, document_id: str, data: Document) -> bool:
        try:
            await self._document(collection, document_id).create(data)
            return True
        except Exception as exc:
            # Keep the optional dependency out of module import scope.
            try:
                from google.api_core.exceptions import AlreadyExists
            except ImportError:  # pragma: no cover
                raise
            if isinstance(exc, AlreadyExists):
                return False
            raise

    async def delete(self, collection: str, document_id: str) -> None:
        await self._document(collection, document_id).delete()

    async def consume(self, collection: str, document_id: str, *, now: float | None = None) -> Document | None:
        try:
            from google.cloud.firestore_v1 import async_transactional
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Cloud support is not installed; install tp-mcp[cloud]") from exc

        current_time = time.time() if now is None else now
        reference = self._document(collection, document_id)

        @async_transactional
        async def consume_in_transaction(transaction):
            snapshot = await reference.get(transaction=transaction)
            if not snapshot.exists:
                return None
            document = dict(snapshot.to_dict() or {})
            expires_at = document.get("expires_at")
            if (
                document.get("consumed_at")
                or document.get("revoked_at")
                or (isinstance(expires_at, (int, float)) and expires_at <= current_time)
            ):
                return None
            transaction.update(reference, {"consumed_at": current_time})
            return document

        return await consume_in_transaction(self._client_for_running_loop().transaction())

    async def _stream_query(self, collection: str, field: str, value: Any) -> AsyncIterator[Any]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = self._client_for_running_loop().collection(collection).where(filter=FieldFilter(field, "==", value))
        async for snapshot in query.stream():
            yield snapshot

    async def query(self, collection: str, field: str, value: Any) -> list[tuple[str, Document]]:
        results: list[tuple[str, Document]] = []
        async for snapshot in self._stream_query(collection, field, value):
            results.append((snapshot.id, dict(snapshot.to_dict() or {})))
        return results

    async def scan(self, collection: str) -> list[tuple[str, Document]]:
        results: list[tuple[str, Document]] = []
        async for snapshot in self._client_for_running_loop().collection(collection).stream():
            results.append((snapshot.id, dict(snapshot.to_dict() or {})))
        return results

    async def delete_where(self, collection: str, field: str, value: Any) -> int:
        count = 0
        async for snapshot in self._stream_query(collection, field, value):
            await snapshot.reference.delete()
            count += 1
        return count


# Collections are centralized to make IAM review and Firestore cleanup scripts
# easy to audit.
ALLOWLIST = "allowed_users"
ALLOWLIST_SUBJECTS = "allowed_subjects"
USERS = "users"
TRAININGPEAKS_IDENTITIES = "trainingpeaks_identities"
OAUTH_CLIENTS = "oauth_clients"
OAUTH_GRANTS = "oauth_grants"
OAUTH_TRANSACTIONS = "oauth_transactions"
OAUTH_CONSENTS = "oauth_consents"
OAUTH_CODES = "oauth_codes"
OAUTH_ACCESS_TOKENS = "oauth_access_tokens"
OAUTH_REFRESH_TOKENS = "oauth_refresh_tokens"
# Legacy collections are retained only so revocation/TTL maintenance can clean
# records written by pre-stateless revisions. New revisions never create them.
ENROLLMENTS = "enrollments"
BROWSER_FLOWS = "browser_flows"
