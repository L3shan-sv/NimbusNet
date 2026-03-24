# Phase 1 — Foundation

> Multi-region AWS infrastructure · Route 53 global routing · Transit Gateway mesh · Monitoring skeleton

---

## What this phase delivers

Phase 1 lays the infrastructure foundation that every subsequent phase builds on.
Nothing moves in NimbusNet until this phase is fully applied and verified.

### Deliverables

- VPC in all four regions (us-east-1, eu-west-1, ap-southeast-1, sa-east-1)
- Transit Gateway with full-mesh peering and unique BGP ASNs per region
- Application Load Balancer per active region (us-east-1, eu-west-1)
- Route 53 hosted zone with latency-based routing + health checks
- Security groups following least-privilege principle
- IAM roles for Go agent, Terraform runner, ML runtime, chaos runner
- DynamoDB tables: incident store, standby lock table, state machine store
- S3 buckets: feature store, model registry, Terraform state, audit logs
- SSM Parameter Store skeleton for runtime configuration
- Full local observability stack (Prometheus, Grafana, Loki, Tempo, Alertmanager)
- Prometheus alerting rules: SLO burn rate (all three windows)
- Grafana dashboard: global topology + SLO burn rate

---

## Directory layout

```
phase-01-foundation/
├── README.md                         ← You are here
├── DOCUMENTATION.md                  ← Phase 1 detailed reference
├── terraform/
│   ├── modules/
│   │   ├── vpc/                      ← VPC, subnets, IGW, NAT, flow logs
│   │   ├── alb/                      ← Application Load Balancer
│   │   ├── route53/                  ← Hosted zone, records, health checks
│   │   ├── transit-gateway/          ← TGW, attachments, peering
│   │   └── security-groups/          ← All security group definitions
│   ├── environments/
│   │   ├── us-east-1/                ← Active region
│   │   ├── eu-west-1/                ← Active region
│   │   ├── ap-southeast-1/           ← Cold standby
│   │   └── sa-east-1/                ← Cold standby
│   └── global/
│       ├── route53/                  ← Global hosted zone + routing policy
│       └── iam/                      ← All IAM roles and policies
├── monitoring/
│   ├── prometheus/
│   │   ├── prometheus.yml
│   │   └── rules/
│   │       ├── slo-burn-rate.yml
│   │       └── nimbusnet-alerts.yml
│   ├── grafana/
│   │   ├── provisioning/
│   │   └── dashboards/
│   ├── alertmanager/
│   │   └── alertmanager.yml
│   ├── loki/
│   │   └── loki.yml
│   ├── tempo/
│   │   └── tempo.yml
│   └── promtail/
│       └── promtail.yml
└── scripts/
    ├── bootstrap.sh                  ← First-time region setup
    ├── verify.sh                     ← Post-apply verification
    └── destroy.sh                    ← Safe teardown (requires confirmation)
```

---

## Apply order

Terraform modules have dependencies. Apply in this exact order:

```
1. terraform/global/iam          — IAM roles (no dependencies)
2. terraform/global/route53      — Hosted zone (no dependencies)
3. terraform/environments/us-east-1    — Active region (depends on IAM)
4. terraform/environments/eu-west-1    — Active region (depends on IAM)
5. terraform/environments/ap-southeast-1  — Cold standby skeleton only
6. terraform/environments/sa-east-1       — Cold standby skeleton only
```

Cold standby environments apply a minimal skeleton (VPC + TGW attachment only).
Full provisioning is triggered by the cold standby contract at runtime.

---

## Quick start

```bash
# 1. Configure AWS credentials
export AWS_PROFILE=nimbusnet-dev

# 2. Bootstrap (creates S3 state bucket + DynamoDB lock table if not exists)
./scripts/bootstrap.sh us-east-1

# 3. Apply global resources first
cd terraform/global/iam && terraform init && terraform apply
cd ../route53     && terraform init && terraform apply

# 4. Apply active regions
cd ../../environments/us-east-1 && terraform init && terraform apply
cd ../eu-west-1                 && terraform init && terraform apply

# 5. Apply cold standby skeletons
cd ../ap-southeast-1 && terraform init && terraform apply
cd ../sa-east-1      && terraform init && terraform apply

# 6. Verify
cd ../../.. && ./scripts/verify.sh

# 7. Start observability stack
cd ../../.. && docker compose up -d
```

---

## Verification checklist

After applying, `./scripts/verify.sh` checks:

- [ ] VPC exists in all four regions with correct CIDR blocks
- [ ] Transit Gateway peering established between all region pairs
- [ ] Route 53 hosted zone resolves correctly
- [ ] Health checks returning 200 on both active ALBs
- [ ] IAM roles have expected permission boundaries
- [ ] DynamoDB tables exist with correct key schema
- [ ] S3 buckets exist with versioning enabled
- [ ] Grafana reachable at http://localhost:3000
- [ ] Prometheus scraping at http://localhost:9090
- [ ] Alertmanager reachable at http://localhost:9093

---

## Phase 1 SLOs (infrastructure layer)

| Target | Metric | Alert |
|--------|--------|-------|
| Route 53 health check pass rate > 99.9% | `aws_route53_health_check_status` | P2 |
| ALB 5xx rate < 0.1% | `aws_alb_httpcode_target_5xx_count` | P1 |
| TGW packet loss < 0.01% | VPC flow logs | P2 |

Full burn rate alerting activates in Phase 5 once the Go agent emits SLI metrics.
Phase 1 alerting covers infrastructure-layer signals only.

---

## What Phase 2 needs from this phase

- EC2 instances (created in Phase 2, but IAM roles and VPCs must exist)
- DynamoDB table `nimbusnet-state-machine` for Go agent state persistence
- SSM parameters: `/nimbusnet/{region}/config` skeleton
- S3 bucket `nimbusnet-feature-store-{account}` for ML training data
- Prometheus scrape targets configured for Go agent metrics endpoint (port 8080)
