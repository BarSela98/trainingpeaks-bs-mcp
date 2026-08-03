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
require_command curl
require_command grep
require_command python3

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
candidate_tag="candidate"

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
  --startup-probe="httpGet.path=/health,httpGet.port=8080,timeoutSeconds=3,periodSeconds=5,failureThreshold=12" \
  --liveness-probe="httpGet.path=/health,httpGet.port=8080,timeoutSeconds=3,periodSeconds=30,failureThreshold=3" \
  --allow-unauthenticated \
  --no-traffic \
  --tag="$candidate_tag" \
  --set-env-vars="TP_MCP_BOOTSTRAP=0,TP_MCP_BASE_URL=${service_url},GOOGLE_CLOUD_PROJECT=${PROJECT_ID},TP_MCP_FIRESTORE_DATABASE=${FIRESTORE_DATABASE},TP_MCP_KMS_KEY=${kms_key_resource},GOOGLE_OAUTH_CLIENT_ID=${GOOGLE_OAUTH_CLIENT_ID}" \
  --set-secrets="GOOGLE_OAUTH_CLIENT_SECRET=${GOOGLE_OAUTH_CLIENT_SECRET_NAME}:${oauth_secret_version}"

if ! candidate_details="$(gcloud run services list \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --flatten='status.traffic[]' \
  --filter="metadata.name=${SERVICE} AND status.traffic.tag=${candidate_tag}" \
  --format='value(status.traffic.revisionName,status.traffic.url)')"; then
  die "Could not inspect the zero-traffic candidate revision; production traffic was not changed."
fi
[[ "$candidate_details" != *$'\n'* ]] || die \
  "Cloud Run returned more than one ${candidate_tag} traffic tag; production traffic was not changed."
IFS=$'\t' read -r candidate_revision candidate_url <<<"$candidate_details"
[[ -n "$candidate_revision" ]] || die \
  "Could not determine the zero-traffic candidate revision; production traffic was not changed."
[[ "$candidate_url" == https://* ]] || die \
  "Could not determine the candidate HTTPS URL; production traffic was not changed."

curl_options=(
  --silent
  --show-error
  --retry 5
  --retry-all-errors
  --retry-delay 2
  --connect-timeout 10
  --max-time 30
)

expect_candidate_status() {
  local expected_status="$1"
  local label="$2"
  local url="$3"
  local actual_status

  if ! actual_status="$(curl "${curl_options[@]}" \
    --output /dev/null \
    --write-out '%{http_code}' \
    "$url")"; then
    die "Candidate ${label} request failed; production traffic was not changed."
  fi
  [[ "$actual_status" == "$expected_status" ]] || die \
    "Candidate ${label} returned HTTP ${actual_status}; expected ${expected_status}. Production traffic was not changed."
}

printf 'Smoke-testing candidate revision %s at %s\n' "$candidate_revision" "$candidate_url"
expect_candidate_status 200 "health endpoint" "${candidate_url}/health"
expect_candidate_status 200 "OAuth authorization metadata" \
  "${candidate_url}/.well-known/oauth-authorization-server"
expect_candidate_status 200 "OAuth protected-resource metadata" \
  "${candidate_url}/.well-known/oauth-protected-resource/mcp"

# Dynamic registration exercises Firestore writes and KMS encryption under
# the candidate revision's runtime service account. Loading that client during
# authorization then exercises Firestore reads and KMS decryption. The resulting
# smoke records contain no user credential and are removed by Firestore TTL.
# Disable any caller-provided shell tracing before the registration response,
# because that response contains a newly minted client secret.
set +x
if ! registration_response="$(curl "${curl_options[@]}" \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{"client_name":"deploy-smoke-test","redirect_uris":["http://127.0.0.1/callback"],"grant_types":["authorization_code"],"response_types":["code"],"token_endpoint_auth_method":"client_secret_post","scope":"trainingpeaks:read"}' \
  --write-out $'\n%{http_code}' \
  "${candidate_url}/register")"; then
  die "Candidate dynamic registration failed; production traffic was not changed."
fi
registration_status="${registration_response##*$'\n'}"
registration_body="${registration_response%$'\n'*}"
[[ "$registration_status" == "201" ]] || die \
  "Candidate dynamic registration returned HTTP ${registration_status}; expected 201. Production traffic was not changed."
if ! smoke_client_id="$(python3 -c \
  'import json, sys; value = json.load(sys.stdin).get("client_id"); print(value if isinstance(value, str) else "")' \
  <<<"$registration_body")"; then
  die "Could not parse the candidate registration response; production traffic was not changed."
fi
unset registration_body registration_response
[[ "$smoke_client_id" =~ ^[A-Za-z0-9._~-]{1,200}$ ]] || die \
  "Candidate registration returned an invalid client ID; production traffic was not changed."

if ! authorization_response="$(curl "${curl_options[@]}" \
  --get \
  --output /dev/null \
  --dump-header - \
  --write-out $'\n%{http_code}' \
  --data-urlencode 'response_type=code' \
  --data-urlencode "client_id=${smoke_client_id}" \
  --data-urlencode 'redirect_uri=http://127.0.0.1/callback' \
  --data-urlencode 'scope=trainingpeaks:read' \
  --data-urlencode 'state=deploy-smoke-test' \
  --data-urlencode 'code_challenge=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' \
  --data-urlencode 'code_challenge_method=S256' \
  --data-urlencode "resource=${service_url}/mcp" \
  "${candidate_url}/authorize")"; then
  die "Candidate authorization bootstrap failed; production traffic was not changed."
fi
unset smoke_client_id
authorization_status="${authorization_response##*$'\n'}"
authorization_headers="${authorization_response%$'\n'*}"
[[ "$authorization_status" == "302" ]] || die \
  "Candidate authorization bootstrap returned HTTP ${authorization_status}; expected 302. Production traffic was not changed."
grep -Eqi '^location:[[:space:]]*https://accounts\.google\.com/o/oauth2/v2/auth\?' \
  <<<"$authorization_headers" || die \
  "Candidate authorization did not redirect to Google; production traffic was not changed."
unset authorization_headers authorization_response

if ! mcp_response="$(curl "${curl_options[@]}" \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"deploy-smoke-test","version":"1"}}}' \
  --dump-header - \
  --output /dev/null \
  --write-out $'\n%{http_code}' \
  "${candidate_url}/mcp")"; then
  die "Candidate unauthenticated MCP request failed; production traffic was not changed."
fi
mcp_status="${mcp_response##*$'\n'}"
mcp_headers="${mcp_response%$'\n'*}"
[[ "$mcp_status" == "401" ]] || die \
  "Candidate unauthenticated MCP request returned HTTP ${mcp_status}; expected 401. Production traffic was not changed."
grep -Eqi '^www-authenticate:[[:space:]]*Bearer([[:space:]]|$)' <<<"$mcp_headers" || die \
  "Candidate MCP response did not include a Bearer challenge; production traffic was not changed."
grep -Eqi '^www-authenticate:.*resource_metadata=' <<<"$mcp_headers" || die \
  "Candidate MCP challenge did not advertise resource metadata; production traffic was not changed."

printf 'Candidate smoke tests passed; promoting revision %s.\n' "$candidate_revision"
gcloud run services update-traffic "$SERVICE" \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --to-revisions="${candidate_revision}=100" \
  --quiet

printf '\nDeployment complete.\n'
printf 'Revision: %s\n' "$candidate_revision"
printf 'MCP endpoint: %s/mcp\n' "$service_url"
printf 'Health check: %s/health\n' "$service_url"
printf 'OAuth redirect: %s/oauth/google/callback\n' "$service_url"
