"""Invite-only athlete administration for the Cloud Run deployment."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from tp_mcp.cloud.oauth import REFRESH_TOKEN_TTL_SECONDS, email_key
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
    CloudStore,
    FirestoreCloudStore,
    ttl_timestamp,
)


def _verify_personal_adc(expected_account: str, expected_project: str) -> None:
    """Fail closed when Firestore ADC differs from the guarded gcloud user."""
    normalized_expected = expected_account.strip().casefold()
    if normalized_expected.endswith("@ridewithvia.com") or normalized_expected.endswith(".gserviceaccount.com"):
        raise ValueError("Application Default Credentials must use the selected personal Google account")
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        raise ValueError(
            "GOOGLE_APPLICATION_CREDENTIALS overrides personal ADC; unset it and run "
            f"'gcloud auth application-default login {expected_account}'"
        )
    try:
        from google.auth import _cloud_sdk

        adc_document = json.loads(Path(_cloud_sdk.get_application_default_credentials_path()).read_text())
    except (ImportError, OSError, ValueError) as exc:
        raise ValueError(
            f"Personal ADC is unavailable; run 'gcloud auth application-default login {expected_account}'"
        ) from exc

    if adc_document.get("type") != "authorized_user":
        raise ValueError("Application Default Credentials must be personal authorized-user credentials")
    adc_account = adc_document.get("account")
    if not isinstance(adc_account, str) or adc_account.strip().casefold() != normalized_expected:
        raise ValueError(
            "Application Default Credentials do not match GCLOUD_ACCOUNT; run "
            f"'gcloud auth application-default login {expected_account}'"
        )
    quota_project = adc_document.get("quota_project_id")
    if quota_project != expected_project:
        raise ValueError(
            "ADC quota project does not match the target; run "
            f"'gcloud auth application-default set-quota-project {expected_project}'"
        )


def _normalize_email(value: str) -> str:
    email = value.strip().casefold()
    if "@" not in email or email.startswith("@") or email.endswith("@") or len(email) > 254:
        raise ValueError("A valid email address is required")
    return email


async def _revoke_subject_tokens(store: CloudStore, subject: str) -> int:
    """Revoke every durable grant and pending flow for a Google subject.

    Grant documents are deliberately retained as tombstones so a delayed or
    replayed code/refresh token cannot recreate access after an administrator
    removes the athlete from the allowlist.
    """
    now = time.time()
    tombstone_expiry = now + REFRESH_TOKEN_TTL_SECONDS
    affected = 0
    for grant_id, _ in await store.query(OAUTH_GRANTS, "subject", subject):
        await store.put(
            OAUTH_GRANTS,
            grant_id,
            {
                "revoked_at": now,
                "expires_at": tombstone_expiry,
                "expires_at_timestamp": ttl_timestamp(tombstone_expiry),
            },
            merge=True,
        )
        affected += 1
        affected += await store.delete_where(OAUTH_ACCESS_TOKENS, "grant_id", grant_id)
        affected += await store.delete_where(OAUTH_REFRESH_TOKENS, "grant_id", grant_id)
        affected += await store.delete_where(OAUTH_CODES, "grant_id", grant_id)

    # Defense-in-depth cleanup for legacy records and partially completed
    # authorizations that predate the stateless-cookie flow.
    for collection in (
        OAUTH_ACCESS_TOKENS,
        OAUTH_REFRESH_TOKENS,
        OAUTH_CODES,
        OAUTH_CONSENTS,
        OAUTH_TRANSACTIONS,
        ENROLLMENTS,
        BROWSER_FLOWS,
    ):
        affected += await store.delete_where(collection, "subject", subject)
    return affected


async def invite(store: CloudStore, email_value: str) -> None:
    email = _normalize_email(email_value)
    identifier = email_key(email)
    current = await store.get(ALLOWLIST, identifier)
    now = time.time()
    await store.put(
        ALLOWLIST,
        identifier,
        {
            "email": email,
            "enabled": True,
            "invited_at": current.get("invited_at", now) if current else now,
            "updated_at": now,
        },
        merge=True,
    )
    subject = current.get("subject") if current else None
    if isinstance(subject, str):
        await store.put(
            ALLOWLIST_SUBJECTS,
            subject,
            {"enabled": True, "allowlist_id": identifier, "email": email, "updated_at": now},
        )
    print(f"Invited {email}")


async def revoke(store: CloudStore, email_value: str) -> None:
    email = _normalize_email(email_value)
    identifier = email_key(email)
    current = await store.get(ALLOWLIST, identifier)
    if current is None:
        raise ValueError(f"No invitation exists for {email}")
    await store.put(ALLOWLIST, identifier, {"enabled": False, "revoked_at": time.time()}, merge=True)
    subject = current.get("subject")
    revoked_tokens = 0
    if isinstance(subject, str):
        await store.put(ALLOWLIST_SUBJECTS, subject, {"enabled": False, "updated_at": time.time()}, merge=True)
        revoked_tokens = await _revoke_subject_tokens(store, subject)
    print(f"Revoked {email}; invalidated {revoked_tokens} OAuth and pending-flow record(s)")


async def list_invites(store: CloudStore) -> None:
    records = await store.scan(ALLOWLIST)
    if not records:
        print("No invited athletes.")
        return
    for _, record in sorted(records, key=lambda item: str(item[1].get("email", ""))):
        email = record.get("email", "<unknown>")
        status = "enabled" if record.get("enabled") is True else "revoked"
        identity = "bound" if record.get("subject") else "unbound"
        print(f"{email}\t{status}\t{identity}")


async def run(args: argparse.Namespace, store: CloudStore | None = None) -> int:
    if store is None:
        if not args.expected_account:
            raise ValueError("--expected-account or GCLOUD_ACCOUNT is required for cloud administration")
        _verify_personal_adc(args.expected_account, args.project)
    cloud_store = store or FirestoreCloudStore(args.project, args.database)
    if args.command == "invite":
        await invite(cloud_store, args.email)
    elif args.command == "revoke":
        await revoke(cloud_store, args.email)
    else:
        await list_invites(cloud_store)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage TrainingPeaks MCP invitations")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"), required=False)
    parser.add_argument("--database", default=os.environ.get("TP_MCP_FIRESTORE_DATABASE", "(default)"))
    parser.add_argument("--expected-account", default=os.environ.get("GCLOUD_ACCOUNT"))
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("invite", "revoke"):
        subcommand = commands.add_parser(command)
        subcommand.add_argument("email")
    commands.add_parser("list")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.project:
        parser.error("--project or GOOGLE_CLOUD_PROJECT is required")
    try:
        return asyncio.run(run(args))
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
