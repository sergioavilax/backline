# One log group for all three containers, separated by stream prefix (api / ui /
# eval). Seven-day retention because the artifact is the repo, not the logs — and
# an infinite-retention group is a small forever-cost left behind after teardown.
#
# The eval task's rendered summary table prints here. That screenshot — an eval
# suite grading agents, in CloudWatch Logs — is one of the deliverables of §A5.2.

resource "aws_cloudwatch_log_group" "backline" {
  name              = "/ecs/backline"
  retention_in_days = 7

  tags = { Name = "backline-logs" }
}
