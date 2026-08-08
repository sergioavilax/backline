# Two secrets. Both are created with `recovery_window_in_days = 0` because the
# default 30-day recovery window keeps deleted secrets alive — and billable — long
# after `terraform destroy` reports success, and blocks re-creating a secret of the
# same name if you run the exercise twice (V13).

# ---------------------------------------------------------------------------
# 1. DATABASE_URL — composed and set BY Terraform, because every input already
#    lives in state anyway (the generated password, the instance address).
# ---------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "database_url" {
  name                    = "backline/database-url"
  description             = "Postgres DSN for the Backline API and eval tasks, with sslmode=require."
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id

  # Two load-bearing details in one string:
  #
  #   .address  NOT  .endpoint  — `.endpoint` already carries ":5432", so using it
  #   here produces "host:5432:5432" and a DSN that fails to parse. This is the
  #   single easiest way to lose thirty minutes in this whole deployment.
  #
  #   ?sslmode=require — RDS PostgreSQL 15+ defaults `rds.force_ssl = 1`. asyncpg
  #   parses this and negotiates TLS; without it /readyz returns 503 with a pg_hba
  #   message and looks like a security-group problem, which it is not (V4).
  secret_string = "postgresql://backline:${random_password.db.result}@${aws_db_instance.backline.address}:5432/backline?sslmode=require"
}

# ---------------------------------------------------------------------------
# 2. ANTHROPIC_API_KEY — a shell only. Terraform creates the container and never
#    learns the value, which is the entire point: the key stays out of the repo,
#    out of state, and out of any plan output.
#
#    The human sets it once, after apply, with a LEADING SPACE so the command is
#    skipped by shell history:
#
#      aws secretsmanager put-secret-value \
#        --secret-id backline/anthropic-api-key \
#        --secret-string "$ANTHROPIC_API_KEY"
#
#    Until that runs, tasks start but the app comes up in demo mode — which is the
#    documented symptom in the §9 failure playbook, not a mystery.
# ---------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "anthropic_api_key" {
  name                    = "backline/anthropic-api-key"
  description             = "Anthropic API key. Value is set out-of-band by the operator; Terraform never sees it."
  recovery_window_in_days = 0
}
