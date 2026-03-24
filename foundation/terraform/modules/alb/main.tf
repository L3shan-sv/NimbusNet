terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.40" }
  }
}

resource "aws_lb" "main" {
  name               = "nimbusnet-alb-${var.region_name}"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [var.security_group_id]
  subnets            = var.public_subnet_ids

  enable_deletion_protection = false
  enable_http2               = true

  access_logs {
    bucket  = var.access_logs_bucket
    prefix  = "alb-${var.region_name}"
    enabled = var.access_logs_bucket != ""
  }

  tags = merge(var.tags, {
    Name = "nimbusnet-alb-${var.region_name}"
  })
}

resource "aws_lb_target_group" "app" {
  name        = "nimbusnet-tg-${var.region_name}"
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "instance"

  health_check {
    enabled             = true
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 10
    path                = "/healthz"
    port                = "traffic-port"
    protocol            = "HTTP"
    matcher             = "200"
  }

  tags = merge(var.tags, {
    Name = "nimbusnet-tg-${var.region_name}"
  })
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

output "alb_arn"      { value = aws_lb.main.arn }
output "alb_dns_name" { value = aws_lb.main.dns_name }
output "alb_zone_id"  { value = aws_lb.main.zone_id }
output "target_group_arn" { value = aws_lb_target_group.app.arn }
