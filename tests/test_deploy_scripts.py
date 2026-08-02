"""Focused safety tests for the manual Cloud Run deployment scripts."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMON_SH = REPO_ROOT / "deploy" / "common.sh"
BOOTSTRAP_SH = REPO_ROOT / "deploy" / "bootstrap.sh"
DEPLOY_SH = REPO_ROOT / "deploy" / "deploy.sh"
AUTH_OVERRIDE_ENVIRONMENT = {
    "CLOUDSDK_AUTH_ACCESS_TOKEN",
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
    "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT",
}


def _environment(fake_bin: Path, **updates: str) -> dict[str, str]:
    environment = os.environ.copy()
    for name in AUTH_OVERRIDE_ENVIRONMENT:
        environment.pop(name, None)
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "GCLOUD_ACCOUNT": "owner@example.com",
            **updates,
        }
    )
    return environment


def _write_fake_gcloud(fake_bin: Path, body: str) -> None:
    gcloud = fake_bin / "gcloud"
    gcloud.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8")
    gcloud.chmod(0o755)


def _write_fake_curl(fake_bin: Path, *, authorization_location: str) -> None:
    curl = fake_bin / "curl"
    curl.write_text(
        f"""#!/usr/bin/env bash
set -eu
url="${{@: -1}}"
printf 'curl %s\\n' "$url" >>"$FAKE_COMMAND_LOG"
case "$url" in
  */healthz|*/.well-known/oauth-authorization-server|*/.well-known/oauth-protected-resource/mcp)
    printf '200'
    ;;
  */register)
    printf '%s\\n201' '{{"client_id":"client-123","client_secret":"must-not-be-printed"}}'
    ;;
  */authorize)
    printf 'HTTP/2 302\\r\\nlocation: {authorization_location}\\r\\n\\r\\n\\n302'
    ;;
  */mcp)
    printf 'HTTP/2 401\\r\\nwww-authenticate: Bearer resource_metadata="https://service.example/.well-known/oauth-protected-resource/mcp"\\r\\n\\r\\n\\n401'
    ;;
  *)
    exit 97
    ;;
esac
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)


def _write_deploy_gcloud(fake_bin: Path) -> None:
    _write_fake_gcloud(
        fake_bin,
        """
printf 'gcloud %s\n' "$*" >>"$FAKE_COMMAND_LOG"
if [[ "$1 $2" == "config get-value" ]]; then
  exit 0
elif [[ "$1 $2" == "auth list" ]]; then
  printf 'owner@example.com\n'
elif [[ "$1 $2" == "projects describe" ]]; then
  printf 'trainingpeaks-bs-mcp\n'
elif [[ "$1 $2" == "secrets describe" ]]; then
  exit 0
elif [[ "$1 $2 $3" == "secrets versions list" ]]; then
  printf 'projects/trainingpeaks-bs-mcp/secrets/google-oauth-client-secret/versions/7\n'
elif [[ "$1 $2 $3" == "iam service-accounts describe" ]]; then
  exit 0
elif [[ "$1 $2 $3" == "storage buckets describe" ]]; then
  exit 0
elif [[ "$1 $2 $3" == "run services describe" ]]; then
  printf 'https://service.example\n'
elif [[ "$1 $2" == "builds submit" ]]; then
  exit 0
elif [[ "$1 $2" == "run deploy" ]]; then
  exit 0
elif [[ "$1 $2 $3" == "run services list" ]]; then
  printf 'trainingpeaks-mcp-00002-abc\thttps://candidate.example\n'
elif [[ "$1 $2 $3" == "run services update-traffic" ]]; then
  exit 0
else
  exit 98
fi
""",
    )


