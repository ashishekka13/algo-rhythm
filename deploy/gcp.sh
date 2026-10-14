#!/usr/bin/env bash
# algo-rhythm on Google Cloud: one step at a time, each safe to run again.
#
#   deploy/gcp.sh check        what exists already (read-only)
#   deploy/gcp.sh apis         enable the APIs (free)
#   deploy/gcp.sh registry     Artifact Registry repository for the images (storage billed, cents)
#   deploy/gcp.sh sql          Cloud SQL PostgreSQL 16 instance, database and user (billed: ~$10/month at db-f1-micro)
#   deploy/gcp.sh secrets      DATABASE_URL and GEMINI_API_KEY in Secret Manager (asks for the Gemini key; free tier)
#   deploy/gcp.sh account      service account for the server and jobs, and its roles (free)
#   deploy/gcp.sh cors         let browsers fetch audio from the bucket (free)
#   deploy/gcp.sh build        build both images in Cloud Build (billed per build minute, about 15 min)
#   deploy/gcp.sh server       deploy the web server on Cloud Run (billed per request time; scales to zero)
#   deploy/gcp.sh jobs         create or update the three Cloud Run Jobs (billed only while they run)
#   deploy/gcp.sh run-songs    analyse every original not yet in the library (25 tasks, 4 at a time; about 3 hours for 100 songs)
#   deploy/gcp.sh run-details  Gemini song details and search vectors
#   deploy/gcp.sh run-art      song images (about $0.04 each)
#
# Settings below; override any of them in the environment.
set -euo pipefail

PROJECT=${PROJECT:-song-mixer-511123}
REGION=${REGION:-asia-south1}
BUCKET=${BUCKET:-song-mixer-511123-algo-rhythm}
SOURCE_PREFIX=${SOURCE_PREFIX:-library}          # the originals: gs://$BUCKET/$SOURCE_PREFIX/...
LIBRARY_PREFIX=${LIBRARY_PREFIX:-web}            # what the browser plays: gs://$BUCKET/$LIBRARY_PREFIX/...
SQL_INSTANCE=${SQL_INSTANCE:-algo-rhythm}            # existing instance (shared with the Oct 10 app)
SQL_TIER=${SQL_TIER:-db-f1-micro}
DB_NAME=${DB_NAME:-algorhythm_web}           # its own database: the Oct 10 app keeps "algorhythm"
DB_USER=${DB_USER:-algorhythm_web}
REPO=${REPO:-algo-rhythm}
SERVICE=${SERVICE:-algo-rhythm-web}           # alongside the Oct 10 service "algo-rhythm"
SA_NAME=${SA_NAME:-algo-rhythm-run}
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
TAG=${TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)}
API_IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/server:$TAG"
JOBS_IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/jobs:$TAG"
CONN="$PROJECT:$REGION:$SQL_INSTANCE"
G="gcloud --project=$PROJECT"

say() { printf '\n== %s\n' "$*"; }

case "${1:-}" in
check)
  say "account"; gcloud config get-value account
  say "bucket"; $G storage buckets describe "gs://$BUCKET" --format="value(location,locationType)"
  say "Cloud SQL"; $G sql instances list --format="table(name,region,databaseVersion,settings.tier,state)" 2>/dev/null || true
  say "Cloud Run"; $G run services list --region="$REGION" 2>/dev/null || true
  say "secrets"; $G secrets list --format="value(name)" 2>/dev/null || true
  ;;
apis)
  $G services enable run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
    artifactregistry.googleapis.com cloudbuild.googleapis.com iamcredentials.googleapis.com aiplatform.googleapis.com
  ;;
registry)
  $G artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1 || \
    $G artifacts repositories create "$REPO" --repository-format=docker --location="$REGION" \
      --description="algo-rhythm images"
  ;;
sql)
  if ! $G sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
    $G sql instances create "$SQL_INSTANCE" --database-version=POSTGRES_16 --edition=ENTERPRISE --tier="$SQL_TIER" \
      --region="$REGION" --storage-size=10 --storage-auto-increase --backup-start-time=03:00
  fi
  $G sql databases describe "$DB_NAME" --instance="$SQL_INSTANCE" >/dev/null 2>&1 || \
    $G sql databases create "$DB_NAME" --instance="$SQL_INSTANCE"
  if ! $G secrets describe database-url >/dev/null 2>&1; then
    PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
    $G sql users create "$DB_USER" --instance="$SQL_INSTANCE" --password="$PASS"
    printf 'postgresql+psycopg://%s:%s@/%s?host=/cloudsql/%s' "$DB_USER" "$PASS" "$DB_NAME" "$CONN" | \
      $G secrets create database-url --data-file=- --replication-policy=automatic
    echo "the database password is only in the secret database-url"
  fi
  ;;
