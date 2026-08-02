# Deploy the remote MCP to Google Cloud Run

This deployment keeps local `tp-mcp serve` (stdio) unchanged and publishes the
multi-athlete Streamable HTTP service at `https://SERVICE_URL/mcp`. Cloud Run is
publicly invokable so MCP OAuth can start, while the application requires an
OAuth bearer token for MCP operations.

The remote service is deliberately stateless for TrainingPeaks authentication.
It never stores a TrainingPeaks cookie. Every authenticated request to `/mcp`
must carry the current `Production_tpAuth` value in the
`X-TrainingPeaks-Auth` header.

The scripts deliberately refuse an active `@ridewithvia.com` account. They also
require you to name the personal account in `GCLOUD_ACCOUNT`; this prevents an
old gcloud configuration from silently deploying into the wrong account.

## Prerequisites

- A personal Google account with permission to create projects and OAuth clients
- A billing account owned by, or explicitly available to, that personal account
- `gcloud` and `git`
- Docker is optional locally; Cloud Build builds the production image
- A clean, committed checkout of this repository

Authenticate explicitly. Replace the example address and billing account ID
with your own values; do not save client secrets in shell history or `.env`
files.

```bash
gcloud auth login your-personal-account@example.com
gcloud config set account your-personal-account@example.com
export GCLOUD_ACCOUNT=your-personal-account@example.com
export BILLING_ACCOUNT_ID=000000-000000-000000
```

Confirm the selected identity before continuing:

```bash
gcloud auth list
gcloud billing accounts list
```

## 1. Bootstrap the project and permanent service URL

The preferred project ID is `trainingpeaks-bs-mcp`. If it is globally
unavailable, the bootstrap script tries the shortest numeric suffix through
`99`. Project creation is opt-in because it is a billable external mutation.

```bash
export CREATE_PROJECT=1
./deploy/bootstrap.sh
```

The script performs these operations in `me-west1`:

- creates and links the project to the selected billing account;
- enables Cloud Run, Cloud Build, Artifact Registry, Firestore, Cloud KMS,
  Secret Manager, IAM, and Service Usage;
- creates a regional Docker repository and Firestore Native `(default)`
  database with delete protection;
- creates a private `me-west1` Cloud Storage bucket and a dedicated
  `trainingpeaks-mcp-builder` service account for regional Cloud Build source
  staging and logs;
- enables Firestore TTL policies for temporary OAuth and Dynamic Client
  Registration records (the service also enforces deadlines before cleanup);
- creates the `trainingpeaks-mcp` KMS keyring and `tp-mcp-oauth` encryption key;
- creates the `trainingpeaks-mcp-runtime` service account;
- grants only Firestore data access and OAuth-record encrypt/decrypt access to
  the runtime;
- grants the builder storage administration only on its dedicated build bucket and
  writer access on the Artifact Registry repository and Cloud Logging;
- builds and bootstrap-deploys a public Cloud Run service with 1 CPU, 512 MiB,
  concurrency 20, maximum 3 instances, minimum 0, and a 300-second timeout.

Cloud KMS protects OAuth-owned sensitive fields such as dynamically registered
client secrets and opaque authorization state. It is not used for a
TrainingPeaks cookie because the service never persists that cookie.

Copy the exact `PROJECT_ID` and permanent service URL printed at the end. If a
numeric suffix was selected, export it for every later command:

```bash
export PROJECT_ID=trainingpeaks-bs-mcp1
```

The bootstrap revision is intentionally not a usable MCP. It only reserves the
permanent Cloud Run URL needed to configure the Google OAuth redirect URI.

## 2. Configure Google OAuth

