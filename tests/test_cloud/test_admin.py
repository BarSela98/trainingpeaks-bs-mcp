"""Cloud allowlist administration and durable grant revocation tests."""

from __future__ import annotations

import json

import pytest

from tp_mcp.cloud.admin import _verify_personal_adc, build_parser, invite, revoke
from tp_mcp.cloud.oauth import GoogleIdentity, email_key
from tp_mcp.cloud.storage import (
    ALLOWLIST,
    ALLOWLIST_SUBJECTS,
    BROWSER_FLOWS,
    ENROLLMENTS,
    OAUTH_ACCESS_TOKENS,
    OAUTH_CODES,
    OAUTH_CONSENTS,
    OAUTH_GRANTS,
    OAUTH_REFRESH_TOKENS,
    OAUTH_TRANSACTIONS,
)


@pytest.mark.asyncio
async def test_revoke_tombstones_grants_and_cleans_pending_subject_flows(store, provider) -> None:
    email = "athlete@example.com"
    subject = "google-subject-1"
    await invite(store, email)
    assert await provider.bind_invited_identity(GoogleIdentity(subject=subject, email=email))

    await store.put(OAUTH_GRANTS, "grant-1", {"subject": subject, "expires_at": 9_999_999_999})
    for collection in (OAUTH_ACCESS_TOKENS, OAUTH_REFRESH_TOKENS, OAUTH_CODES):
        await store.put(collection, f"{collection}-by-grant", {"grant_id": "grant-1", "subject": subject})
        await store.put(collection, f"{collection}-legacy", {"subject": subject})
    for collection in (OAUTH_TRANSACTIONS, OAUTH_CONSENTS, ENROLLMENTS, BROWSER_FLOWS):
        await store.put(collection, f"pending-{collection}", {"subject": subject})

    await revoke(store, email)

    invite_record = await store.get(ALLOWLIST, email_key(email))
    subject_record = await store.get(ALLOWLIST_SUBJECTS, subject)
    grant = await store.get(OAUTH_GRANTS, "grant-1")
    assert invite_record is not None and invite_record["enabled"] is False
    assert subject_record is not None and subject_record["enabled"] is False
    assert grant is not None and isinstance(grant.get("revoked_at"), float)
    assert grant.get("expires_at_timestamp") is not None
    for collection in (
        OAUTH_ACCESS_TOKENS,
        OAUTH_REFRESH_TOKENS,
        OAUTH_CODES,
        OAUTH_TRANSACTIONS,
        OAUTH_CONSENTS,
        ENROLLMENTS,
        BROWSER_FLOWS,
    ):
        assert await store.query(collection, "subject", subject) == []


def test_admin_cli_has_no_credential_disconnect_command() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--project", "test-project", "disconnect", "athlete@example.com"])


def test_admin_adc_guard_requires_matching_personal_account_and_project(tmp_path, monkeypatch) -> None:
    from google.auth import _cloud_sdk

    adc_path = tmp_path / "application_default_credentials.json"
    adc_path.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "account": "owner@gmail.com",
                "quota_project_id": "trainingpeaks-test",
            }
        )
    )
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr(_cloud_sdk, "get_application_default_credentials_path", lambda: str(adc_path))

    _verify_personal_adc("owner@gmail.com", "trainingpeaks-test")

    with pytest.raises(ValueError, match="do not match GCLOUD_ACCOUNT"):
        _verify_personal_adc("someone-else@gmail.com", "trainingpeaks-test")
    with pytest.raises(ValueError, match="quota project"):
        _verify_personal_adc("owner@gmail.com", "different-project")


def test_admin_adc_guard_rejects_credential_file_override(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/untrusted-credentials.json")

    with pytest.raises(ValueError, match="overrides personal ADC"):
        _verify_personal_adc("owner@gmail.com", "trainingpeaks-test")
