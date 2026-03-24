variable "region_name" {
  type = string
}

variable "bgp_asn" {
  description = "Unique BGP ASN for this region's Transit Gateway"
  type        = number
}

variable "vpc_id" {
  type = string
}

variable "intra_subnet_ids" {
  description = "Intra subnet IDs for TGW attachment"
  type        = list(string)
}

variable "private_route_table_ids" {
  description = "Private route table IDs to inject remote VPC routes into"
  type        = list(string)
}

variable "is_active_region" {
  description = "Whether this is an active region (vs cold standby)"
  type        = bool
  default     = true
}

variable "remote_vpc_cidrs" {
  description = "CIDR blocks of all other NimbusNet VPCs (for cross-region routing)"
  type        = list(string)
  default     = []
}

variable "management_cidr" {
  description = "Management CIDR always routable (Terraform runner, monitoring)"
  type        = string
  default     = "10.100.0.0/16"
}

variable "tags" {
  type    = map(string)
  default = {}
}

# ── Outputs ──────────────────────────────────────────────────────────
output "tgw_id" {
  value = aws_ec2_transit_gateway.main.id
}

output "tgw_attachment_id" {
  value = aws_ec2_transit_gateway_vpc_attachment.main.id
}

output "tgw_route_table_id" {
  value = var.is_active_region ? (
    aws_ec2_transit_gateway_route_table.active[0].id
  ) : (
    aws_ec2_transit_gateway_route_table.standby[0].id
  )
}
