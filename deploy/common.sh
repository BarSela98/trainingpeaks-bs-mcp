#!/usr/bin/env bash

# Shared, source-only helpers for the manual Cloud Run deployment scripts.

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

guard_personal_gcloud_account() {
  require_command gcloud

  local active_account normalized_account override property

  # These inputs make gcloud ignore or impersonate the selected account. Check
  # them before `gcloud auth list`, otherwise that command can still report the
  # expected personal account while subsequent API calls use other credentials.
  [[ -z "${CLOUDSDK_AUTH_ACCESS_TOKEN:-}" ]] || die \
    "Refusing CLOUDSDK_AUTH_ACCESS_TOKEN. Use the selected personal gcloud account directly."
  for property in impersonate_service_account credential_file_override access_token_file; do
    if ! override="$(gcloud config get-value "auth/${property}" 2>/dev/null)"; then
      die "Could not inspect gcloud auth/${property}; refusing to continue."
    fi
    [[ -z "$override" ]] || die \
      "Refusing gcloud auth/${property} override. Use the selected personal account directly."
  done

  if ! active_account="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -n 1)"; then
    die "Could not read gcloud credentials. Check that your gcloud configuration is writable."
  fi
  [[ -n "$active_account" ]] || die "No active gcloud account. Run: gcloud auth login"

  normalized_account="$(printf '%s' "$active_account" | tr '[:upper:]' '[:lower:]')"
  case "$normalized_account" in
    *@ridewithvia.com)
      die "Refusing to use Via account ${active_account}. Activate your personal Google account."
      ;;
    *@*.gserviceaccount.com)
      die "Refusing to deploy as service account ${active_account}. Activate your personal Google account."
      ;;
  esac

  [[ -n "${GCLOUD_ACCOUNT:-}" ]] || die \
    "Set GCLOUD_ACCOUNT to the personal account you intend to use (active: ${active_account})."
  [[ "$active_account" == "$GCLOUD_ACCOUNT" ]] || die \
    "Active account ${active_account} does not match GCLOUD_ACCOUNT=${GCLOUD_ACCOUNT}."

  printf 'Using personal gcloud account: %s\n' "$active_account"
}

validate_deploy_settings() {
  [[ "$PROJECT_ID" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || die "Invalid PROJECT_ID: ${PROJECT_ID}"
  [[ "$REGION" =~ ^[a-z]+-[a-z]+[0-9]+$ ]] || die "Invalid REGION: ${REGION}"
  [[ ${#SERVICE} -le 49 && "$SERVICE" =~ ^[a-z]([a-z0-9-]*[a-z0-9])?$ ]] || die \
    "Invalid SERVICE (must be at most 49 characters and end with an alphanumeric): ${SERVICE}"
  [[ "$ARTIFACT_REPOSITORY" =~ ^[a-z][a-z0-9-]{0,62}$ ]] || die \
    "Invalid ARTIFACT_REPOSITORY: ${ARTIFACT_REPOSITORY}"
}

require_project_access() {
  gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null 2>&1 || die \
    "Cannot access project ${PROJECT_ID} with ${GCLOUD_ACCOUNT}. Run deploy/bootstrap.sh first."
}

build_service_account_email() {
  printf '%s@%s.iam.gserviceaccount.com' "$BUILD_SERVICE_ACCOUNT" "$PROJECT_ID"
}

build_bucket_name() {
  printf '%s' "${BUILD_BUCKET:-${PROJECT_ID}-cloud-build-${REGION}}"
}

PROJECT_ID="${PROJECT_ID:-trainingpeaks-bs-mcp}"
PROJECT_NAME="${PROJECT_NAME:-TrainingPeaks MCP}"
REGION="${REGION:-me-west1}"
SERVICE="${SERVICE:-trainingpeaks-mcp}"
ARTIFACT_REPOSITORY="${ARTIFACT_REPOSITORY:-trainingpeaks-mcp}"
RUNTIME_SERVICE_ACCOUNT="${RUNTIME_SERVICE_ACCOUNT:-trainingpeaks-mcp-runtime}"
BUILD_SERVICE_ACCOUNT="${BUILD_SERVICE_ACCOUNT:-trainingpeaks-mcp-builder}"
FIRESTORE_DATABASE="${FIRESTORE_DATABASE:-(default)}"
KMS_KEYRING="${KMS_KEYRING:-trainingpeaks-mcp}"
KMS_KEY="${KMS_KEY:-tp-mcp-oauth}"
GOOGLE_OAUTH_CLIENT_SECRET_NAME="${GOOGLE_OAUTH_CLIENT_SECRET_NAME:-google-oauth-client-secret}"

readonly PROJECT_NAME REGION SERVICE ARTIFACT_REPOSITORY RUNTIME_SERVICE_ACCOUNT BUILD_SERVICE_ACCOUNT
readonly FIRESTORE_DATABASE KMS_KEYRING KMS_KEY
readonly GOOGLE_OAUTH_CLIENT_SECRET_NAME
