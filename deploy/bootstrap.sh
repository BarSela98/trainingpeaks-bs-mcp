#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

guard_personal_gcloud_account
validate_deploy_settings
require_command grep
require_command git

if [[ "${ALLOW_DIRTY:-0}" != "1" ]] && [[ -n "$(git -C "$repo_root" status --porcelain)" ]]; then
  die "The repository has uncommitted changes. Commit them first or explicitly set ALLOW_DIRTY=1."
fi

create_project() {
  local base_id="$PROJECT_ID"
  local candidate creation_output suffix

  if gcloud projects describe "$base_id" --format='value(projectId)' >/dev/null 2>&1; then
    printf 'Using existing project: %s\n' "$base_id"
    return
  fi

  [[ "${CREATE_PROJECT:-0}" == "1" ]] || die \
    "Project ${base_id} is unavailable. Set CREATE_PROJECT=1 to create it (and try numeric suffixes)."

  for suffix in "" {1..99}; do
    candidate="${base_id}${suffix}"
    printf 'Trying project ID: %s\n' "$candidate"
    if creation_output="$(gcloud projects create "$candidate" --name="$PROJECT_NAME" --quiet 2>&1)"; then
      PROJECT_ID="$candidate"
      printf 'Created project: %s\n' "$PROJECT_ID"
      return
    fi
    if ! grep -Eqi 'already (exists|in use)|not available|cannot be used' <<<"$creation_output"; then
      printf '%s\n' "$creation_output" >&2
      die "Project creation failed for a reason other than ID availability."
    fi
  done

  die "No available project ID found through ${base_id}99. Choose PROJECT_ID explicitly."
}

create_project

[[ -n "${BILLING_ACCOUNT_ID:-}" ]] || die \
  "Set BILLING_ACCOUNT_ID to the personal billing account that should own this project."

gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING_ACCOUNT_ID" --quiet
gcloud config set project "$PROJECT_ID" --quiet

gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  cloudkms.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  serviceusage.googleapis.com \
  storage.googleapis.com \
  --project="$PROJECT_ID"

if ! existing_service="$(gcloud run services list \
  --region="$REGION" \
  --filter="metadata.name=${SERVICE}" \
  --format='value(metadata.name)' \
  --project="$PROJECT_ID")"; then
  die "Could not verify that Cloud Run service ${SERVICE} is absent; refusing to bootstrap."
fi
[[ -z "$existing_service" ]] || die \
  "Cloud Run service ${SERVICE} already exists in ${PROJECT_ID}/${REGION}; bootstrap is a one-time operation."

if ! gcloud artifacts repositories describe "$ARTIFACT_REPOSITORY" \
  --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$ARTIFACT_REPOSITORY" \
    --repository-format=docker \
    --location="$REGION" \
    --description="TrainingPeaks MCP container images" \
    --immutable-tags \
    --project="$PROJECT_ID"
else
  gcloud artifacts repositories update "$ARTIFACT_REPOSITORY" \
    --location="$REGION" \
    --immutable-tags \
    --project="$PROJECT_ID" \
    --quiet >/dev/null
fi

build_bucket="$(build_bucket_name)"
builder_service_account_email="$(build_service_account_email)"
if ! gcloud storage buckets describe "gs://${build_bucket}" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${build_bucket}" \
    --location="$REGION" \
    --uniform-bucket-level-access \
    --public-access-prevention \
    --project="$PROJECT_ID"
fi

if ! gcloud iam service-accounts describe "$builder_service_account_email" \
  --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$BUILD_SERVICE_ACCOUNT" \
    --display-name="TrainingPeaks MCP image builder" \
    --project="$PROJECT_ID"
fi

project_number="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
cloud_build_service_agent="service-${project_number}@gcp-sa-cloudbuild.iam.gserviceaccount.com"
gcloud iam service-accounts add-iam-policy-binding "$builder_service_account_email" \
  --member="serviceAccount:${cloud_build_service_agent}" \
  --role=roles/iam.serviceAccountTokenCreator \
  --project="$PROJECT_ID" \
  --quiet >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$builder_service_account_email" \
  --member="user:${GCLOUD_ACCOUNT}" \
  --role=roles/iam.serviceAccountUser \
  --project="$PROJECT_ID" \
  --quiet >/dev/null
gcloud artifacts repositories add-iam-policy-binding "$ARTIFACT_REPOSITORY" \
  --location="$REGION" \
  --member="serviceAccount:${builder_service_account_email}" \
  --role=roles/artifactregistry.writer \
  --project="$PROJECT_ID" \
  --quiet >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${builder_service_account_email}" \
  --role=roles/logging.logWriter \
  --condition=None \
  --quiet >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://${build_bucket}" \
  --member="serviceAccount:${builder_service_account_email}" \
  --role=roles/storage.admin \
  --project="$PROJECT_ID" \
  --quiet >/dev/null