In the [Google Auth Platform console](https://console.cloud.google.com/auth/overview):

1. Select the project printed by the bootstrap script.
2. Configure an **External** app in **Testing** mode.
3. Request only `openid`, `email`, and `profile` identity scopes.
4. Add each invited athlete's Google account as a test user.
5. Create an OAuth **Web application** client.
6. Add exactly `SERVICE_URL/oauth/google/callback` as an authorized redirect URI.

Keep the client secret out of files in this repository. Export the client ID;
the ID is configuration, not a secret:

```bash
export GOOGLE_OAUTH_CLIENT_ID=123456789.apps.googleusercontent.com
```

## 3. Store application secrets

Run the secret setup script and paste the Google OAuth client secret into its
hidden prompt:

```bash
./deploy/configure-secrets.sh
```

For non-interactive administration, point
`GOOGLE_OAUTH_CLIENT_SECRET_FILE` at a protected file. The secret itself is
never accepted as a command-line argument or environment variable.

```bash
chmod 600 /safe/path/google-oauth-client-secret
GOOGLE_OAUTH_CLIENT_SECRET_FILE=/safe/path/google-oauth-client-secret \
  ./deploy/configure-secrets.sh
```

The script creates a global Secret Manager secret whose payload is replicated
only in `me-west1`, then grants the runtime service account accessor rights on
that secret only. OAuth authorization state and dynamically registered client
data are encrypted with Cloud KMS rather than a process-local signing secret.

## 4. Deploy the complete revision

Commit all application changes, then build and deploy the immutable commit-tagged
image:

```bash
./deploy/deploy.sh
```

The build uploads only `Dockerfile`, `pyproject.toml`, `uv.lock`, `README.md`,
and `src/**`; the default-deny build context excludes local credentials and all
other workspace files. The complete revision receives these application
settings:

- `TP_MCP_BASE_URL` — the permanent Cloud Run URL
- `GOOGLE_CLOUD_PROJECT` and `TP_MCP_FIRESTORE_DATABASE`
- `TP_MCP_KMS_KEY` — full resource name for the OAuth-record key
- `GOOGLE_OAUTH_CLIENT_ID`
- `GOOGLE_OAUTH_CLIENT_SECRET` from a numeric Secret Manager version pinned to
  that Cloud Run revision
- `TP_MCP_BOOTSTRAP=0`

`PORT` is assigned by Cloud Run. The image runs as Linux UID/GID `10001` and
starts `tp-mcp serve-http`, which listens on `0.0.0.0:$PORT`.

## 5. Invite athletes

The application admin commands use Application Default Credentials. Authenticate
ADC with the same personal account and set its quota project:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[cloud]"
gcloud auth application-default login "$GCLOUD_ACCOUNT" \
  --billing-project="$PROJECT_ID"
gcloud auth application-default set-quota-project "$PROJECT_ID"
```

Unset `GOOGLE_APPLICATION_CREDENTIALS` before administration. The wrapper
rejects service-account overrides, Via accounts, stale ADC for another Google
account, and a quota project that differs from `PROJECT_ID`.

Use the guarded wrapper so every write targets the selected personal project:

```bash
./deploy/admin.sh invite athlete@example.com
./deploy/admin.sh list
./deploy/admin.sh revoke athlete@example.com
```

`revoke` removes allowlist access and immediately blocks existing MCP OAuth
tokens. Google OAuth testing users and the Firestore application allowlist are
separate controls; an athlete must be present in both.

During connection, Google confirms the athlete's identity first. The service
then shows its own confirmation page listing the exact TrainingPeaks permissions
requested by the MCP client. In particular, `trainingpeaks:write` is identified
as permission to create, update, and delete TrainingPeaks data. No MCP
authorization code is issued until the athlete approves this short-lived,
single-use confirmation.

## 6. Configure the client to send TrainingPeaks auth per request

Add `SERVICE_URL/mcp` as a remote Streamable HTTP MCP. Configure the client's
secret/header facility to inject this header on **every** request to `/mcp`:

```text
X-TrainingPeaks-Auth: <raw Production_tpAuth cookie value>
```

The value is the cookie value only, without the `Production_tpAuth=` name. Keep
it in the client's OS keychain, secret store, or another non-exporting header
provider. Do not put the value in a committed MCP configuration file, shell
history, URL, query string, or ordinary environment file. If the client cannot
securely inject a custom header for every request, use the local stdio server
instead of the remote endpoint.

Anthropic's documented remote custom-connector setup currently accepts a
server URL and optional OAuth client credentials, but does not document a way
to attach an arbitrary per-request secret header. Consequently, the built-in
Claude.ai/Claude Desktop remote connector is not supported by this stateless
deployment today. Use a Streamable HTTP MCP client that has a secure custom
header facility, or keep Claude Desktop on local stdio. See [Anthropic's remote
connector documentation](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).

MCP OAuth is separate from TrainingPeaks authentication. After Google sign-in,
the client sends both headers on authenticated MCP requests:

```http
Authorization: Bearer <MCP OAuth access token>
X-TrainingPeaks-Auth: <Production_tpAuth cookie value>
```

The Cloud Run service uses `X-TrainingPeaks-Auth` only for that request. It must
not write the value to Firestore, Secret Manager, logs, OAuth records, or a
cross-request cache. On the first authenticated request, it validates the
cookie with TrainingPeaks and atomically binds the Google subject to the
returned non-secret TrainingPeaks athlete ID. It repeats validation on every
request and rejects a cookie for a different athlete. Firestore keeps the
identity binding, never the cookie. Refresh the client-side header when the
TrainingPeaks cookie expires. Local commands such as `tp-mcp auth` remain
available for the stdio deployment, where the credential is stored only on the
athlete's own computer.

## Validation and operations

The health endpoint is intentionally unauthenticated:

```bash
export SERVICE_URL=https://trainingpeaks-mcp-...run.app
curl --fail --show-error "${SERVICE_URL}/healthz"
```

The MCP endpoint must advertise OAuth when called without a bearer token:

```bash
curl --include "${SERVICE_URL}/mcp"
```

Expect HTTP `401` and a `WWW-Authenticate` challenge. Then validate
`initialize`, `tools/list`, `resources/list`, and a read-only tool with MCP
Inspector configured to send `X-TrainingPeaks-Auth` on every `/mcp` request.
Never paste the cookie into Inspector logs or an exported configuration.

Inspect the deployed revision and logs with the personal project pinned:

```bash
gcloud run services describe trainingpeaks-mcp \
  --region=me-west1 --project="$PROJECT_ID"
gcloud run services logs read trainingpeaks-mcp \
  --region=me-west1 --project="$PROJECT_ID" --limit=100
```

To roll back, list revisions and move all traffic to a previously validated
revision. This does not change Firestore data or revoke OAuth grants:

```bash
gcloud run revisions list --service=trainingpeaks-mcp \
  --region=me-west1 --project="$PROJECT_ID"
gcloud run services update-traffic trainingpeaks-mcp \
  --region=me-west1 --project="$PROJECT_ID" \
  --to-revisions=REVISION_NAME=100
```

Never place a TrainingPeaks cookie, OAuth client secret, refresh token, or
service-account key in source control, container layers, Cloud Build
substitutions, command arguments, URLs, or logs.
