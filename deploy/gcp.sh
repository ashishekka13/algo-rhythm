#!/usr/bin/env bash
# algo-rhythm on Google Cloud: one step at a time, each safe to run again.
#
#   deploy/gcp.sh check        what exists already (read-only)
#   deploy/gcp.sh apis         enable the APIs (free)
#   deploy/gcp.sh registry     Artifact Registry repository for the images (storage billed, cents)
#   deploy/gcp.sh sql          Cloud SQL PostgreSQL 16 instance, database and user (billed: ~$10/month at db-f1-micro)
#   deploy/gcp.sh account      service account for the server and jobs, and its roles (free; Gemini runs as it)
#   deploy/gcp.sh cors         let browsers fetch audio from the bucket (free)
#   deploy/gcp.sh build        build both images in Cloud Build (billed per build minute, about 15 min)
#   deploy/gcp.sh server       deploy the web server on Cloud Run (billed per request time; scales to zero)
#   deploy/gcp.sh jobs         create or update the three Cloud Run Jobs (billed only while they run)
#   deploy/gcp.sh run-songs    analyse every original not yet in the library (25 tasks, 4 at a time; about 3 hours for 100 songs)
#   deploy/gcp.sh run-details  Gemini song details and search vectors
#   deploy/gcp.sh run-art      song images (about $0.04 each)
#   deploy/gcp.sh run-trivia   trivia questions for songs without them (Gemini, ~10 requests per 100 songs)
#
# Staging: STAGE=staging deploy/gcp.sh <step> works on the staging service (algorythm-web-staging) and its own database
# (algorhythm_web_staging, secret database-url-staging) on the same instance; it shares the bucket's audio.
#   STAGE=staging deploy/gcp.sh staging-db   create staging's database and copy the library and host accounts into it
#                                            from production (tools/clone_db.py; run again to refresh; needs
#                                            cloud-sql-proxy and psql on this machine)
#   STAGE=staging deploy/gcp.sh server       deploy the server to staging (build first; TAG = the commit)
#   STAGE=staging deploy/gcp.sh cors         add staging's addresses to the bucket's CORS (keeps the others)
# The jobs (songs, details, art, trivia) are production's; staging has only its own trivia job:
#   STAGE=staging deploy/gcp.sh jobs && STAGE=staging deploy/gcp.sh run-trivia
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
STAGE=${STAGE:-production}                     # production | staging
if [ "$STAGE" = staging ]; then
  DB_NAME=${DB_NAME:-algorhythm_web_staging}
  SERVICE=${SERVICE:-algorythm-web-staging}
  DB_SECRET=${DB_SECRET:-database-url-staging}
elif [ "$STAGE" != production ]; then
  echo "STAGE must be production or staging, not $STAGE" >&2; exit 1
fi
DB_NAME=${DB_NAME:-algorhythm_web}           # its own database: the Oct 10 app keeps "algorhythm"
DB_SECRET=${DB_SECRET:-database-url}
DB_USER=${DB_USER:-algorhythm_web}
REPO=${REPO:-algo-rhythm}
SERVICE=${SERVICE:-algo-rhythm-web}           # alongside the Oct 10 service "algo-rhythm"
SA_NAME=${SA_NAME:-algo-rhythm-run}
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
AI_SA=${AI_SA:-gemini-algorythm@$PROJECT.iam.gserviceaccount.com}  # every Gemini / Vertex AI call runs as this account
TAG=${TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)}
API_IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/server:$TAG"
JOBS_IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/jobs:$TAG"
CONN="$PROJECT:$REGION:$SQL_INSTANCE"
# The server's size. gen2: a microVM, faster for rubberband's processes and files than gen1's sandbox. 4 vCPU: a join's
# stretches and the parts of a song it tries run side by side. CONCURRENCY: open screens are idle WebSockets.
# MAX_INSTANCES: parties share updates across instances (LISTEN/NOTIFY), but each instance holds up to
# DB_POOL_SIZE + DB_POOL_OVERFLOW + 1 database connections: db-f1-micro allows 25 in all (production, staging and the
# Oct 10 app share it), so keep 1 until the database tier is larger (db-g1-small: 50; db-custom-1-3840: 100).
# Staging is for trying changes, not for load: one small instance. Production is sized for the parties.
if [ "$STAGE" = staging ]; then
  CPU=${CPU:-2}; MAX_INSTANCES=${MAX_INSTANCES:-1}
