# Postgres 16 on the smallest sane instance. The world is 130 MB and the eval
# runner is the only concurrent client, so t4g.micro is not a compromise here.

# Generated, never typed. `override_special = "-_"` keeps the password free of
# characters that would need percent-encoding inside the DATABASE_URL we compose
# in secrets.tf — a URL-escaping bug in a DSN is a miserable way to lose an hour.
resource "random_password" "db" {
  length           = 32
  special          = true
  override_special = "-_"
}

resource "aws_db_subnet_group" "backline" {
  name       = "backline-db-subnet-group"
  subnet_ids = [aws_subnet.public_a.id, aws_subnet.public_b.id]

  tags = { Name = "backline-db-subnet-group" }
}

resource "aws_db_instance" "backline" {
  identifier = "backline"

  # Major version only: AWS resolves the latest 16.x, which is >= 16.5 and therefore
  # ships pgvector 0.8.x. The dump's `CREATE EXTENSION vector` is version-unpinned,
  # so the ivfflat index restores as-is with no re-embed and no re-index (V8).
  engine         = "postgres"
  engine_version = "16"

  instance_class    = "db.t4g.micro"
  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "backline"
  username = "backline"
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.backline.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  # TRADEOFF, deliberate and time-boxed: the instance is publicly reachable so the
  # A3 restore can run from the operator's laptop straight into RDS. What makes it
  # defensible is the security group above — 5432 is open to exactly one /32 and to
  # the service SG, nothing else — plus a lifespan measured in hours and a teardown
  # that is part of the plan. Production would put this in private subnets and reach
  # it through an SSM session-manager tunnel or a bastion; that costs a NAT or a VPC
  # endpoint and an extra hop, which buys nothing for a one-day exercise. Stated
  # rather than hidden, because hiding it would be the actual mistake.
  publicly_accessible = true

  # Ephemeral by construction: no backups to orphan, no final snapshot to pay for,
  # no deletion protection to fight during teardown.
  backup_retention_period = 0
  skip_final_snapshot     = true
  deletion_protection     = false
  apply_immediately       = true

  # Default parameter group `default.postgres16` keeps `rds.force_ssl = 1` ON. We
  # comply with it via `?sslmode=require` in the composed DSN (secrets.tf) rather
  # than editing a parameter group to switch it off — better story, zero extra
  # resources, and asyncpg negotiates TLS from that DSN without code changes (V4).

  tags = { Name = "backline-rds" }
}
