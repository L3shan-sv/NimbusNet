terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.40" }
  }
  backend "s3" {
    bucket         = "nimbusnet-terraform-state"
    key            = "global/iam/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "nimbusnet-terraform-locks"
  }
}

provider "aws" {
  region = "us-east-1"
  default_tags {
    tags = { Project = "nimbusnet", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}

# ── Go agent role ────────────────────────────────────────────────────
resource "aws_iam_role" "go_agent" {
  name = "nimbusnet-go-agent"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "go_agent" {
  name = "nimbusnet-go-agent-policy"
  role = aws_iam_role.go_agent.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ec2:ReplaceRoute", "ec2:DescribeRouteTables"]
        Resource = "*"
        Condition = { StringEquals = { "aws:ResourceTag/Project" = "nimbusnet" } }
      },
      {
        Effect = "Allow"
        Action = [
          "route53:ChangeResourceRecordSets",
          "route53:GetHealthCheck",
          "route53:UpdateHealthCheck",
          "route53:ListResourceRecordSets"
        ]
        Resource = "arn:aws:route53:::hostedzone/*"
        Condition = { StringEquals = { "aws:ResourceTag/Project" = "nimbusnet" } }
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem", "dynamodb:GetItem",
          "dynamodb:UpdateItem", "dynamodb:ConditionCheckItem",
          "dynamodb:Query"
        ]
        Resource = [
          "arn:aws:dynamodb:*:${data.aws_caller_identity.current.account_id}:table/nimbusnet-*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParametersByPath"]
        Resource = "arn:aws:ssm:*:${data.aws_caller_identity.current.account_id}:parameter/nimbusnet/*"
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "NimbusNet" } }
      }
    ]
  })
}

resource "aws_iam_instance_profile" "go_agent" {
  name = "nimbusnet-go-agent"
  role = aws_iam_role.go_agent.name
}

# ── ML runtime role ──────────────────────────────────────────────────
resource "aws_iam_role" "ml_runtime" {
  name = "nimbusnet-ml-runtime"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "ml_runtime" {
  name = "nimbusnet-ml-runtime-policy"
  role = aws_iam_role.ml_runtime.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::nimbusnet-feature-store-${data.aws_caller_identity.current.account_id}",
          "arn:aws:s3:::nimbusnet-feature-store-${data.aws_caller_identity.current.account_id}/*",
          "arn:aws:s3:::nimbusnet-model-registry-${data.aws_caller_identity.current.account_id}",
          "arn:aws:s3:::nimbusnet-model-registry-${data.aws_caller_identity.current.account_id}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = [
          "arn:aws:s3:::nimbusnet-feature-store-${data.aws_caller_identity.current.account_id}/training-data/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParametersByPath"]
        Resource = "arn:aws:ssm:*:${data.aws_caller_identity.current.account_id}:parameter/nimbusnet/*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "ml_runtime" {
  name = "nimbusnet-ml-runtime"
  role = aws_iam_role.ml_runtime.name
}

# ── Chaos runner role ────────────────────────────────────────────────
resource "aws_iam_role" "chaos_runner" {
  name = "nimbusnet-chaos-runner"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "chaos_runner" {
  name = "nimbusnet-chaos-runner-policy"
  role = aws_iam_role.chaos_runner.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["ec2:DescribeInstances", "ec2:StartInstances", "ec2:StopInstances"]
      Resource = "*"
      Condition = { StringEquals = { "aws:ResourceTag/chaos-target" = "true" } }
    }]
  })
}

# ── Terraform runner role ────────────────────────────────────────────
resource "aws_iam_role" "terraform_runner" {
  name = "nimbusnet-terraform-runner"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "ec2.amazonaws.com" }
        Action    = "sts:AssumeRole"
      },
      {
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "sts:AssumeRole"
        Condition = { StringEquals = { "sts:ExternalId" = "nimbusnet-cold-standby-runner" } }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "terraform_runner_poweruser" {
  role       = aws_iam_role.terraform_runner.name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

# ── VPC flow logs role ───────────────────────────────────────────────
resource "aws_iam_role" "vpc_flow_logs" {
  name = "nimbusnet-vpc-flow-logs"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "vpc-flow-logs.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "vpc_flow_logs" {
  name = "nimbusnet-vpc-flow-logs-policy"
  role = aws_iam_role.vpc_flow_logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogGroup", "logs:CreateLogStream",
        "logs:PutLogEvents", "logs:DescribeLogGroups", "logs:DescribeLogStreams"
      ]
      Resource = "*"
    }]
  })
}

# ── Outputs ──────────────────────────────────────────────────────────
output "go_agent_role_arn"        { value = aws_iam_role.go_agent.arn }
output "ml_runtime_role_arn"      { value = aws_iam_role.ml_runtime.arn }
output "chaos_runner_role_arn"    { value = aws_iam_role.chaos_runner.arn }
output "terraform_runner_role_arn" { value = aws_iam_role.terraform_runner.arn }
output "vpc_flow_logs_role_arn"   { value = aws_iam_role.vpc_flow_logs.arn }