def _run_common(command: str, *, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    source = shlex.quote(str(COMMON_SH))
    return subprocess.run(
        ["/bin/bash", "-c", f"set -Eeuo pipefail; source {source}; {command}"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


@pytest.mark.parametrize("service", ["a", "trainingpeaks-mcp", "a" * 48 + "0"])
def test_cloud_run_service_name_accepts_valid_boundary(tmp_path: Path, service: str) -> None:
    result = _run_common("validate_deploy_settings", environment=_environment(tmp_path, SERVICE=service))

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("service", ["trainingpeaks-", "a" * 50, "1service", "Service"])
def test_cloud_run_service_name_rejects_invalid_values(tmp_path: Path, service: str) -> None:
    result = _run_common("validate_deploy_settings", environment=_environment(tmp_path, SERVICE=service))

    assert result.returncode != 0
    assert "Invalid SERVICE" in result.stderr


@pytest.mark.parametrize(
    "property_name",
    ["impersonate_service_account", "credential_file_override", "access_token_file"],
)
def test_gcloud_auth_property_overrides_are_rejected(tmp_path: Path, property_name: str) -> None:
    _write_fake_gcloud(
        tmp_path,
        """
if [[ "$1 $2" == "config get-value" ]]; then
  if [[ "$3" == "auth/${FAKE_OVERRIDE_PROPERTY}" ]]; then
    printf 'override-value\\n'
  fi
  exit 0
fi
if [[ "$1 $2" == "auth list" ]]; then
  printf 'owner@example.com\\n'
  exit 0
fi
exit 99
""",
    )
    result = _run_common(
        "guard_personal_gcloud_account",
        environment=_environment(tmp_path, FAKE_OVERRIDE_PROPERTY=property_name),
    )

    assert result.returncode != 0
    assert f"auth/{property_name} override" in result.stderr


def test_direct_gcloud_access_token_is_rejected_before_gcloud_is_called(tmp_path: Path) -> None:
    _write_fake_gcloud(tmp_path, "exit 88\n")
    result = _run_common(
        "guard_personal_gcloud_account",
        environment=_environment(tmp_path, CLOUDSDK_AUTH_ACCESS_TOKEN="opaque-token"),
    )

    assert result.returncode != 0
    assert "Refusing CLOUDSDK_AUTH_ACCESS_TOKEN" in result.stderr


def test_bootstrap_refuses_to_replace_an_existing_cloud_run_service(tmp_path: Path) -> None:
    command_log = tmp_path / "gcloud.log"
    _write_fake_gcloud(
        tmp_path,
        """
printf '%s\\n' "$*" >>"$FAKE_GCLOUD_LOG"
if [[ "$1 $2" == "config get-value" ]]; then
  exit 0
fi
if [[ "$1 $2" == "auth list" ]]; then
  printf 'owner@example.com\\n'
elif [[ "$1 $2" == "projects describe" ]]; then
  printf 'trainingpeaks-bs-mcp\\n'
elif [[ "$1 $2 $3" == "run services list" ]]; then
  printf 'trainingpeaks-mcp\\n'
fi
""",
    )
    result = subprocess.run(
        ["/bin/bash", str(BOOTSTRAP_SH)],
        check=False,
        capture_output=True,
        text=True,
        env=_environment(
            tmp_path,
            ALLOW_DIRTY="1",
            BILLING_ACCOUNT_ID="000000-000000-000000",
            FAKE_GCLOUD_LOG=str(command_log),
        ),
    )

    assert result.returncode != 0
    assert "bootstrap is a one-time operation" in result.stderr
    assert "run deploy" not in command_log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("firestore_configuration", "expected_error"),
    [
        ("DATASTORE_MODE\tme-west1\tDELETE_PROTECTION_ENABLED", "expected FIRESTORE_NATIVE"),
        ("FIRESTORE_NATIVE\tus-central1\tDELETE_PROTECTION_ENABLED", "expected me-west1"),
        ("FIRESTORE_NATIVE\tme-west1\tDELETE_PROTECTION_DISABLED", "does not have delete protection"),
    ],
)
def test_bootstrap_rejects_incompatible_existing_firestore_database(
    tmp_path: Path,
    firestore_configuration: str,
    expected_error: str,
) -> None:
    _write_fake_gcloud(
        tmp_path,
        """
if [[ "$1 $2" == "config get-value" ]]; then
  exit 0
fi
if [[ "$1 $2" == "auth list" ]]; then
  printf 'owner@example.com\\n'
elif [[ "$1 $2" == "projects describe" ]]; then
  if [[ "$*" == *"projectNumber"* ]]; then
    printf '123456789\\n'
  else
    printf 'trainingpeaks-bs-mcp\\n'
  fi
elif [[ "$1 $2 $3" == "run services list" ]]; then
  exit 0
elif [[ "$1 $2 $3" == "firestore databases list" ]]; then
  printf 'projects/trainingpeaks-bs-mcp/databases/(default)\\n'
elif [[ "$1 $2 $3" == "firestore databases describe" ]]; then
  printf '%s\\n' "$FAKE_FIRESTORE_CONFIGURATION"
fi
""",
    )
    result = subprocess.run(
        ["/bin/bash", str(BOOTSTRAP_SH)],
        check=False,
        capture_output=True,
        text=True,
        env=_environment(
            tmp_path,
            ALLOW_DIRTY="1",
            BILLING_ACCOUNT_ID="000000-000000-000000",
            FAKE_FIRESTORE_CONFIGURATION=firestore_configuration,
        ),
    )

    assert result.returncode != 0
    assert expected_error in result.stderr


def test_deploy_smokes_zero_traffic_candidate_before_exact_revision_promotion(tmp_path: Path) -> None:
    command_log = tmp_path / "commands.log"
    _write_deploy_gcloud(tmp_path)
    _write_fake_curl(
        tmp_path,
        authorization_location="https://accounts.google.com/o/oauth2/v2/auth?state=opaque",
    )
    result = subprocess.run(
        ["/bin/bash", "-x", str(DEPLOY_SH)],
        check=False,
        capture_output=True,
        text=True,
        env=_environment(
            tmp_path,
            ALLOW_DIRTY="1",
            GOOGLE_OAUTH_CLIENT_ID="google-client.example",
            FAKE_COMMAND_LOG=str(command_log),
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "must-not-be-printed" not in result.stdout
    assert "must-not-be-printed" not in result.stderr
    commands = command_log.read_text(encoding="utf-8").splitlines()
    deploy_index = next(index for index, command in enumerate(commands) if command.startswith("gcloud run deploy "))
    promote_index = next(
        index for index, command in enumerate(commands) if command.startswith("gcloud run services update-traffic ")
    )
    assert "--no-traffic" in commands[deploy_index]
    assert "--tag=candidate" in commands[deploy_index]
    assert "--to-revisions=trainingpeaks-mcp-00002-abc=100" in commands[promote_index]
    for endpoint in ("/healthz", "/register", "/authorize", "/mcp"):
        smoke_index = next(index for index, command in enumerate(commands) if command.endswith(endpoint))
        assert deploy_index < smoke_index < promote_index


def test_deploy_does_not_promote_candidate_with_non_google_authorization_redirect(tmp_path: Path) -> None:
    command_log = tmp_path / "commands.log"
    _write_deploy_gcloud(tmp_path)
    _write_fake_curl(tmp_path, authorization_location="http://127.0.0.1/callback?error=server_error")
    result = subprocess.run(
        ["/bin/bash", str(DEPLOY_SH)],
        check=False,
        capture_output=True,
        text=True,
        env=_environment(
            tmp_path,
            ALLOW_DIRTY="1",
            GOOGLE_OAUTH_CLIENT_ID="google-client.example",
            FAKE_COMMAND_LOG=str(command_log),
        ),
    )

    assert result.returncode != 0
    assert "did not redirect to Google" in result.stderr
    assert "run services update-traffic" not in command_log.read_text(encoding="utf-8")
