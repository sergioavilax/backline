#!/usr/bin/env bash
# Phase A5.2 — run the 133-question suite as a one-off Fargate task.
#
# The eval runner talks straight to Postgres and Anthropic; it does not go through
# the HTTP API (V6). So "run the suite on AWS" is a run-task against the same image
# the API serves from, with a command override — not a request to a service.
#
#   deploy/aws/scripts/run_eval_task.sh                          # fresh run
#   deploy/aws/scripts/run_eval_task.sh --resume <run-id> --retry-errors
#
# The second form is the A5.3 heal loop. Mid-run 429/529s from the provider are
# quarantined as infra errors and the gate refuses them by design (D-032); resuming
# retries only those, and never touches a legitimately-scored row.
#
set -euo pipefail

TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tf() { terraform -chdir="$TF_DIR" output -raw "$1"; }

REGION="$(tf region)"
NETWORK="$(tf eval_run_task_network_config)"

# Base command must stay in sync with the task definition's override (ecs.tf).
COMMAND=(uv run --no-sync python -m evals run --suite core --model claude-sonnet-5 --budget 20.00 --gate)

if [[ $# -gt 0 ]]; then
  echo "==> appending resume arguments: $*"
  COMMAND+=("$@")
fi

# Render the command array as the JSON list the containerOverrides schema wants.
COMMAND_JSON="$(printf '%s\n' "${COMMAND[@]}" | jq -R . | jq -s -c .)"

OVERRIDES="$(jq -n -c --argjson cmd "$COMMAND_JSON" \
  '{containerOverrides: [{name: "eval", command: $cmd}]}')"

echo "==> cluster   : backline"
echo "==> task def  : backline-eval"
echo "==> command   : ${COMMAND[*]}"
echo

TASK_ARN="$(aws ecs run-task \
  --cluster backline \
  --launch-type FARGATE \
  --task-definition backline-eval \
  --region "$REGION" \
  --network-configuration "$NETWORK" \
  --overrides "$OVERRIDES" \
  --query 'tasks[0].taskArn' \
  --output text)"

echo "==> task: $TASK_ARN"
echo
echo "Follow it:"
echo "  aws logs tail /ecs/backline --follow --log-stream-name-prefix eval --region $REGION"
echo
echo "The task exits with the gate's exit code, visible on the STOPPED entry:"
echo "  aws ecs describe-tasks --cluster backline --tasks $TASK_ARN --region $REGION \\"
echo "    --query 'tasks[0].{lastStatus:lastStatus,exit:containers[0].exitCode,reason:stoppedReason}'"
echo
echo "When it finishes, pull the artifacts out of RDS (Fargate disk is gone, RDS is not):"
echo "  deploy/aws/scripts/fetch_summary.sh"