fi
CPU=${CPU:-4}
MEMORY=${MEMORY:-4Gi}
CONCURRENCY=${CONCURRENCY:-250}
MAX_INSTANCES=${MAX_INSTANCES:-1}
DB_POOL_SIZE=${DB_POOL_SIZE:-3}
DB_POOL_OVERFLOW=${DB_POOL_OVERFLOW:-5}
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
  # Gemini on Vertex AI (no API key): $SA impersonates $AI_SA, which alone holds roles/aiplatform.user
  $G projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$AI_SA" --role=roles/aiplatform.user \
    --condition=None >/dev/null
  $G iam service-accounts add-iam-policy-binding "$AI_SA" --member="serviceAccount:$SA" \
    --role=roles/iam.serviceAccountTokenCreator >/dev/null
  echo "service account $SA ready (AI as $AI_SA)"
  ;;
cors)
  # every address of the service (Cloud Run gives two) and a laptop's, added to the origins the bucket already allows
  # (production, staging and the Oct 10 app share the bucket: replacing the list would lock the others out)
  SVC=$($G run services describe "$SERVICE" --region="$REGION" --format=json)
  OLD=$(gcloud storage buckets describe "gs://$BUCKET" --format=json)
  ORIGINS=$(python3 -c '
import json, sys
d, b = json.loads(sys.argv[1]), json.loads(sys.argv[2])
urls = json.loads(d["metadata"]["annotations"].get("run.googleapis.com/urls", "[]")) + [d["status"]["url"]]
have = [o for rule in (b.get("cors_config") or []) for o in rule.get("origin", [])]
print(json.dumps(sorted(set(have + urls + ["http://127.0.0.1:8765", "http://localhost:8765"]))))' "$SVC" "$OLD")
  printf '[{"origin": %s, "method": ["GET", "HEAD"], "responseHeader": ["Content-Type", "Content-Length", "Range"], "maxAgeSeconds": 3600}]' \
    "$ORIGINS" > /tmp/algo-cors.json
  gcloud storage buckets update "gs://$BUCKET" --cors-file=/tmp/algo-cors.json
  echo "browsers at these origins may fetch audio from gs://$BUCKET: $ORIGINS"
  ;;
build)
  $G builds submit --config cloudbuild.yaml --substitutions="_API=$API_IMAGE,_JOBS=$JOBS_IMAGE" .
  ;;
server)
  # Sized by CPU, MEMORY, CONCURRENCY and MAX_INSTANCES above.
  $G run deploy "$SERVICE" --image="$API_IMAGE" --region="$REGION" --service-account="$SA" \
    --add-cloudsql-instances="$CONN" --set-secrets="DATABASE_URL=$DB_SECRET:latest" \
    --set-env-vars="STORAGE_URL=gs://$BUCKET/$LIBRARY_PREFIX,GEMINI_BACKEND=vertex,GEMINI_AUTH=adc,GEMINI_SERVICE_ACCOUNT=$AI_SA,GOOGLE_CLOUD_PROJECT=$PROJECT,MC_TTS=gemini,LOG_LEVEL=INFO,DB_POOL_SIZE=$DB_POOL_SIZE,DB_POOL_OVERFLOW=$DB_POOL_OVERFLOW,LOCAL_CACHE_MB=1000,RENDER_SLOTS=2" \
    --cpu="$CPU" --memory="$MEMORY" --concurrency="$CONCURRENCY" --min-instances=0 --max-instances="$MAX_INSTANCES" \
    --execution-environment=gen2 --timeout=3600 --session-affinity --cpu-boost --allow-unauthenticated
  $G run services describe "$SERVICE" --region="$REGION" --format="value(status.url)"
  ;;
