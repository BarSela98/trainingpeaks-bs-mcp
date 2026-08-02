#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

guard_personal_gcloud_account
validate_deploy_settings
require_project_access

if [[ -x "${repo_root}/.venv/bin/python" ]]; then
  python_executable="${repo_root}/.venv/bin/python"
else
  require_command python3
  python_executable="$(command -v python3)"
fi

export GOOGLE_CLOUD_PROJECT="$PROJECT_ID"
exec "$python_executable" -m tp_mcp.cloud.admin \
  --project "$PROJECT_ID" \
  --database "$FIRESTORE_DATABASE" \
  --expected-account "$GCLOUD_ACCOUNT" \
  "$@"
