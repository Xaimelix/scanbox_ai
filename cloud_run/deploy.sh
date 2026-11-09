#!/usr/bin/env bash
# Run from the repository root. Creates billable GCP resources when executed.
set -euo pipefail
: "${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT}"
: "${SCANBOX_BUCKET:?Set a globally unique SCANBOX_BUCKET}"
: "${SCANBOX_CALLER:?Set IAM member, e.g. serviceAccount:backend@project.iam.gserviceaccount.com}"
CLOUD_RUN_REGION="${CLOUD_RUN_REGION:-europe-west4}"
SCANBOX_ENGINE="${SCANBOX_ENGINE:-mock}"
SCANBOX_BASE_IMAGE="${SCANBOX_BASE_IMAGE:-python:3.12-slim}"
if [[ "$SCANBOX_ENGINE" != mock && "$SCANBOX_ENGINE" != hunyuan-shape ]]; then
  echo 'SCANBOX_ENGINE must be mock or hunyuan-shape' >&2
  exit 2
fi
if [[ "$SCANBOX_ENGINE" == hunyuan-shape && "$SCANBOX_BASE_IMAGE" == python:3.12-slim ]]; then
  echo 'Set SCANBOX_BASE_IMAGE to a tested ML image with hy3dshape, CUDA PyTorch and weights' >&2
  exit 2
fi
case "$CLOUD_RUN_REGION" in
  europe-west1|europe-west4|us-central1|us-east4|asia-southeast1) ;;
  *) echo 'Select a documented generally available L4 region' >&2; exit 2 ;;
esac
command -v gcloud >/dev/null
export GOOGLE_CLOUD_PROJECT CLOUD_RUN_REGION SCANBOX_BUCKET SCANBOX_ENGINE
SCANBOX_WORKER_SA="scanbox-gpu@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"
SCANBOX_API_SA="scanbox-control@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"
image_root="${CLOUD_RUN_REGION}-docker.pkg.dev/${GOOGLE_CLOUD_PROJECT}/scanbox"
image_tag="$(date -u +%Y%m%d%H%M%S)"
api_image="${image_root}/control:${image_tag}"
worker_image="${image_root}/worker:${image_tag}"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com storage.googleapis.com iam.googleapis.com \
  --project "$GOOGLE_CLOUD_PROJECT"
if ! gcloud artifacts repositories describe scanbox --location "$CLOUD_RUN_REGION" \
  --project "$GOOGLE_CLOUD_PROJECT" >/dev/null 2>&1; then
  gcloud artifacts repositories create scanbox --repository-format docker \
    --location "$CLOUD_RUN_REGION" --project "$GOOGLE_CLOUD_PROJECT"
fi
if ! gcloud storage buckets describe "gs://${SCANBOX_BUCKET}" \
  --project "$GOOGLE_CLOUD_PROJECT" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${SCANBOX_BUCKET}" --location "$CLOUD_RUN_REGION" \
    --uniform-bucket-level-access --public-access-prevention --project "$GOOGLE_CLOUD_PROJECT"
fi
for identity in scanbox-gpu scanbox-control; do
  if ! gcloud iam service-accounts describe "${identity}@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com" \
    --project "$GOOGLE_CLOUD_PROJECT" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$identity" --project "$GOOGLE_CLOUD_PROJECT"
  fi
done
role_file="$(mktemp)"
env_file="$(mktemp)"
trap 'rm -f "$role_file" "$env_file"' EXIT
cat > "$role_file" <<'JSON'
{
  "title": "ScanBox GPU job controller",
  "description": "Create, observe, cancel and remove ScanBox jobs",
  "stage": "GA",
  "includedPermissions": [
    "run.jobs.create", "run.jobs.get", "run.jobs.list", "run.jobs.delete", "run.jobs.run",
    "run.executions.get", "run.executions.cancel", "run.operations.get"
  ]
}
JSON
if gcloud iam roles describe scanboxJobController --project "$GOOGLE_CLOUD_PROJECT" >/dev/null 2>&1; then
  gcloud iam roles update scanboxJobController --file "$role_file" --project "$GOOGLE_CLOUD_PROJECT"
else
  gcloud iam roles create scanboxJobController --file "$role_file" --project "$GOOGLE_CLOUD_PROJECT"
fi
gcloud projects add-iam-policy-binding "$GOOGLE_CLOUD_PROJECT" \
  --member "serviceAccount:${SCANBOX_API_SA}" \
  --role "projects/${GOOGLE_CLOUD_PROJECT}/roles/scanboxJobController" --condition None >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$SCANBOX_WORKER_SA" \
  --member "serviceAccount:${SCANBOX_API_SA}" --role roles/iam.serviceAccountUser \
  --project "$GOOGLE_CLOUD_PROJECT" >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://${SCANBOX_BUCKET}" \
  --member "serviceAccount:${SCANBOX_WORKER_SA}" --role roles/storage.objectViewer \
  --condition "title=scanbox-inputs,expression=resource.name.startsWith('projects/_/buckets/${SCANBOX_BUCKET}/objects/inputs/')" >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://${SCANBOX_BUCKET}" \
  --member "serviceAccount:${SCANBOX_WORKER_SA}" --role roles/storage.objectCreator \
  --condition "title=scanbox-outputs,expression=resource.name.startsWith('projects/_/buckets/${SCANBOX_BUCKET}/objects/outputs/')" >/dev/null
gcloud builds submit . --config cloud_run/cloudbuild.worker.yaml \
  --substitutions "_IMAGE=${worker_image},_BASE_IMAGE=${SCANBOX_BASE_IMAGE}" \
  --project "$GOOGLE_CLOUD_PROJECT"
gcloud builds submit . --config cloud_run/cloudbuild.api.yaml \
  --substitutions "_IMAGE=${api_image}" --project "$GOOGLE_CLOUD_PROJECT"
# Resolve worker to its immutable digest: retries must use exactly the same image.
worker_digest="$(gcloud artifacts docker images describe "$worker_image" \
  --format 'value(image_summary.digest)' --project "$GOOGLE_CLOUD_PROJECT")"
test -n "$worker_digest"
SCANBOX_WORKER_IMAGE="${image_root}/worker@${worker_digest}"
export SCANBOX_WORKER_IMAGE SCANBOX_WORKER_SA
python3 - "$env_file" <<'PY'
import json, os, sys
keys = ("GOOGLE_CLOUD_PROJECT", "CLOUD_RUN_REGION", "SCANBOX_BUCKET",
        "SCANBOX_WORKER_IMAGE", "SCANBOX_WORKER_SA", "SCANBOX_ENGINE")
with open(sys.argv[1], "w") as stream:
    json.dump({key: os.environ[key] for key in keys}, stream)
PY
gcloud run deploy scanbox-control --image "$api_image" --region "$CLOUD_RUN_REGION" \
  --service-account "$SCANBOX_API_SA" --no-allow-unauthenticated \
  --cpu 1 --memory 512Mi --min-instances 0 --max-instances 2 --timeout 60 \
  --env-vars-file "$env_file" --project "$GOOGLE_CLOUD_PROJECT"
gcloud run services add-iam-policy-binding scanbox-control --region "$CLOUD_RUN_REGION" \
  --member "$SCANBOX_CALLER" --role roles/run.invoker --project "$GOOGLE_CLOUD_PROJECT"
gcloud run services describe scanbox-control --region "$CLOUD_RUN_REGION" \
  --format 'value(status.url)' --project "$GOOGLE_CLOUD_PROJECT"
