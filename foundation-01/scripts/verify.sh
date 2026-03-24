#!/usr/bin/env bash
# verify.sh — Post-apply verification for Phase 1
# Checks all expected resources exist and are healthy.
set -euo pipefail

PASS=0
FAIL=0
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

check() {
  local description="$1"
  local command="$2"
  if eval "${command}" &>/dev/null; then
    echo -e "  ${GREEN}✓${NC} ${description}"
    ((PASS++))
  else
    echo -e "  ${RED}✗${NC} ${description}"
    ((FAIL++))
  fi
}

echo ""
echo "NimbusNet Phase 1 — verification"
echo "================================="

# ── VPCs ────────────────────────────────────────────────────────────
echo ""
echo "VPCs"
for region in us-east-1 eu-west-1 ap-southeast-1 sa-east-1; do
  check "VPC exists in ${region}" \
    "aws ec2 describe-vpcs --region ${region} --filters Name=tag:Project,Values=nimbusnet --query 'Vpcs[0].VpcId' --output text | grep -v None"
done

# ── DynamoDB tables ──────────────────────────────────────────────────
echo ""
echo "DynamoDB tables"
for table in nimbusnet-state-machine nimbusnet-standby-locks nimbusnet-incidents; do
  check "Table ${table} exists" \
    "aws dynamodb describe-table --table-name ${table} --region us-east-1"
done

# ── S3 buckets ───────────────────────────────────────────────────────
echo ""
echo "S3 buckets"
check "Feature store bucket exists" \
  "aws s3api head-bucket --bucket nimbusnet-feature-store-${ACCOUNT_ID}"
check "Model registry bucket exists" \
  "aws s3api head-bucket --bucket nimbusnet-model-registry-${ACCOUNT_ID}"
check "State bucket exists" \
  "aws s3api head-bucket --bucket nimbusnet-terraform-state-${ACCOUNT_ID}"

# ── SSM parameters ───────────────────────────────────────────────────
echo ""
echo "SSM parameters"
for param in \
  "/nimbusnet/us-east-1/config" \
  "/nimbusnet/global/route53-hosted-zone-id" \
  "/nimbusnet/global/feature-store-bucket" \
  "/nimbusnet/global/model-registry-bucket"; do
  check "SSM parameter ${param}" \
    "aws ssm get-parameter --name '${param}' --region us-east-1"
done

# ── IAM roles ────────────────────────────────────────────────────────
echo ""
echo "IAM roles"
for role in nimbusnet-go-agent nimbusnet-ml-runtime nimbusnet-chaos-runner nimbusnet-terraform-runner; do
  check "IAM role ${role}" \
    "aws iam get-role --role-name ${role}"
done

# ── Local observability stack ────────────────────────────────────────
echo ""
echo "Observability stack (local Docker)"
check "Prometheus healthy" \
  "curl -sf http://localhost:9090/-/healthy"
check "Grafana healthy" \
  "curl -sf http://localhost:3000/api/health"
check "Alertmanager healthy" \
  "curl -sf http://localhost:9093/-/healthy"
check "Loki ready" \
  "curl -sf http://localhost:3100/ready"

# ── Results ──────────────────────────────────────────────────────────
echo ""
echo "================================="
echo -e "  ${GREEN}Passed: ${PASS}${NC}"
if [ "${FAIL}" -gt 0 ]; then
  echo -e "  ${RED}Failed: ${FAIL}${NC}"
  echo ""
  echo -e "  ${YELLOW}Some checks failed. Review the output above and re-apply.${NC}"
  exit 1
else
  echo -e "  ${GREEN}All checks passed. Phase 1 is ready.${NC}"
  echo ""
  echo "  Next: Phase 2 — eBPF/XDP detection layer"
fi
