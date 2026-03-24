terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }

  backend "s3" {
    bucket         = "nimbusnet-terraform-state"
    key            = "us-east-1/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "nimbusnet-terraform-locks"
  }
}

provider "aws" {
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = "nimbusnet"
      Environment = "production"
      Region      = "us-east-1"
      ManagedBy   = "terraform"
      Phase       = "1"
    }
  }
}

locals {
  region_name = "us-east-1"
  is_active   = true

  common_tags = {
    Project     = "nimbusnet"
    Environment = "production"
    Region      = local.region_name
  }
}

# ── VPC ──────────────────────────────────────────────────────────────
module "vpc" {
  source = "../../modules/vpc"

  vpc_cidr    = "10.0.0.0/16"
  region_name = local.region_name

  availability_zones = ["us-east-1a", "us-east-1b"]

  public_subnet_cidrs  = ["10.0.0.0/20", "10.0.16.0/20"]
  private_subnet_cidrs = ["10.0.48.0/20", "10.0.64.0/20"]
  intra_subnet_cidrs   = ["10.0.96.0/20", "10.0.112.0/20"]

  enable_nat_gateway    = true
  flow_log_iam_role_arn = data.aws_iam_role.flow_logs.arn

  tags = local.common_tags
}

# ── Transit Gateway ───────────────────────────────────────────────────
module "transit_gateway" {
  source = "../../modules/transit-gateway"

  region_name             = local.region_name
  bgp_asn                 = 64512
  vpc_id                  = module.vpc.vpc_id
  intra_subnet_ids        = module.vpc.intra_subnet_ids
  private_route_table_ids = module.vpc.private_route_table_ids
  is_active_region        = local.is_active

  # Other NimbusNet VPC CIDRs for cross-region routing
  remote_vpc_cidrs = [
    "10.1.0.0/16", # eu-west-1
    "10.2.0.0/16", # ap-southeast-1
    "10.3.0.0/16", # sa-east-1
  ]

  tags = local.common_tags
}

# ── Security Groups ───────────────────────────────────────────────────
module "security_groups" {
  source = "../../modules/security-groups"

  vpc_id      = module.vpc.vpc_id
  region_name = local.region_name
  vpc_cidr    = "10.0.0.0/16"

  # Allow inter-region traffic via TGW
  remote_vpc_cidrs = [
    "10.1.0.0/16",
    "10.2.0.0/16",
    "10.3.0.0/16",
  ]

  tags = local.common_tags
}

# ── ALB ───────────────────────────────────────────────────────────────
module "alb" {
  source = "../../modules/alb"

  region_name       = local.region_name
  vpc_id            = module.vpc.vpc_id
  public_subnet_ids = module.vpc.public_subnet_ids
  security_group_id = module.security_groups.alb_sg_id

  tags = local.common_tags
}

# ── DynamoDB tables ───────────────────────────────────────────────────
resource "aws_dynamodb_table" "state_machine" {
  name           = "nimbusnet-state-machine"
  billing_mode   = "PAY_PER_REQUEST"
  hash_key       = "region"
  range_key      = "updated_at"

  attribute {
    name = "region"
    type = "S"
  }

  attribute {
    name = "updated_at"
    type = "N"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = local.common_tags
}

resource "aws_dynamodb_table" "standby_locks" {
  name         = "nimbusnet-standby-locks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "region"

  attribute {
    name = "region"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = local.common_tags
}

resource "aws_dynamodb_table" "incidents" {
  name           = "nimbusnet-incidents"
  billing_mode   = "PAY_PER_REQUEST"
  hash_key       = "incident_id"
  range_key      = "started_at"

  attribute {
    name = "incident_id"
    type = "S"
  }

  attribute {
    name = "started_at"
    type = "N"
  }

  attribute {
    name = "region"
    type = "S"
  }

  global_secondary_index {
    name            = "region-index"
    hash_key        = "region"
    range_key       = "started_at"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = local.common_tags
}

# ── S3 Buckets ────────────────────────────────────────────────────────
resource "aws_s3_bucket" "feature_store" {
  bucket = "nimbusnet-feature-store-${data.aws_caller_identity.current.account_id}"

  tags = local.common_tags
}

resource "aws_s3_bucket_versioning" "feature_store" {
  bucket = aws_s3_bucket.feature_store.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "feature_store" {
  bucket = aws_s3_bucket.feature_store.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "feature_store" {
  bucket                  = aws_s3_bucket.feature_store.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket" "model_registry" {
  bucket = "nimbusnet-model-registry-${data.aws_caller_identity.current.account_id}"
  tags   = local.common_tags
}

resource "aws_s3_bucket_versioning" "model_registry" {
  bucket = aws_s3_bucket.model_registry.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_public_access_block" "model_registry" {
  bucket                  = aws_s3_bucket.model_registry.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ── SSM Parameters skeleton ───────────────────────────────────────────
resource "aws_ssm_parameter" "region_config" {
  name  = "/nimbusnet/us-east-1/config"
  type  = "String"
  value = jsonencode({
    region          = "us-east-1"
    role            = "active"
    bgp_asn         = 64512
    anomaly_threshold = 0.4
    healing_threshold = 0.7
    ttl_seconds     = 300
  })

  tags = local.common_tags
}

resource "aws_ssm_parameter" "feature_store_bucket" {
  name  = "/nimbusnet/global/feature-store-bucket"
  type  = "String"
  value = aws_s3_bucket.feature_store.bucket

  tags = local.common_tags
}

resource "aws_ssm_parameter" "model_registry_bucket" {
  name  = "/nimbusnet/global/model-registry-bucket"
  type  = "String"
  value = aws_s3_bucket.model_registry.bucket

  tags = local.common_tags
}

# ── Data sources ──────────────────────────────────────────────────────
data "aws_caller_identity" "current" {}

data "aws_iam_role" "flow_logs" {
  name = "nimbusnet-vpc-flow-logs"
}
