"""Tenant isolation tests for remote TrainingPeaks client requests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from tp_mcp.client.context import (
    MAX_TRAININGPEAKS_AUTH_BYTES,
    cloud_credential,
    cloud_principal,
    cloud_request_context,
    is_safe_trainingpeaks_credential,
)
from tp_mcp.client.http import APIResponse, ErrorCode, TPClient


@pytest.fixture(autouse=True)
def _clear_client_caches():
    """Keep the legacy local-stdio caches deterministic between tests."""
    TPClient._cached_athlete_id = None
    TPClient._cached_user_data = None
    TPClient._shared_token_cache = None
    yield
    TPClient._cached_athlete_id = None
    TPClient._cached_user_data = None
    TPClient._shared_token_cache = None


class TestCloudRequestContext:
    @pytest.mark.parametrize("credential", ["abc.DEF-_123=", b"opaque%2Fvalue"])
    def test_accepts_single_cookie_values(self, credential):
        assert is_safe_trainingpeaks_credential(credential)

    @pytest.mark.parametrize(
        "credential",
        ["", " leading", "trailing ", "one;other=two", "one,two", '"quoted"', "back\\slash", "nonascii-א"],
    )
    def test_rejects_ambiguous_or_non_ascii_cookie_values(self, credential):
        assert not is_safe_trainingpeaks_credential(credential)

    def test_rejects_oversized_cookie_value(self):
        assert not is_safe_trainingpeaks_credential("x" * (MAX_TRAININGPEAKS_AUTH_BYTES + 1))

    def test_binds_and_restores_nested_context(self):
        assert cloud_principal.get() is None
        assert cloud_credential.get() is None

        with cloud_request_context(" principal-a ", "cookie-a"):
            assert cloud_principal.get() == "principal-a"
            assert cloud_credential.get() == "cookie-a"
            with cloud_request_context("principal-b", None):
                assert cloud_principal.get() == "principal-b"
                assert cloud_credential.get() is None
            assert cloud_principal.get() == "principal-a"
            assert cloud_credential.get() == "cookie-a"

        assert cloud_principal.get() is None
        assert cloud_credential.get() is None

    def test_restores_context_after_exception(self):
        with (
            pytest.raises(RuntimeError, match="boom"),
            cloud_request_context("principal-a", "cookie-a"),
        ):
            raise RuntimeError("boom")

        assert cloud_principal.get() is None
        assert cloud_credential.get() is None

    @pytest.mark.parametrize("principal", ["", "   "])
    def test_rejects_empty_principal(self, principal):
        with (
            pytest.raises(ValueError, match="cannot be empty"),
            cloud_request_context(principal, "cookie"),
        ):
            pass


class TestCloudCredentialIsolation:
    def test_cloud_credential_is_available_only_during_its_request(self):
        with cloud_request_context("principal", "cookie-a"):
            first_request = TPClient()
            assert first_request._credential_cookie() == "cookie-a"

        assert first_request._credential_cookie() is None

        with cloud_request_context("principal", "cookie-b"):
            second_request = TPClient()
            assert second_request._credential_cookie() == "cookie-b"

        assert second_request._credential_cookie() is None

        with cloud_request_context("principal", None):
            missing_request = TPClient()
            assert missing_request._credential_cookie() is None

    def test_client_cannot_read_another_principals_request_cookie(self):
        with cloud_request_context("principal-a", "cookie-a"):
            principal_a_client = TPClient()

        with cloud_request_context("principal-b", "cookie-b"):
            assert principal_a_client._credential_cookie() is None

    def test_missing_cloud_credential_never_falls_back_to_keyring(self):
        with (
            patch("tp_mcp.client.http.get_credential") as get_local_credential,
            cloud_request_context("principal-a", None),
        ):
            client = TPClient()
            assert client._credential_cookie() is None
            get_local_credential.assert_not_called()
            assert "X-TrainingPeaks-Auth" in client._missing_credential_message()

    def test_local_client_still_uses_keyring(self):
        local_result = SimpleNamespace(success=True, cookie="local-cookie")
        with patch("tp_mcp.client.http.get_credential", return_value=local_result) as get_local_credential:
            client = TPClient()
            assert client._credential_cookie() == "local-cookie"

        get_local_credential.assert_called_once_with()

    def test_rejects_credential_without_authenticated_principal(self):
        credential_token = cloud_credential.set("orphaned-cookie")
        try:
            with pytest.raises(RuntimeError, match="without an authenticated principal"):
                TPClient()
        finally:
            cloud_credential.reset(credential_token)

    @pytest.mark.asyncio
    async def test_missing_cloud_credential_returns_remote_auth_error(self):
        with (
            patch("tp_mcp.client.http.get_credential") as get_local_credential,
            cloud_request_context("principal-a", None),
        ):
            client = TPClient()
            client._client = AsyncMock()
            response = await client._exchange_cookie_for_token()

        assert response.error_code == ErrorCode.AUTH_INVALID
        assert "X-TrainingPeaks-Auth" in response.message
        get_local_credential.assert_not_called()
        client._client.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_concurrent_token_exchanges_use_each_principals_cookie(self):
        async def token_response(**kwargs):
            cookie = kwargs["headers"]["Cookie"]
            token = "token-a" if cookie.endswith("cookie-a") else "token-b"
            return httpx.Response(
                200,
                json={"success": True, "token": {"access_token": token, "expires_in": 3600}},
            )

        async def exchange(principal: str, cookie: str):
            with cloud_request_context(principal, cookie):
                client = TPClient()
                client._client = AsyncMock()
                client._client.request.side_effect = token_response
                result = await client._ensure_access_token()
                authorization = client._get_headers()["Authorization"]
                exchange_cookie = client._client.request.call_args.kwargs["headers"]["Cookie"]
            return client, result, authorization, exchange_cookie

        exchange_a, exchange_b = await asyncio.gather(
            exchange("principal-a", "cookie-a"),
            exchange("principal-b", "cookie-b"),
        )
        client_a, result_a, authorization_a, exchange_cookie_a = exchange_a
        client_b, result_b, authorization_b, exchange_cookie_b = exchange_b

        assert result_a.success and result_b.success
        assert client_a._token_cache is not client_b._token_cache
        assert authorization_a == "Bearer token-a"
        assert authorization_b == "Bearer token-b"
        assert exchange_cookie_a.endswith("cookie-a")
        assert exchange_cookie_b.endswith("cookie-b")
        assert client_a._credential_cookie() is None
        assert client_b._credential_cookie() is None

    @pytest.mark.asyncio
    async def test_second_request_for_same_subject_cannot_reuse_first_bearer_token(self):
        with cloud_request_context("principal-a", "cookie-a"):
            first_request = TPClient()
            first_request._client = AsyncMock()
            first_request._client.request.return_value = httpx.Response(
                200,
                json={"success": True, "token": {"access_token": "token-1", "expires_in": 3600}},
            )
            first_result = await first_request._ensure_access_token()
            first_authorization = first_request._get_headers()["Authorization"]

        with cloud_request_context("principal-a", "cookie-a"):
            second_request = TPClient()
            second_request._client = AsyncMock()
            second_request._client.request.return_value = httpx.Response(
                200,
                json={"success": True, "token": {"access_token": "token-2", "expires_in": 3600}},
            )

            assert second_request._token_cache is not first_request._token_cache
            assert second_request._token_cache.access_token is None
            second_result = await second_request._ensure_access_token()
            second_authorization = second_request._get_headers()["Authorization"]

        assert first_result.success
        assert first_authorization == "Bearer token-1"
        assert second_result.success
        assert second_authorization == "Bearer token-2"
        first_request._client.request.assert_awaited_once()
        second_request._client.request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_clears_all_remote_request_derived_state(self):
        with cloud_request_context("principal-a", "cookie-a"):
            client = TPClient()
            http_client = AsyncMock()
            client._client = http_client
            client._token_cache.access_token = "derived-token"
            client._token_cache.expires_at = float("inf")
            client._athlete_id = 101
            client._remote_user_data = {"personId": 101}

            await client.close()

        http_client.aclose.assert_awaited_once()
        assert client._client is None
        assert client._token_cache.access_token is None
        assert client._token_cache.expires_at == 0.0
        assert client.athlete_id is None
        assert client._remote_user_data is None
        assert client._credential_cookie() is None


class TestCloudProfileIsolation:
    @pytest.mark.asyncio
    async def test_athlete_and_profile_caches_are_request_local(self):
        # Legacy local stdio state must not be visible to either remote request.
        TPClient._cached_athlete_id = 999
        TPClient._cached_user_data = {"personId": 999}

        async def resolve(principal: str, cookie: str, person_id: int, email: str):
            with cloud_request_context(principal, cookie):
                client = TPClient()
                client.get = AsyncMock(
                    return_value=APIResponse(
                        success=True,
                        data={"user": {"personId": person_id, "email": email}},
                    )
                )
                first_result = await client.ensure_athlete_id()
                second_result = await client.ensure_athlete_id()
            return client, first_result, second_result

        resolved_a, resolved_b = await asyncio.gather(
            resolve("principal-a", "cookie-a", 101, "a@example.com"),
            resolve("principal-b", "cookie-b", 202, "b@example.com"),
        )
        client_a, athlete_a, cached_athlete_a = resolved_a
        client_b, athlete_b, cached_athlete_b = resolved_b

        assert athlete_a == 101
        assert athlete_b == 202
        assert cached_athlete_a == 101
        assert cached_athlete_b == 202
        assert client_a._remote_user_data == {"personId": 101, "email": "a@example.com"}
        assert client_b._remote_user_data == {"personId": 202, "email": "b@example.com"}
        assert client_a.athlete_id == 101
        assert client_b.athlete_id == 202
        assert TPClient._cached_athlete_id == 999
        assert TPClient._cached_user_data == {"personId": 999}
        client_a.get.assert_awaited_once()
        client_b.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_second_request_for_same_subject_refetches_profile(self):
        with cloud_request_context("principal-a", "cookie-a"):
            first_request = TPClient()
            first_request.get = AsyncMock(return_value=APIResponse(success=True, data={"user": {"personId": 101}}))
            assert await first_request.ensure_athlete_id() == 101

        with cloud_request_context("principal-a", "cookie-a"):
            second_request = TPClient()
            second_request.get = AsyncMock(return_value=APIResponse(success=True, data={"user": {"personId": 102}}))

            assert second_request.athlete_id is None
            assert second_request._remote_user_data is None
            assert await second_request.ensure_athlete_id() == 102
        first_request.get.assert_awaited_once()
        second_request.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_local_stdio_caches_remain_shared(self):
        first_client = TPClient()
        second_client = TPClient()

        assert first_client._token_cache is second_client._token_cache
        first_client._token_cache.access_token = "local-token"
        first_client._token_cache.expires_at = float("inf")
        assert second_client._token_cache.access_token == "local-token"

        TPClient._cached_athlete_id = 999
        TPClient._cached_user_data = {"personId": 999}
        second_client.get = AsyncMock()

        assert await second_client.ensure_athlete_id() == 999
        second_client.get.assert_not_called()

        await first_client.close()

        assert second_client._token_cache.access_token == "local-token"
        assert TPClient._cached_athlete_id == 999
        assert TPClient._cached_user_data == {"personId": 999}
