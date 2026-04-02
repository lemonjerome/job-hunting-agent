#!/usr/bin/env bash
# =============================================================================
# Google Cloud One-Time Setup for Job Hunting Agent
#
# Run this script ONCE from your local machine after creating a GCP project.
# Prerequisites:
#   - gcloud CLI installed and authenticated (gcloud auth login)
#   - A GCP project with billing enabled (free tier is sufficient)
#   - Your OAuth credentials JSON files for Gmail and GDrive
#
# Usage:
#   export GCP_PROJECT=your-project-id
#   export REGION=us-central1
#   bash scripts/gcloud_setup.sh
# =============================================================================

set -euo pipefail

: "${GCP_PROJECT:?Set GCP_PROJECT env var to your project ID}"
: "${REGION:=us-central1}"

REPO_NAME="job-agent"
PUBSUB_TOPIC="gmail-job-alerts"
ARTIFACT_REGISTRY_REPO="job-agent"

echo "=== Setting up Job Hunting Agent on GCP project: $GCP_PROJECT ==="
echo ""

# ---------------------------------------------------------------------------
# 1. Set default project
# ---------------------------------------------------------------------------
gcloud config set project "$GCP_PROJECT"

# ---------------------------------------------------------------------------
# 2. Enable required APIs
# ---------------------------------------------------------------------------
echo "--- Enabling APIs ---"
gcloud services enable \
  run.googleapis.com \
  cloudfunctions.googleapis.com \
  pubsub.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com \
  gmail.googleapis.com \
  drive.googleapis.com \
  sheets.googleapis.com

echo "APIs enabled."

# ---------------------------------------------------------------------------
# 3. Artifact Registry — Docker repository
# ---------------------------------------------------------------------------
echo "--- Creating Artifact Registry repository ---"
gcloud artifacts repositories create "$ARTIFACT_REGISTRY_REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="Job Hunting Agent Docker images" \
  2>/dev/null || echo "(already exists)"

# ---------------------------------------------------------------------------
# 4. Cloud Pub/Sub topic for Gmail push notifications
# ---------------------------------------------------------------------------
echo "--- Creating Pub/Sub topic: $PUBSUB_TOPIC ---"
gcloud pubsub topics create "$PUBSUB_TOPIC" 2>/dev/null || echo "(already exists)"

# Grant Gmail permission to publish to the topic
echo "--- Granting Gmail API publish permission ---"
gcloud pubsub topics add-iam-policy-binding "$PUBSUB_TOPIC" \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher"

# ---------------------------------------------------------------------------
# 5. Service account for Cloud Run + Cloud Function
# ---------------------------------------------------------------------------
SA_NAME="job-agent-runner"
SA_EMAIL="$SA_NAME@$GCP_PROJECT.iam.gserviceaccount.com"

echo "--- Creating service account: $SA_NAME ---"
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="Job Agent Runner" \
  2>/dev/null || echo "(already exists)"

# Grant necessary roles
for ROLE in \
  roles/run.invoker \
  roles/secretmanager.secretAccessor \
  roles/pubsub.subscriber \
  roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
    --member="serviceAccount:$SA_EMAIL" \
    --role="$ROLE" \
    --quiet
done
echo "Service account roles granted."

# ---------------------------------------------------------------------------
# 6. Store secrets in Secret Manager
# ---------------------------------------------------------------------------
echo ""
echo "--- Storing secrets ---"
echo "You will be prompted for each secret value."
echo ""

store_secret() {
  local NAME="$1"
  local PROMPT="$2"
  echo -n "$PROMPT: "
  read -rs VALUE
  echo ""
  if gcloud secrets describe "$NAME" --project="$GCP_PROJECT" &>/dev/null; then
    echo "$VALUE" | gcloud secrets versions add "$NAME" --data-file=-
    echo "  Updated: $NAME"
  else
    echo "$VALUE" | gcloud secrets create "$NAME" --data-file=-
    echo "  Created: $NAME"
  fi
}

store_secret "OLLAMA_BASE_URL"   "Ollama base URL"
store_secret "OLLAMA_API_KEY"    "Ollama API key"
store_secret "SELF_EMAIL"        "Your Gmail address (e.g. gabrielcatadmanramos@gmail.com)"
store_secret "RESUME_FILENAME"   "Resume filename in GDrive (e.g. RAMOS_Gabriel_C_Resume.pdf)"

echo ""
echo "--- Storing OAuth credential files ---"
echo "Path to your Gmail/GDrive OAuth credentials JSON (from Google Cloud Console):"
read -r CREDS_PATH
if [ -f "$CREDS_PATH" ]; then
  gcloud secrets create "google-oauth-credentials" \
    --data-file="$CREDS_PATH" 2>/dev/null || \
  gcloud secrets versions add "google-oauth-credentials" --data-file="$CREDS_PATH"
  echo "  Stored: google-oauth-credentials"
else
  echo "  WARNING: File not found. Store it manually later."
fi

echo ""
echo "--- Storing Gmail OAuth token ---"
echo "Path to your .gmail_token.json (run python scripts/setup_gmail_watch.py locally first):"
read -r TOKEN_PATH
if [ -f "$TOKEN_PATH" ]; then
  gcloud secrets create "gmail-oauth-token" \
    --data-file="$TOKEN_PATH" 2>/dev/null || \
  gcloud secrets versions add "gmail-oauth-token" --data-file="$TOKEN_PATH"
  echo "  Stored: gmail-oauth-token"
else
  echo "  WARNING: File not found. Store it manually after running setup_gmail_watch.py."
fi

# ---------------------------------------------------------------------------
# 7. Register Gmail Watch (requires local OAuth token from step above)
# ---------------------------------------------------------------------------
echo ""
echo "--- Registering Gmail Watch ---"
GCP_PROJECT="$GCP_PROJECT" python scripts/setup_gmail_watch.py || \
  echo "WARNING: Gmail Watch setup failed. Run 'python scripts/setup_gmail_watch.py' manually."

# ---------------------------------------------------------------------------
# 8. Cloud Scheduler — daily Gmail Watch renewal
# ---------------------------------------------------------------------------
echo ""
echo "--- Note: Cloud Scheduler job for Watch renewal will be created after Cloud Run deploy ---"
echo "After deploying, run:"
echo ""
echo "  gcloud scheduler jobs create http renew-gmail-watch \\"
echo "    --project=$GCP_PROJECT \\"
echo "    --schedule='0 0 * * *' \\"
echo "    --uri=\$CLOUD_RUN_URL/renew-watch \\"
echo "    --http-method=POST \\"
echo "    --oidc-service-account-email=$SA_EMAIL \\"
echo "    --time-zone='Asia/Manila' \\"
echo "    --location=$REGION"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "==================================================================="
echo "GCP setup complete."
echo ""
echo "Next steps:"
echo "  1. Push your code to GitHub main branch — CI/CD will build and deploy."
echo "  2. After first deploy, run the Cloud Scheduler command above."
echo "  3. Connect Pub/Sub to Cloud Function (done automatically in CI/CD)."
echo ""
echo "Image registry: $REGION-docker.pkg.dev/$GCP_PROJECT/$ARTIFACT_REGISTRY_REPO/job-agent"
echo "==================================================================="
