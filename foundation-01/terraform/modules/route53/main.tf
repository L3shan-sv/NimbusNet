terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }
}

# ── Hosted Zone ──────────────────────────────────────────────────────
resource "aws_route53_zone" "main" {
  name = var.domain_name

  tags = merge(var.tags, {
    Name = "nimbusnet-zone-${var.domain_name}"
  })
}

# ── Health Checks ────────────────────────────────────────────────────
resource "aws_route53_health_check" "region" {
  for_each = var.health_check_targets

  fqdn              = each.value.fqdn
  port              = each.value.port
  type              = "HTTP"
  resource_path     = "/healthz"
  failure_threshold = 3
  request_interval  = 10
  measure_latency   = true

  tags = merge(var.tags, {
    Name   = "nimbusnet-hc-${each.key}"
    Region = each.key
  })
}

# ── Latency-based routing records (active regions) ───────────────────
resource "aws_route53_record" "latency" {
  for_each = var.latency_routing_targets

  zone_id        = aws_route53_zone.main.zone_id
  name           = var.domain_name
  type           = "A"
  set_identifier = each.key

  latency_routing_policy {
    region = each.value.aws_region
  }

  health_check_id = aws_route53_health_check.region[each.key].id
  ttl             = 30

  records = [each.value.alb_dns_name]
}

# ── Weighted records for Go agent override ───────────────────────────
# The Go agent can update these weights as the adaptive shaper adjusts.
# Initially all active regions have equal weight.
resource "aws_route53_record" "weighted" {
  for_each = var.latency_routing_targets

  zone_id        = aws_route53_zone.main.zone_id
  name           = "weighted.${var.domain_name}"
  type           = "A"
  set_identifier = "${each.key}-weighted"

  weighted_routing_policy {
    weight = each.value.initial_weight
  }

  health_check_id = aws_route53_health_check.region[each.key].id
  ttl             = 30

  records = [each.value.alb_dns_name]
}

# ── SSM parameters for Go agent to discover record IDs ───────────────
resource "aws_ssm_parameter" "health_check_id" {
  for_each = var.health_check_targets

  name  = "/nimbusnet/${each.key}/route53-health-check-id"
  type  = "String"
  value = aws_route53_health_check.region[each.key].id

  tags = var.tags
}

resource "aws_ssm_parameter" "hosted_zone_id" {
  name  = "/nimbusnet/global/route53-hosted-zone-id"
  type  = "String"
  value = aws_route53_zone.main.zone_id

  tags = var.tags
}
