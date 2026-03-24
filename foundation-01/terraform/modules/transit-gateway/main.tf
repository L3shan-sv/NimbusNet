terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }
}

# ── Transit Gateway ──────────────────────────────────────────────────
resource "aws_ec2_transit_gateway" "main" {
  description                     = "NimbusNet Transit Gateway - ${var.region_name}"
  amazon_side_asn                 = var.bgp_asn
  auto_accept_shared_attachments  = "disable"
  default_route_table_association = "disable"
  default_route_table_propagation = "disable"
  dns_support                     = "enable"
  vpn_ecmp_support                = "enable"

  tags = merge(var.tags, {
    Name = "nimbusnet-tgw-${var.region_name}"
  })
}

# ── VPC Attachment ───────────────────────────────────────────────────
resource "aws_ec2_transit_gateway_vpc_attachment" "main" {
  transit_gateway_id                              = aws_ec2_transit_gateway.main.id
  vpc_id                                          = var.vpc_id
  subnet_ids                                      = var.intra_subnet_ids
  transit_gateway_default_route_table_association = false
  transit_gateway_default_route_table_propagation = false
  dns_support                                     = "enable"

  tags = merge(var.tags, {
    Name = "nimbusnet-tgw-attach-${var.region_name}"
  })
}

# ── Route Tables ─────────────────────────────────────────────────────
# Active regions get the full-mesh route table
resource "aws_ec2_transit_gateway_route_table" "active" {
  count              = var.is_active_region ? 1 : 0
  transit_gateway_id = aws_ec2_transit_gateway.main.id

  tags = merge(var.tags, {
    Name = "nimbusnet-tgw-rtb-active-${var.region_name}"
  })
}

# Cold standby regions get a restricted route table (management CIDR only)
resource "aws_ec2_transit_gateway_route_table" "standby" {
  count              = var.is_active_region ? 0 : 1
  transit_gateway_id = aws_ec2_transit_gateway.main.id

  tags = merge(var.tags, {
    Name = "nimbusnet-tgw-rtb-standby-${var.region_name}"
  })
}

resource "aws_ec2_transit_gateway_route_table_association" "main" {
  transit_gateway_attachment_id  = aws_ec2_transit_gateway_vpc_attachment.main.id
  transit_gateway_route_table_id = var.is_active_region ? (
    aws_ec2_transit_gateway_route_table.active[0].id
  ) : (
    aws_ec2_transit_gateway_route_table.standby[0].id
  )
}

# Management CIDR always routable (for Terraform runner, monitoring)
resource "aws_ec2_transit_gateway_route" "management" {
  destination_cidr_block         = var.management_cidr
  transit_gateway_attachment_id  = aws_ec2_transit_gateway_vpc_attachment.main.id
  transit_gateway_route_table_id = var.is_active_region ? (
    aws_ec2_transit_gateway_route_table.active[0].id
  ) : (
    aws_ec2_transit_gateway_route_table.standby[0].id
  )
}

# Routes back to VPC private subnets via private route tables
resource "aws_route" "tgw_to_other_regions" {
  count                  = length(var.remote_vpc_cidrs)
  route_table_id         = var.private_route_table_ids[0]
  destination_cidr_block = var.remote_vpc_cidrs[count.index]
  transit_gateway_id     = aws_ec2_transit_gateway.main.id
}
