#!/usr/bin/env bash
# bootstrap.sh — First-time NimbusNet region setup
# Creates S3 state bucket and DynamoDB lock table before Terraform init.
# Usage: ./scripts/bootstrap.sh <region>
set -euo pipefail

REGION="${1:?Usage: $0 <region>}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
STATE_BUCKET="nimbusnet-terraform-state-${ACCOUNT_ID}"
LOCK_TABLE="nimbusnet-terraform-locks"
PROJECT_TAG="nimbusnet"

echo "==> NimbusNet bootstrap: region=${REGION} account=${ACCOUNT_ID}"

# ── S3 state bucket ──────────────────────────────────────────────────
if aws s3api head-bucket --bucket "${STATE_BUCKET}" 2>/dev/null; then
  echo "    S3 state bucket already exists: ${STATE_BUCKET}"
else
  echo "    Creating S3 state bucket: ${STATE_BUCKET}"
  if [ "${REGION}" = "us-east-1" ]; then
    aws s3api create-bucket \
      --bucket "${STATE_BUCKET}" \
      --region "${REGION}"
  else
    aws s3api create-bucket \
      --bucket "${STATE_BUCKET}" \
      --region "${REGION}" \
      --create-bucket-configuration LocationConstraint="${REGION}"
  fi

  aws s3api put-bucket-versioning \
    --bucket "${STATE_BUCKET}" \
    --versioning-configuration Status=Enabled

  aws s3api put-bucket-encryption \
    --bucket "${STATE_BUCKET}" \
    --server-side-encryption-configuration '{
      "Rules": [{
        "ApplyServerSideEncryptionByDefault": {
          "SSEAlgorithm": "AES256"
        }
      }]
    }'

  aws s3api put-public-access-block \
    --bucket "${STATE_BUCKET}" \
    --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

  aws s3api put-bucket-tagging \
    --bucket "${STATE_BUCKET}" \
    --tagging "TagSet=[{Key=Project,Value=${PROJECT_TAG}},{Key=ManagedBy,Value=terraform}]"

  echo "    State bucket created and configured."
fi

# ── DynamoDB lock table ───────────────────────────────────────────────
if aws dynamodb describe-table --table-name "${LOCK_TABLE}" --region "${REGION}" 2>/dev/null; then
  echo "    DynamoDB lock table already exists: ${LOCK_TABLE}"
else
  echo "    Creating DynamoDB lock table: ${LOCK_TABLE}"
  aws dynamodb create-table \
    --table-name "${LOCK_TABLE}" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region "${REGION}" \
    --tags Key=Project,Value="${PROJECT_TAG}" Key=ManagedBy,Value=terraform

  aws dynamodb wait table-exists \
    --table-name "${LOCK_TABLE}" \
    --region "${REGION}"

  echo "    DynamoDB lock table created."
fi

# ── IAM role for VPC flow logs ────────────────────────────────────────
FLOW_LOG_ROLE="nimbusnet-vpc-flow-logs"
if aws iam get-role --role-name "${FLOW_LOG_ROLE}" 2>/dev/null; then
  echo "    VPC flow logs IAM role already exists."
else
  echo "    Creating VPC flow logs IAM role."
  aws iam create-role \
    --role-name "${FLOW_LOG_ROLE}" \
    --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Principal": { "Service": "vpc-flow-logs.amazonaws.com" },
        "Action": "sts:AssumeRole"
      }]
    }' \
    --tags Key=Project,Value="${PROJECT_TAG}"

  aws iam put-role-policy \
    --role-name "${FLOW_LOG_ROLE}" \
    --policy-name nimbusnet-flow-logs-policy \
    --policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Action": [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams"
        ],
        "Resource": "*"
      }]
    }'

  echo "    VPC flow logs IAM role created."
fi

echo ""
echo "==> Bootstrap complete for region: ${REGION}"
echo "    State bucket : ${STATE_BUCKET}"
echo "    Lock table   : ${LOCK_TABLE}"
echo ""
echo "    Next steps:"
echo "    1. cd terraform/global/iam && terraform init && terraform apply"
echo "    2. cd terraform/global/route53 && terraform init && terraform apply"
echo "    3. cd terraform/environments/${REGION} && terraform init && terraform apply"
