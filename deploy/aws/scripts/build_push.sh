#!/usr/bin/env bash
# Phase A4 — tag and push both images to ECR.
#
# The API image must already exist locally as backline-aws:latest, built and gated
# in A1 (docker/aws.Dockerfile). This script does NOT rebuild it: the gated image
# and the pushed image should be the same bytes.
#
# The UI image IS built here, and cannot be built any earlier: NEXT_PUBLIC_API_URL
# is baked into the Next.js bundle at build time (V5), so the ALB's DNS name has to
# exist before the UI image can be correct.
#
#   deploy/aws/scripts/build_push.sh
#
set -euo pipefail

TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$TF_DIR/../.." && pwd)"

tf() { terraform -chdir="$TF_DIR" output -raw "$1"; }

REGION="$(tf region)"
REGISTRY="$(tf ecr_registry)"
ECR_API="$(tf ecr_api_repository_url)"
ECR_UI="$(tf ecr_ui_repository_url)"
API_URL="$(tf api_url)"

echo "==> registry : $REGISTRY"
echo "==> api repo : $ECR_API"
echo "==> ui repo  : $ECR_UI"
echo "==> UI will be built with NEXT_PUBLIC_API_URL=$API_URL"
echo

if ! docker image inspect backline-aws:latest >/dev/null 2>&1; then
  echo "ERROR: backline-aws:latest not found locally." >&2
  echo "       Build and gate it first (AWS_DEPLOY_PLAN §A1.2/§A1.3):" >&2
  echo "         docker build -f docker/aws.Dockerfile -t backline-aws:latest ." >&2
  exit 1
fi

echo "==> ECR login"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

echo "==> push API image (~4 GB on the first push — this is the slow part)"
docker tag backline-aws:latest "${ECR_API}:latest"
docker push "${ECR_API}:latest"

echo "==> build UI image with the ALB address baked in (V5)"
docker build \
  -f "$REPO_ROOT/ui/Dockerfile" \
  --build-arg "NEXT_PUBLIC_API_URL=${API_URL}" \
  -t backline-ui-aws:latest \
  "$REPO_ROOT/ui"

echo "==> push UI image"
docker tag backline-ui-aws:latest "${ECR_UI}:latest"
docker push "${ECR_UI}:latest"

echo "==> force new deployments so ECS picks up the freshly pushed tags"
aws ecs update-service --cluster backline --service api --force-new-deployment --region "$REGION" >/dev/null
aws ecs update-service --cluster backline --service ui  --force-new-deployment --region "$REGION" >/dev/null

echo
echo "Done. Watch them come up with:"
echo "  aws logs tail /ecs/backline --follow --region $REGION"
echo
echo "Then the smoke sequence (§A4), in order — each one gates the next:"
echo "  curl -s ${API_URL}/healthz"
echo "  curl -s ${API_URL}/readyz     # the SSL + SG + restore proof in one line"
echo "  open $(tf ui_url)"
