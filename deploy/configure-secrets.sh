#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

guard_personal_gcloud_account
validate_deploy_settings
require_project_access

runtime_service_account_email="${RUNTIME_SERVICE_ACCOUNT}@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud iam service-accounts describe "$runtime_service_account_email" \
  --project="$PROJECT_ID" >/dev/null || die "Runtime service account has not been created."

ensure_secret() {
  local secret_name="$1"
  if ! gcloud secrets describe "$secret_name" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud secrets create "$secret_name" \
      --replication-policy=user-managed \
      --locations="$REGION" \
      --project="$PROJECT_ID"
  fi
}

grant_secret_access() {
  local secret_name="$1"
  gcloud secrets add-iam-policy-binding "$secret_name" \
    --member="serviceAccount:${runtime_service_account_email}" \
    --role=roles/secretmanager.secretAccessor \
    --condition=None \
    --project="$PROJECT_ID" \
    --quiet >/dev/null
}

ensure_secret "$GOOGLE_OAUTH_CLIENT_SECRET_NAME"

if [[ -n "${GOOGLE_OAUTH_CLIENT_SECRET_FILE:-}" ]]; then
  [[ -f "$GOOGLE_OAUTH_CLIENT_SECRET_FILE" ]] || die \
    "GOOGLE_OAUTH_CLIENT_SECRET_FILE does not exist: ${GOOGLE_OAUTH_CLIENT_SECRET_FILE}"
  gcloud secrets versions add "$GOOGLE_OAUTH_CLIENT_SECRET_NAME" \
    --data-file="$GOOGLE_OAUTH_CLIENT_SECRET_FILE" \
    --project="$PROJECT_ID"
else
  read -r -s -p 'Google OAuth client secret (hidden): ' oauth_client_secret
  printf '\n'
  [[ -n "$oauth_client_secret" ]] || die "Google OAuth client secret cannot be empty."
  printf '%s' "$oauth_client_secret" | gcloud secrets versions add \
    "$GOOGLE_OAUTH_CLIENT_SECRET_NAME" --data-file=- --project="$PROJECT_ID"
  unset oauth_client_secret
fi
grant_secret_access "$GOOGLE_OAUTH_CLIENT_SECRET_NAME"

printf 'Secret versions configured. Values were not written to the repository or command line.\n'
