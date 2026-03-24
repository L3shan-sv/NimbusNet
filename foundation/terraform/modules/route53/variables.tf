variable "domain_name" {
  description = "Root domain name for NimbusNet (e.g. nimbusnet.example.com)"
  type        = string
}

variable "health_check_targets" {
  description = "Map of region name to health check config"
  type = map(object({
    fqdn = string
    port = number
  }))
}

variable "latency_routing_targets" {
  description = "Map of region name to latency routing config"
  type = map(object({
    aws_region   = string
    alb_dns_name = string
    initial_weight = number
  }))
}

variable "tags" {
  type    = map(string)
  default = {}
}

# ── Outputs ──────────────────────────────────────────────────────────
output "hosted_zone_id" {
  value = aws_route53_zone.main.zone_id
}

output "hosted_zone_name_servers" {
  value = aws_route53_zone.main.name_servers
}

output "health_check_ids" {
  value = { for k, v in aws_route53_health_check.region : k => v.id }
}
