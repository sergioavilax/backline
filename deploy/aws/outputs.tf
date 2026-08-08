# Everything the runbook needs, so that A3–A5 are copy-paste rather than
# console-archaeology. The scripts under scripts/ read these with
# `terraform -chdir=deploy/aws output -raw <name>`.

output "region" {
  description = "Region everything lives in. The scripts read this rather than re-deriving it."
  value       = var.region
}

output "alb_dns" {
  description = "ALB DNS name. Reachable only from var.home_cidr."
  value       = aws_lb.backline.dns_name
}

output "ui_url" {
  description = "UI in a browser (port 80 listener)."
  value       = "http://${aws_lb.backline.dns_name}"
}

output "api_url" {
  description = "API base URL (port 8000 listener). This is the value NEXT_PUBLIC_API_URL must be baked with when the UI image is built in A4 (V5)."
  value       = "http://${aws_lb.backline.dns_name}:8000"
}

output "rds_address" {
  description = "RDS hostname — .address, without a port. The A3 restore builds its DSN from this."
  value       = aws_db_instance.backline.address
}

output "ecr_api_repository_url" {
  description = "ECR repo for the API/eval image (docker/aws.Dockerfile, built and gated in A1)."
  value       = aws_ecr_repository.api.repository_url
}

output "ecr_ui_repository_url" {
  description = "ECR repo for the UI image (built in A4, after alb_dns exists)."
  value       = aws_ecr_repository.ui.repository_url
}

output "ecr_registry" {
  description = "Registry host for `docker login`."
  value       = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
}

output "evidence_bucket" {
  description = "S3 bucket for ALB access logs, the A3 dump, and A5 eval artifacts."
  value       = aws_s3_bucket.evidence.id
}

output "log_group" {
  description = "CloudWatch log group for all three containers."
  value       = aws_cloudwatch_log_group.backline.name
}

# The network configuration string is verbatim what `aws ecs run-task` wants for
# --network-configuration. Emitting it here means A5 never hand-assembles subnet
# and security-group ids under time pressure.
output "eval_run_task_network_config" {
  description = "Paste-ready --network-configuration argument for the A5 eval run-task."
  value       = "awsvpcConfiguration={subnets=[${aws_subnet.public_a.id},${aws_subnet.public_b.id}],securityGroups=[${aws_security_group.svc.id}],assignPublicIp=ENABLED}"
}

output "eval_run_task_command" {
  description = "The full one-off eval command (scripts/run_eval_task.sh wraps this)."
  value = join(" ", [
    "aws ecs run-task",
    "--cluster ${aws_ecs_cluster.backline.name}",
    "--launch-type FARGATE",
    "--task-definition ${aws_ecs_task_definition.eval.family}",
    "--region ${var.region}",
    "--network-configuration \"awsvpcConfiguration={subnets=[${aws_subnet.public_a.id},${aws_subnet.public_b.id}],securityGroups=[${aws_security_group.svc.id}],assignPublicIp=ENABLED}\"",
  ])
}

# Marked sensitive so it is not echoed by a bare `terraform output`. Read it
# deliberately with `terraform output -raw database_url` when A3 needs it.
output "database_url" {
  description = "Composed Postgres DSN (with sslmode=require). Same value as the backline/database-url secret."
  value       = aws_secretsmanager_secret_version.database_url.secret_string
  sensitive   = true
}
