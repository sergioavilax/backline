# VPC, two public subnets, IGW, one route table, and the three-security-group chain.
#
# There is deliberately NO NAT Gateway. Tasks run in public subnets with public IPs
# and egress via the IGW for ECR pulls and the Anthropic API. A NAT would add ~$32/mo
# plus $0.045/GB for exactly zero benefit at this scope and lifespan. Knowing that
# is the point; paying for it would not be.

resource "aws_vpc" "backline" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "backline-vpc" }
}

# Two AZs because both the ALB and the RDS subnet group require two. Public because
# there is no NAT (see header).
resource "aws_subnet" "public_a" {
  vpc_id                  = aws_vpc.backline.id
  cidr_block              = "10.42.1.0/24"
  availability_zone       = "${var.region}a"
  map_public_ip_on_launch = true

  tags = { Name = "backline-public-a" }
}

resource "aws_subnet" "public_b" {
  vpc_id                  = aws_vpc.backline.id
  cidr_block              = "10.42.2.0/24"
  availability_zone       = "${var.region}b"
  map_public_ip_on_launch = true

  tags = { Name = "backline-public-b" }
}

resource "aws_internet_gateway" "backline" {
  vpc_id = aws_vpc.backline.id

  tags = { Name = "backline-igw" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.backline.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.backline.id
  }

  tags = { Name = "backline-public-rt" }
}

resource "aws_route_table_association" "public_a" {
  subnet_id      = aws_subnet.public_a.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "public_b" {
  subnet_id      = aws_subnet.public_b.id
  route_table_id = aws_route_table.public.id
}

# ---------------------------------------------------------------------------
# The security-group chain. Read it top to bottom: the internet reaches only the
# ALB, the ALB reaches only the services, the services reach only the database.
# There is no 0.0.0.0/0 ingress anywhere in this file — every ingress is either
# var.home_cidr (a /32) or another security group by id.
# ---------------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "backline-alb-sg"
  description = "ALB ingress: operator home IP only, ports 80 (UI) and 8000 (API)."
  vpc_id      = aws_vpc.backline.id

  # NOTE: AWS restricts security-group descriptions to
  # ^[0-9A-Za-z_ .:/()#,@\[\]+=&;{}!$*-]*$ — no apostrophes, no em dashes. Prose
  # that reads fine in a comment fails at the API boundary, so these stay plain.
  ingress {
    description = "UI over HTTP from the operator home IP only"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.home_cidr]
  }

  ingress {
    description = "API over HTTP from the operator home IP only"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.home_cidr]
  }

  egress {
    description = "ALB to targets"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "backline-alb-sg" }
}

resource "aws_security_group" "svc" {
  name        = "backline-svc-sg"
  description = "Fargate tasks: ingress from the ALB security group only; egress anywhere (Anthropic, ECR, RDS)."
  vpc_id      = aws_vpc.backline.id

  ingress {
    description     = "API port from the ALB (by security group, not CIDR)"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    description     = "UI port from the ALB (by security group, not CIDR)"
    from_port       = 3000
    to_port         = 3000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  # Egress is open because the tasks must reach the Anthropic API, ECR, Secrets
  # Manager and CloudWatch — all public endpoints, since there is no NAT and no
  # VPC endpoints at this scope. Egress-open + ingress-locked is the tradeoff.
  egress {
    description = "Anthropic API, ECR, Secrets Manager, CloudWatch, RDS"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "backline-svc-sg" }
}

resource "aws_security_group" "rds" {
  name        = "backline-rds-sg"
  description = "Postgres 5432 from the service SG and from the operator home IP (the A3 restore path). Nothing else."
  vpc_id      = aws_vpc.backline.id

  ingress {
    description     = "Postgres from Fargate tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.svc.id]
  }

  ingress {
    description = "Postgres from the operator home IP: the A3 dump/restore path"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.home_cidr]
  }

  egress {
    description = "Outbound (unused in practice; RDS initiates nothing here)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "backline-rds-sg" }
}
