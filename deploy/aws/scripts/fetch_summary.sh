#!/usr/bin/env bash
# Phase A5.4 — recover the eval artifacts from RDS.
#
# A Fargate task's disk vanishes when it stops, so the JSONL traces written inside
# the container are gone. The scores are not: the runner persists to app.eval_runs
# and app.eval_results, and updates eval_runs.summary (jsonb) at completion (V6).
# One query is the whole recovery story.
#
# Uses the compose db container's own v16 client tools rather than a host psql, so
# there is no client/server version skew and nothing to install.
#
#   deploy/aws/scripts/fetch_summary.sh
#
set -euo pipefail

TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$TF_DIR/../.." && pwd)"
OUT_DIR="$TF_DIR/evidence"

tf() { terraform -chdir="$TF_DIR" output -raw "$1"; }

RDS_URL="$(tf database_url)"

mkdir -p "$OUT_DIR"

echo "==> pulling the latest finished eval run summary from RDS"
(cd "$REPO_ROOT" && docker compose exec -T db psql "$RDS_URL" -t -A -c \
  "SELECT summary FROM app.eval_runs WHERE finished_at IS NOT NULL
   ORDER BY started_at DESC LIMIT 1") > "$OUT_DIR/aws-run-summary.json"

if [[ ! -s "$OUT_DIR/aws-run-summary.json" ]]; then
  echo "ERROR: no finished eval run found in app.eval_runs." >&2
  echo "       Either the task is still running, or it died before completion —" >&2
  echo "       check: aws logs tail /ecs/backline --log-stream-name-prefix eval" >&2
  exit 1
fi

echo "==> rendered report"
(cd "$REPO_ROOT" && uv run python -m evals report --summary "$OUT_DIR/aws-run-summary.json")

echo
echo "==> strict gate, on the record (§A5.5 — a FAIL here is reported, not re-rolled)"
(cd "$REPO_ROOT" && uv run python -m evals gate --summary "$OUT_DIR/aws-run-summary.json") || {
  echo
  echo "Gate returned non-zero. That is a documented possible outcome, not a reason"
  echo "to re-run: see §A5.5 and V10 for the known variance failure modes (T2"
  echo "flicker, small-n category swings). Report it with its reason."
}

echo
echo "==> archiving evidence to S3"
aws s3 cp "$OUT_DIR/" "s3://$(tf evidence_bucket)/evals/" --recursive --region "$(tf region)"

echo
echo "Wrote: $OUT_DIR/aws-run-summary.json"
