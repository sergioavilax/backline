# Cluster, three task definitions, two services. The third task definition has no
# service on purpose: the eval is a one-off `aws ecs run-task`, not a thing that
# should ever restart itself.

locals {
  # Shared by the API and the eval task — they are the same image running the same
  # code against the same database; only the entrypoint differs.
  #
  # EMBED_MODEL is pinned rather than defaulted because query-time search reads the
  # `embedding_model` recorded in rag.contract_chunks and must embed queries with
  # the same model. A mismatch does not error — it silently returns garbage
  # neighbours, which is the worst possible failure mode for a retrieval eval (V9).
  common_env = {
    DATA_DIR     = "/data"
    WORLD_SEED   = "20260805"
    RERANK       = "on"
    EMBED_MODEL  = "BAAI/bge-small-en-v1.5"
    RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # The image has no git binary, so the eval runner reads run attribution from
    # this variable and records NULL without it (V11). Every eval row written on
    # AWS is therefore traceable to the exact deployed commit.
    GITHUB_SHA = var.deployed_git_sha
  }

  # Belt and suspenders next to the --budget flag in the command override below.
  eval_env = merge(local.common_env, {
    EVAL_BUDGET_USD = "20.00"
  })

  common_secrets = [
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
    { name = "ANTHROPIC_API_KEY", valueFrom = aws_secretsmanager_secret.anthropic_api_key.arn },
  ]

  api_image = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"
  ui_image  = "${aws_ecr_repository.ui.repository_url}:${var.image_tag}"
}

resource "aws_ecs_cluster" "backline" {
  name = "backline"

  setting {
    name  = "containerInsights"
    value = "disabled"
  }

  tags = { Name = "backline-cluster" }
}

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
resource "aws_ecs_task_definition" "api" {
  family                   = "backline-api"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = local.api_image
      essential = true

      portMappings = [
        { containerPort = 8000, protocol = "tcp" }
      ]

      environment = [for k, v in local.common_env : { name = k, value = v }]
      secrets     = local.common_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.backline.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "api"
        }
      }
    }
  ])

  tags = { Name = "backline-api-task" }
}

# ---------------------------------------------------------------------------
# UI — no secrets and no environment at all. NEXT_PUBLIC_API_URL is baked into
# the bundle when the image is built (V5), which is why the UI image cannot be
# built until `terraform output alb_dns` exists. That ordering is the whole
# reason A4 comes after A2.
# ---------------------------------------------------------------------------
resource "aws_ecs_task_definition" "ui" {
  family                   = "backline-ui"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.ui_cpu
  memory                   = var.ui_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = "ui"
      image     = local.ui_image
      essential = true

      portMappings = [
        { containerPort = 3000, protocol = "tcp" }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.backline.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "ui"
        }
      }
    }
  ])

  tags = { Name = "backline-ui-task" }
}

# ---------------------------------------------------------------------------
# EVAL — the same image as the API with a command override. The eval runner talks
# straight to Postgres and Anthropic; it does not go through the HTTP API (V6), so
# this task needs no load balancer, no service, and no port. Results persist to
# app.eval_runs / app.eval_results in RDS, which is how the artifacts survive the
# Fargate task's ephemeral disk.
#
# `--gate` makes the container's exit code the gate's verdict, so the run's
# pass/fail is visible in the ECS console's STOPPED entry without reading a log.
# ---------------------------------------------------------------------------
resource "aws_ecs_task_definition" "eval" {
  family                   = "backline-eval"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.eval_cpu
  memory                   = var.eval_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = "eval"
      image     = local.api_image
      essential = true

      command = [
        "uv", "run", "--no-sync", "python", "-m", "evals", "run",
        "--suite", "core",
        "--model", "claude-sonnet-5",
        "--budget", "20.00",
        "--gate",
      ]

      environment = [for k, v in local.eval_env : { name = k, value = v }]
      secrets     = local.common_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.backline.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "eval"
        }
      }
    }
  ])

  tags = { Name = "backline-eval-task" }
}

# ---------------------------------------------------------------------------
# Services
#
# health_check_grace_period_seconds = 300 is not padding. The API image is ~4 GB
# and a cold ECR pull takes minutes; without the grace period the ALB health check
# starts failing while the image is still downloading, ECS kills the task as
# unhealthy, and the replacement starts the same pull from scratch — a crash loop
# that looks like an application bug and is purely a timing artifact.
# ---------------------------------------------------------------------------
resource "aws_ecs_service" "api" {
  name            = "api"
  cluster         = aws_ecs_cluster.backline.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = [aws_subnet.public_a.id, aws_subnet.public_b.id]
    security_groups  = [aws_security_group.svc.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  health_check_grace_period_seconds = 300

  depends_on = [aws_lb_listener.api]

  tags = { Name = "backline-api-service" }
}

resource "aws_ecs_service" "ui" {
  name            = "ui"
  cluster         = aws_ecs_cluster.backline.id
  task_definition = aws_ecs_task_definition.ui.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = [aws_subnet.public_a.id, aws_subnet.public_b.id]
    security_groups  = [aws_security_group.svc.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.ui.arn
    container_name   = "ui"
    container_port   = 3000
  }

  health_check_grace_period_seconds = 300

  depends_on = [aws_lb_listener.ui]

  tags = { Name = "backline-ui-service" }
}