staging-db)
  [ "$STAGE" = staging ] || { echo "run as STAGE=staging deploy/gcp.sh staging-db" >&2; exit 1; }
  PORT=54329
  cloud-sql-proxy --token "$(gcloud auth print-access-token)" --port $PORT "$CONN" > /tmp/algo-proxy.log 2>&1 &
  PROXY=$!
  trap 'kill $PROXY 2>/dev/null' EXIT
  for _ in $(seq 1 30); do nc -z 127.0.0.1 $PORT 2>/dev/null && break; sleep 1; done
  PROD_URL=$($G secrets versions access latest --secret=database-url)
  # the secret's URL reaches the instance through Cloud Run's socket; here it goes through the proxy instead
  via_proxy() { python3 -c 'import re, sys; print(re.sub(r"@/[^?]+\?host=.*$", "@127.0.0.1:" + sys.argv[2] + "/" + sys.argv[3], sys.argv[1]))' "$PROD_URL" $PORT "$1"; }
  PSQL_URL=$(via_proxy postgres | sed 's#^postgresql+psycopg://#postgresql://#')
  if ! psql "$PSQL_URL" -tAc "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'" | grep -q 1; then
    psql "$PSQL_URL" -qc "CREATE DATABASE $DB_NAME"      # owned by $DB_USER, who then creates its tables
  fi
  if ! $G secrets describe "$DB_SECRET" >/dev/null 2>&1; then
    python3 -c 'import re, sys; print(re.sub(r"@/[^?]+\?", "@/" + sys.argv[2] + "?", sys.argv[1]), end="")' "$PROD_URL" "$DB_NAME" | \
      $G secrets create "$DB_SECRET" --data-file=- --replication-policy=automatic
  fi
  "${PYTHON:-.venv/bin/python}" -m tools.clone_db "$(via_proxy "${PROD_DB:-algorhythm_web}")" "$(via_proxy "$DB_NAME")"
  echo "staging database $DB_NAME is a copy of production's library and host accounts (secret $DB_SECRET)"
  ;;
jobs)
  # production: songs, details, art and trivia. Staging: only its own trivia job (algo-rhythm-trivia-staging), so its
  # database (a copy of the library) gets questions too; the songs and their analysis come from production.
  COMMON=(--image="$JOBS_IMAGE" --region="$REGION" --service-account="$SA" --set-cloudsql-instances="$CONN"
          --set-secrets="DATABASE_URL=$DB_SECRET:latest"
          --set-env-vars="SOURCE_URL=gs://$BUCKET/$SOURCE_PREFIX,STORAGE_URL=gs://$BUCKET/$LIBRARY_PREFIX,GEMINI_BACKEND=vertex,GEMINI_AUTH=adc,GEMINI_SERVICE_ACCOUNT=$AI_SA,GOOGLE_CLOUD_PROJECT=$PROJECT")
  # songs: 25 tasks of about 4 songs (Demucs on 4 vCPU takes ~6 min a song; a task has 60 min), 4 at a time
  # (4 x 4 vCPU: the project's 20 vCPU per region leaves room for the server)
  SPECS=("songs:25:4:4:8Gi:3600" "details:1:1:1:2Gi:1800" "art:1:1:1:2Gi:3600" "trivia:1:1:1:2Gi:1800")
  SUFFIX=""
  if [ "$STAGE" = staging ]; then SPECS=("trivia:1:1:1:2Gi:1800"); SUFFIX="-staging"; fi
  for spec in "${SPECS[@]}"; do
    IFS=: read -r name tasks parallel cpu mem timeout <<< "$spec"
    verb=create; $G run jobs describe "algo-rhythm-$name$SUFFIX" --region="$REGION" >/dev/null 2>&1 && verb=update
    $G run jobs "$verb" "algo-rhythm-$name$SUFFIX" "${COMMON[@]}" --args="$name" --tasks="$tasks" --parallelism="$parallel" \
      --cpu="$cpu" --memory="$mem" --task-timeout="${timeout}s" --max-retries=1
  done
  ;;
run-songs)   $G run jobs execute algo-rhythm-songs --region="$REGION" --wait ;;
run-details) $G run jobs execute algo-rhythm-details --region="$REGION" --wait ;;
run-art)     $G run jobs execute algo-rhythm-art --region="$REGION" --wait ;;
run-trivia)  $G run jobs execute "algo-rhythm-trivia$([ "$STAGE" = staging ] && echo -staging)" --region="$REGION" --wait ;;
*)
  sed -n '2,20p' "$0"; exit 1 ;;
esac
