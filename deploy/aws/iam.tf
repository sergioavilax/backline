# Two roles, and the interesting one is the empty one.

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ---------------------------------------------------------------------------
# Execution role — used by the ECS agent, not by application code. It pulls the
# image, writes the log stream, and reads the two secrets.
# ---------------------------------------------------------------------------
resource "aws_iam_role" "task_execution" {
  name               = "backline-task-execution-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = { Name = "backline-task-execution-role" }
}

resource "aws_iam_role_policy_attachment" "task_execution_managed" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Scoped to exactly the two secret ARNs — not `secretsmanager:*`, not a wildcard
# resource. If a third secret ever appears, this policy should have to change.
data "aws_iam_policy_document" "read_secrets" {
  statement {
    sid     = "ReadBacklineSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.database_url.arn,
      aws_secretsmanager_secret.anthropic_api_key.arn,
    ]
  }
}

resource "aws_iam_role_policy" "task_execution_secrets" {
  name   = "backline-read-secrets"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

# ---------------------------------------------------------------------------
# Task role — deliberately EMPTY. Zero policies attached, and it stays that way.
#
# Backline's agents talk to Postgres and to the Anthropic API. They call no AWS
# API at all. So the credentials the application code can actually reach should
# grant nothing, and an empty role is the cheapest possible proof of that: there
# is no policy to audit because there is no policy. If a future feature needs an
# AWS call, this role is where it becomes visible.
# ---------------------------------------------------------------------------
resource "aws_iam_role" "task" {
  name               = "backline-task-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = { Name = "backline-task-role" }
}
