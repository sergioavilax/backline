# One ALB, two target groups, two listeners.
#
# Two listeners rather than path-based routing because the API serves at the root
# of its own namespace (/sessions, /runs, /healthz, /readyz) and an ALB forwards
# paths verbatim — it does not rewrite them. A "/api/*" rule would deliver
# "/api/sessions" to a server that only knows "/sessions". Port-splitting gives
# both services one stable DNS name with no domain and no certificate, which is
# exactly what V5 needs: NEXT_PUBLIC_API_URL is baked into the UI bundle at build
# time, so the API's address must be known and stable before the UI image is built.

resource "aws_lb" "backline" {
  name               = "backline-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = [aws_subnet.public_a.id, aws_subnet.public_b.id]

  access_logs {
    bucket  = aws_s3_bucket.evidence.id
    prefix  = "alb"
    enabled = true
  }

  tags = { Name = "backline-alb" }

  depends_on = [aws_s3_bucket_policy.evidence]
}

# Health check on /readyz, not /healthz: /healthz proves the process is up, while
# /readyz proves the process can reach the database. That makes the target's health
# a statement about the whole SSL + security-group + restore chain, so a green
# target is real evidence rather than a liveness tautology.
resource "aws_lb_target_group" "api" {
  name        = "backline-api"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.backline.id
  target_type = "ip"

  health_check {
    path                = "/readyz"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = { Name = "backline-api-tg" }
}

resource "aws_lb_target_group" "ui" {
  name        = "backline-ui"
  port        = 3000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.backline.id
  target_type = "ip"

  health_check {
    path                = "/"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = { Name = "backline-ui-tg" }
}

resource "aws_lb_listener" "ui" {
  load_balancer_arn = aws_lb.backline.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.ui.arn
  }

  tags = { Name = "backline-ui-listener" }
}

resource "aws_lb_listener" "api" {
  load_balancer_arn = aws_lb.backline.arn
  port              = 8000
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }

  tags = { Name = "backline-api-listener" }
}
