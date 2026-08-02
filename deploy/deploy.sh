#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

guard_personal_gcloud_account
validate_deploy_settings
require_project_access
require_command git

[[ -n "${GOOGLE_OAUTH_CLIENT_ID:-}" ]] || die \
  "Set GOOGLE_OAUTH_CLIENT_ID to the Web client ID configured for this service."

if [[ "${ALLOW_DIRTY:-0}" != "1" ]] && [[ -n "$(git -C "$repo_root" status --porcelain)" ]]; then
  die "The repository has uncommitted changes. Commit them first or explicitly set ALLOW_DIRTY=1."
fi

gcloud secrets describe "$GOOGLE_OAUTH_CLIENT_SECRET_NAME" --project="$PROJECT_ID" >/dev/null 2>&1 || die \
  "Missing Secret Manager secret ${GOOGLE_OAUTH_CLIENT_SECRET_NAME}. Run deploy/configure-secrets.sh."
oauth_secret_version_resource="$(gcloud secrets versions list "$GOOGLE_OAUTH_CLIENT_SECRET_NAME" \
  --filter='state=ENABLED' \
  --sort-by='~createTime' \
  --limit=1 \
  --format='value(name)' \
  --project="$PROJECT_ID")"
oauth_secret_version="${oauth_secret_version_resource##*/}"
[[ "$oauth_secret_version" =~ ^[1-9][0-9]*$ ]] || die \
  "No enabled numeric version exists for ${GOOGLE_OAUTH_CLIENT_SECRET_NAME}."

builder_service_account_email="$(build_service_account_email)"
build_bucket="$(build_bucket_name)"
gcloud iam service-accounts describe "$builder_service_account_email" \
  --project="$PROJECT_ID" >/dev/null 2>&1 || die "Build service account is missing. Re-run deploy/bootstrap.sh."
gcloud storage buckets describe "gs://${build_bucket}" \
  --project="$PROJECT_ID" >/dev/null 2>&1 || die "Regional build bucket is missing. Re-run deploy/bootstrap.sh."

service_url="$(gcloud run services describe "$SERVICE" \
  --region="$REGION" --project="$PROJECT_ID" --format='value(status.url)')"
[[ "$service_url" == https://* ]] || die "Could not determine the permanent HTTPS Cloud Run URL."

source_revision="$(git -C "$repo_root" rev-parse --short=12 HEAD)"
build_timestamp="$(date -u +%Y%m%d%H%M%S)"
image_tag="${IMAGE_TAG:-${source_revision}-${build_timestamp}}"
[[ "$image_tag" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || die "Invalid IMAGE_TAG: ${image_tag}"
image_uri="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPOSITORY}/trainingpeaks-mcp:${image_tag}"
runtime_service_account_email="${RUNTIME_SERVICE_ACCOUNT}@${PROJECT_ID}.iam.gserviceaccount.com"
kms_key_resource="projects/${PROJECT_ID}/locations/${REGION}/keyRings/${KMS_KEYRING}/cryptoKeys/${KMS_KEY}"

gcloud builds submit "$repo_root" \
  --tag="$image_uri" \
  --ignore-file="${repo_root}/.dockerignore" \
  --region="$REGION" \
  --service-account="projects/${PROJECT_ID}/serviceAccounts/${builder_service_account_email}" \
  --gcs-source-staging-dir="gs://${build_bucket}/source" \
  --gcs-log-dir="gs://${build_bucket}/logs" \
  --project="$PROJECT_ID"

gcloud run deploy "$SERVICE" \
  --image="$image_uri" \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --platform=managed \
  --service-account="$runtime_service_account_email" \
  --cpu=1 \
  --memory=512Mi \
  --concurrency=20 \
  --max-instances=3 \
  --min-instances=0 \
  --timeout=300s \
  --port=8080 \
  --startup-probe="httpGet.path=/healthz,httpGet.port=8080,timeoutSeconds=3,periodSeconds=5,failureThreshold=12" \
  --liveness-probe="httpGet.path=/healthz,httpGet.port=8080,timeoutSeconds=3,periodSeconds=30,failureThreshold=3" \
  --allow-unauthenticated \
  --set-env-vars="TP_MCP_BOOTSTRAP=0,TP_MCP_BASE_URL=${service_url},GOOGLE_CLOUD_PROJECT=${PROJECT_ID},TP_MCP_FIRESTORE_DATABASE=${FIRESTORE_DATABASE},TP_MCP_KMS_KEY=${kms_key_resource},GOOGLE_OAUTH_CLIENT_ID=${GOOGLE_OAUTH_CLIENT_ID}" \
  --set-secrets="GOOGLE_OAUTH_CLIENT_SECRET=${GOOGLE_OAUTH_CLIENT_SECRET_NAME}:${oauth_secret_version}"

printf '\nDeployment complete.\n'
printf 'MCP endpoint: %s/mcp\n' "$service_url"
printf 'Health check: %s/healthz\n' "$service_url"
printf 'OAuth redirect: %s/oauth/google/callback\n' "$service_url"
