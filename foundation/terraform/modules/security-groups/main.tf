terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.40" }
  }
}

# ── ALB security group ───────────────────────────────────────────────
resource "aws_security_group" "alb" {
  name        = "nimbusnet-alb-${var.region_name}"
  description = "NimbusNet ALB — inbound HTTP only"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTP from internet"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "nimbusnet-alb-sg-${var.region_name}" })
}

# ── Application security group ───────────────────────────────────────
resource "aws_security_group" "app" {
  name        = "nimbusnet-app-${var.region_name}"
  description = "NimbusNet application — inbound from ALB + inter-region TGW"
  vpc_id      = var.vpc_id

  ingress {
    description     = "From ALB"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    description = "Health check endpoint from ALB"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  # Prometheus metrics scrape
  ingress {
    description = "Prometheus metrics"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  # Inter-region traffic via TGW
  dynamic "ingress" {
    for_each = var.remote_vpc_cidrs
    content {
      description = "Inter-region TGW traffic"
      from_port   = 0
      to_port     = 0
      protocol    = "-1"
      cidr_blocks = [ingress.value]
    }
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "nimbusnet-app-sg-${var.region_name}" })
}

# ── Go agent security group ──────────────────────────────────────────
resource "aws_security_group" "go_agent" {
  name        = "nimbusnet-go-agent-${var.region_name}"
  description = "NimbusNet Go agent — metrics + management only"
  vpc_id      = var.vpc_id

  ingress {
    description = "Prometheus scrape"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "AWS APIs via VPC endpoints + internet for CloudWatch"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "nimbusnet-go-agent-sg-${var.region_name}" })
}

output "alb_sg_id"      { value = aws_security_group.alb.id }
output "app_sg_id"      { value = aws_security_group.app.id }
output "go_agent_sg_id" { value = aws_security_group.go_agent.id }