if ! firestore_databases="$(gcloud firestore databases list \
  --project="$PROJECT_ID" \
  --format='value(name)')"; then
  die "Could not list Firestore databases; refusing to create or modify one."
fi
firestore_database_resource="projects/${PROJECT_ID}/databases/${FIRESTORE_DATABASE}"
firestore_exists=0
while IFS= read -r database_name; do
  if [[ "$database_name" == "$firestore_database_resource" ]]; then
    firestore_exists=1
    break
  fi
done <<<"$firestore_databases"

if [[ "$firestore_exists" == "0" ]]; then
  gcloud firestore databases create \
    --database="$FIRESTORE_DATABASE" \
    --location="$REGION" \
    --type=firestore-native \
    --delete-protection \
    --project="$PROJECT_ID"
fi

if ! firestore_configuration="$(gcloud firestore databases describe \
  --database="$FIRESTORE_DATABASE" \
  --project="$PROJECT_ID" \
  --format='value(type,locationId,deleteProtectionState)')"; then
  die "Could not inspect Firestore database ${FIRESTORE_DATABASE}."
fi
IFS=$'\t' read -r firestore_type firestore_location firestore_delete_protection <<<"$firestore_configuration"
[[ "$firestore_type" == "FIRESTORE_NATIVE" ]] || die \
  "Firestore database ${FIRESTORE_DATABASE} has type ${firestore_type:-<unset>}; expected FIRESTORE_NATIVE."
[[ "$firestore_location" == "$REGION" ]] || die \
  "Firestore database ${FIRESTORE_DATABASE} is in ${firestore_location:-<unset>}; expected ${REGION}."
[[ "$firestore_delete_protection" == "DELETE_PROTECTION_ENABLED" ]] || die \
  "Firestore database ${FIRESTORE_DATABASE} does not have delete protection enabled."

# Firestore TTL deletion is asynchronous, while the application also checks
# every numeric expiry synchronously. These policies bound storage/cost for
# public OAuth and DCR records even when clients abandon flows.
ttl_collection_groups=(
  oauth_clients
  oauth_transactions
  oauth_consents
  oauth_codes
  oauth_grants
  oauth_access_tokens
  oauth_refresh_tokens
)
for collection_group in "${ttl_collection_groups[@]}"; do
  gcloud firestore fields ttls update expires_at_timestamp \
    --collection-group="$collection_group" \
    --database="$FIRESTORE_DATABASE" \
    --enable-ttl \
    --async \
    --project="$PROJECT_ID" \
    --quiet >/dev/null
done

if ! gcloud kms keyrings describe "$KMS_KEYRING" \
  --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud kms keyrings create "$KMS_KEYRING" --location="$REGION" --project="$PROJECT_ID"
fi

if ! gcloud kms keys describe "$KMS_KEY" --keyring="$KMS_KEYRING" \
  --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud kms keys create "$KMS_KEY" \
    --keyring="$KMS_KEYRING" \
    --location="$REGION" \
    --purpose=encryption \
    --project="$PROJECT_ID"
fi

runtime_service_account_email="${RUNTIME_SERVICE_ACCOUNT}@${PROJECT_ID}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "$runtime_service_account_email" \
  --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$RUNTIME_SERVICE_ACCOUNT" \
    --display-name="TrainingPeaks MCP runtime" \
    --project="$PROJECT_ID"
fi

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${runtime_service_account_email}" \
  --role=roles/datastore.user \
  --condition=None \
  --quiet >/dev/null

gcloud kms keys add-iam-policy-binding "$KMS_KEY" \
  --keyring="$KMS_KEYRING" \
  --location="$REGION" \
  --member="serviceAccount:${runtime_service_account_email}" \
  --role=roles/cloudkms.cryptoKeyEncrypterDecrypter \
  --condition=None \
  --project="$PROJECT_ID" \
  --quiet >/dev/null

source_revision="$(git -C "$repo_root" rev-parse --short=12 HEAD)"
build_timestamp="$(date -u +%Y%m%d%H%M%S)"
image_uri="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPOSITORY}/trainingpeaks-mcp:bootstrap-${source_revision}-${build_timestamp}"
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
  --set-env-vars="TP_MCP_BOOTSTRAP=1,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},TP_MCP_FIRESTORE_DATABASE=${FIRESTORE_DATABASE},TP_MCP_KMS_KEY=projects/${PROJECT_ID}/locations/${REGION}/keyRings/${KMS_KEYRING}/cryptoKeys/${KMS_KEY}"

service_url="$(gcloud run services describe "$SERVICE" \
  --region="$REGION" --project="$PROJECT_ID" --format='value(status.url)')"

printf '\nBootstrap complete.\n'
printf 'Project ID: %s\n' "$PROJECT_ID"
printf 'Service URL: %s\n' "$service_url"
printf 'Google OAuth redirect URI: %s/oauth/google/callback\n' "$service_url"
printf '\nFor later commands, export PROJECT_ID=%q\n' "$PROJECT_ID"
