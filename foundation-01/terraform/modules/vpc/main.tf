terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }
}

# ── VPC ─────────────────────────────────────────────────────────────
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = merge(var.tags, {
    Name = "nimbusnet-${var.region_name}"
  })
}

# ── Internet Gateway ─────────────────────────────────────────────────
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = merge(var.tags, {
    Name = "nimbusnet-igw-${var.region_name}"
  })
}

# ── Public subnets ───────────────────────────────────────────────────
resource "aws_subnet" "public" {
  count                   = length(var.public_subnet_cidrs)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = false

  tags = merge(var.tags, {
    Name = "nimbusnet-public-${var.region_name}-${count.index + 1}"
    Tier = "public"
  })
}

# ── Private subnets ──────────────────────────────────────────────────
resource "aws_subnet" "private" {
  count             = length(var.private_subnet_cidrs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.private_subnet_cidrs[count.index]
  availability_zone = var.availability_zones[count.index]

  tags = merge(var.tags, {
    Name = "nimbusnet-private-${var.region_name}-${count.index + 1}"
    Tier = "private"
  })
}

# ── Intra subnets (TGW attachments — no internet route) ─────────────
resource "aws_subnet" "intra" {
  count             = length(var.intra_subnet_cidrs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.intra_subnet_cidrs[count.index]
  availability_zone = var.availability_zones[count.index]

  tags = merge(var.tags, {
    Name = "nimbusnet-intra-${var.region_name}-${count.index + 1}"
    Tier = "intra"
  })
}

# ── NAT Gateway (active regions only) ───────────────────────────────
resource "aws_eip" "nat" {
  count  = var.enable_nat_gateway ? length(var.public_subnet_cidrs) : 0
  domain = "vpc"

  tags = merge(var.tags, {
    Name = "nimbusnet-eip-${var.region_name}-${count.index + 1}"
  })
}

resource "aws_nat_gateway" "main" {
  count         = var.enable_nat_gateway ? length(var.public_subnet_cidrs) : 0
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = merge(var.tags, {
    Name = "nimbusnet-nat-${var.region_name}-${count.index + 1}"
  })

  depends_on = [aws_internet_gateway.main]
}

# ── Route tables ─────────────────────────────────────────────────────
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = merge(var.tags, {
    Name = "nimbusnet-rtb-public-${var.region_name}"
  })
}

resource "aws_route_table" "private" {
  count  = length(var.private_subnet_cidrs)
  vpc_id = aws_vpc.main.id

  dynamic "route" {
    for_each = var.enable_nat_gateway ? [1] : []
    content {
      cidr_block     = "0.0.0.0/0"
      nat_gateway_id = aws_nat_gateway.main[count.index].id
    }
  }

  tags = merge(var.tags, {
    Name = "nimbusnet-rtb-private-${var.region_name}-${count.index + 1}"
  })
}

resource "aws_route_table" "intra" {
  vpc_id = aws_vpc.main.id

  tags = merge(var.tags, {
    Name = "nimbusnet-rtb-intra-${var.region_name}"
  })
}

# ── Route table associations ─────────────────────────────────────────
resource "aws_route_table_association" "public" {
  count          = length(var.public_subnet_cidrs)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count          = length(var.private_subnet_cidrs)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_route_table_association" "intra" {
  count          = length(var.intra_subnet_cidrs)
  subnet_id      = aws_subnet.intra[count.index].id
  route_table_id = aws_route_table.intra.id
}

# ── VPC Flow Logs ────────────────────────────────────────────────────
resource "aws_cloudwatch_log_group" "flow_logs" {
  name              = "/nimbusnet/vpc-flow-logs/${var.region_name}"
  retention_in_days = 30

  tags = var.tags
}

resource "aws_flow_log" "main" {
  vpc_id          = aws_vpc.main.id
  traffic_type    = "ALL"
  iam_role_arn    = var.flow_log_iam_role_arn
  log_destination = aws_cloudwatch_log_group.flow_logs.arn

  tags = merge(var.tags, {
    Name = "nimbusnet-flow-log-${var.region_name}"
  })
}

# ── VPC Endpoints ────────────────────────────────────────────────────
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = concat(
    [aws_route_table.private[*].id],
    [aws_route_table.intra.id]
  )

  tags = merge(var.tags, {
    Name = "nimbusnet-vpce-s3-${var.region_name}"
  })
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private[*].id]

  tags = merge(var.tags, {
    Name = "nimbusnet-vpce-dynamodb-${var.region_name}"
  })
}

data "aws_region" "current" {}