secrets)
  if ! $G secrets describe gemini-api-key >/dev/null 2>&1; then
    read -r -s -p "Gemini API key (Vertex AI express or AI Studio; not echoed): " KEY; echo
    printf '%s' "$KEY" | $G secrets create gemini-api-key --data-file=- --replication-policy=automatic
  fi
  ;;
account)
  $G iam service-accounts describe "$SA" >/dev/null 2>&1 || \
    $G iam service-accounts create "$SA_NAME" --display-name="algo-rhythm server and jobs"
  for role in roles/cloudsql.client roles/secretmanager.secretAccessor roles/logging.logWriter; do
    $G projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role="$role" --condition=None >/dev/null
  done
  gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" --role=roles/storage.objectAdmin >/dev/null
  # signed URLs: the account signs for itself through IAM
  $G iam service-accounts add-iam-policy-binding "$SA" --member="serviceAccount:$SA" \
    --role=roles/iam.serviceAccountTokenCreator >/dev/null
  echo "service account $SA ready"
  ;;
cors)
  # every address of the service (Cloud Run gives two), plus a laptop's
  URLS=$($G run services describe "$SERVICE" --region="$REGION" --format=json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
urls = json.loads(d["metadata"]["annotations"].get("run.googleapis.com/urls", "[]")) + [d["status"]["url"]]
print(json.dumps(sorted(set(urls)) + ["http://127.0.0.1:8765", "http://localhost:8765"]))' || echo '["*"]')
  ORIGINS="$URLS"
  URL="$URLS"
  printf '[{"origin": %s, "method": ["GET", "HEAD"], "responseHeader": ["Content-Type", "Content-Length", "Range"], "maxAgeSeconds": 3600}]' \
    "$ORIGINS" > /tmp/algo-cors.json
  gcloud storage buckets update "gs://$BUCKET" --cors-file=/tmp/algo-cors.json
  echo "browsers at ${URL:-any origin} may fetch audio from gs://$BUCKET"
  ;;
build)
  $G builds submit --config cloudbuild.yaml --substitutions="_API=$API_IMAGE,_JOBS=$JOBS_IMAGE" .
  ;;
server)
  # One instance at most for now: the WebSocket hub is in-process (algo_rhythm/hub.py says how to lift this).
  $G run deploy "$SERVICE" --image="$API_IMAGE" --region="$REGION" --service-account="$SA" \
    --add-cloudsql-instances="$CONN" --set-secrets="DATABASE_URL=database-url:latest,GEMINI_API_KEY=gemini-api-key:latest" \
    --set-env-vars="STORAGE_URL=gs://$BUCKET/$LIBRARY_PREFIX,GEMINI_BACKEND=vertex,MC_TTS=gemini,LOG_LEVEL=INFO" \
    --cpu=2 --memory=4Gi --concurrency=40 --min-instances=0 --max-instances=1 --timeout=3600 \
    --session-affinity --cpu-boost --allow-unauthenticated
  $G run services describe "$SERVICE" --region="$REGION" --format="value(status.url)"
  ;;
jobs)
  COMMON=(--image="$JOBS_IMAGE" --region="$REGION" --service-account="$SA" --set-cloudsql-instances="$CONN"
          --set-secrets="DATABASE_URL=database-url:latest,GEMINI_API_KEY=gemini-api-key:latest"
          --set-env-vars="SOURCE_URL=gs://$BUCKET/$SOURCE_PREFIX,STORAGE_URL=gs://$BUCKET/$LIBRARY_PREFIX,GEMINI_BACKEND=vertex")
  # songs: 25 tasks of about 4 songs (Demucs on 4 vCPU takes ~6 min a song; a task has 60 min), 4 at a time
  # (4 x 4 vCPU: the project's 20 vCPU per region leaves room for the server)
  for spec in "songs:25:4:4:8Gi:3600" "details:1:1:1:2Gi:1800" "art:1:1:1:2Gi:3600"; do
    IFS=: read -r name tasks parallel cpu mem timeout <<< "$spec"
    verb=create; $G run jobs describe "algo-rhythm-$name" --region="$REGION" >/dev/null 2>&1 && verb=update
    $G run jobs "$verb" "algo-rhythm-$name" "${COMMON[@]}" --args="$name" --tasks="$tasks" --parallelism="$parallel" \
      --cpu="$cpu" --memory="$mem" --task-timeout="${timeout}s" --max-retries=1
  done
  ;;
run-songs)   $G run jobs execute algo-rhythm-songs --region="$REGION" --wait ;;
run-details) $G run jobs execute algo-rhythm-details --region="$REGION" --wait ;;
run-art)     $G run jobs execute algo-rhythm-art --region="$REGION" --wait ;;
*)
  sed -n '2,20p' "$0"; exit 1 ;;
esac
