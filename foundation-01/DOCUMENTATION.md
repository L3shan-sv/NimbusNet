# Phase 1 — Foundation: Detailed Documentation

---

## VPC design

### CIDR allocation

| Region | VPC CIDR | Public subnets | Private subnets | Intra subnets |
|--------|----------|----------------|-----------------|---------------|
| us-east-1 | 10.0.0.0/16 | 10.0.0.0/20, 10.0.16.0/20 | 10.0.48.0/20, 10.0.64.0/20 | 10.0.96.0/20 |
| eu-west-1 | 10.1.0.0/16 | 10.1.0.0/20, 10.1.16.0/20 | 10.1.48.0/20, 10.1.64.0/20 | 10.1.96.0/20 |
| ap-southeast-1 | 10.2.0.0/16 | 10.2.0.0/20, 10.2.16.0/20 | 10.2.48.0/20, 10.2.64.0/20 | 10.2.96.0/20 |
| sa-east-1 | 10.3.0.0/16 | 10.3.0.0/20, 10.3.16.0/20 | 10.3.48.0/20, 10.3.64.0/20 | 10.3.96.0/20 |

Non-overlapping CIDRs are required for Transit Gateway routing.

### Subnet roles

- **Public subnets**: ALB only. No application workloads.
- **Private subnets**: EC2 application instances, Go agent, ML runtime.
- **Intra subnets**: Transit Gateway attachments, no internet route.

### VPC endpoints (all regions)

- `com.amazonaws.{region}.s3` — Gateway endpoint (feature store, model registry)
- `com.amazonaws.{region}.dynamodb` — Gateway endpoint (state machine, locks)
- `com.amazonaws.{region}.ssm` — Interface endpoint (config parameters)
- `com.amazonaws.{region}.secretsmanager` — Interface endpoint (credentials)

No NAT gateway traffic for control plane operations. All AWS API calls go via VPC endpoints.

---

## Transit Gateway

### Architecture

Full-mesh peering between all four regions. Each region has a unique BGP ASN.

```
us-east-1 (ASN 64512) ◄──► eu-west-1 (ASN 64513)
       │                         │
       ▼                         ▼
ap-southeast-1 (ASN 64514) ◄──► sa-east-1 (ASN 64515)
```

All six peering links are established at Phase 1 apply time. Cold standby regions
have TGW attachments but route propagation is restricted — they only accept
management CIDR (10.100.0.0/16) until provisioned.

### Route tables

| Route table | Associated VPCs | Propagated routes |
|-------------|----------------|-------------------|
| `active-rt` | us-east-1, eu-west-1 | All four VPC CIDRs |
| `standby-rt` | ap-southeast-1, sa-east-1 | Management CIDR only |

When a cold standby region is provisioned, the Go agent calls:
```
aws ec2 enable-transit-gateway-route-table-propagation \
  --transit-gateway-route-table-id {active-rt-id} \
  --transit-gateway-attachment-id {ap-southeast-1-attachment-id}
```

---

## Route 53 configuration

### Hosted zone

Public hosted zone: `nimbusnet.internal` (replace with your domain)

### Record set strategy

```
nimbusnet.internal  →  Latency-based alias records
  us-east-1-alb.nimbusnet.internal  (latency, health check, weight 50)
  eu-west-1-alb.nimbusnet.internal  (latency, health check, weight 50)
```

Health checks: HTTP on port 80, path `/healthz`, 3 failure threshold, 10s interval.

When the Go agent detects degradation, it sets `/healthz → 503` on the target region.
Route 53 picks this up within 30–60 seconds (3 failures × 10s + TTL drain).

### TTL policy

All NimbusNet records: **TTL = 30 seconds**

This is the minimum that makes operational sense. Lower TTL increases DNS query volume
significantly. 30 seconds means worst-case 90-second DNS drain (3 failures + TTL).

---

## IAM roles

### nimbusnet-go-agent

Permissions scoped to NimbusNet resources only via condition on resource ARN prefix.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:ReplaceRoute",
        "ec2:DescribeRouteTables"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": { "aws:ResourceTag/Project": "nimbusnet" }
      }
    },
    {
      "Effect": "Allow",
      "Action": [
        "route53:ChangeResourceRecordSets",
        "route53:GetHealthCheck",
        "route53:UpdateHealthCheck"
      ],
      "Resource": "arn:aws:route53:::hostedzone/{hosted-zone-id}"
    },
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "dynamodb:ConditionCheckItem"
      ],
      "Resource": [
        "arn:aws:dynamodb:*:*:table/nimbusnet-state-machine",
        "arn:aws:dynamodb:*:*:table/nimbusnet-standby-locks",
        "arn:aws:dynamodb:*:*:table/nimbusnet-incidents"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter",
        "ssm:GetParametersByPath"
      ],
      "Resource": "arn:aws:ssm:*:*:parameter/nimbusnet/*"
    }
  ]
}
```

### nimbusnet-ml-runtime

Read-only access to feature store and model registry S3 buckets.
No network, no compute, no DynamoDB.

### nimbusnet-chaos-runner

Full EC2 permissions scoped to instances tagged `chaos-target: true`.
Cannot touch production instances that lack this tag.

### nimbusnet-terraform-runner

Full infrastructure permissions scoped to resources with `Project: nimbusnet` tag.
Used exclusively by Terraform Cloud for cold standby provisioning runs.

---

## DynamoDB tables

### nimbusnet-state-machine

| Attribute | Type | Key |
|-----------|------|-----|
| region | String | Partition key |
| updated_at | Number | Sort key |
| state | String | |
| anomaly_score | Number | |
| last_action | String | |
| ttl | Number | TTL attribute |

### nimbusnet-standby-locks

| Attribute | Type | Key |
|-----------|------|-----|
| region | String | Partition key |
| status | String | |
| locked_by | String | |
| ttl | Number | TTL attribute (300s) |

Conditional write on `attribute_not_exists(region)` enforces idempotency.

### nimbusnet-incidents

| Attribute | Type | Key |
|-----------|------|-----|
| incident_id | String | Partition key |
| started_at | Number | Sort key |
| severity | String | |
| region | String | GSI partition key |
| runbook_id | String | |
| resolved | Boolean | |
| sre_engaged | Boolean | |
| ttl | Number | TTL attribute (90 days) |

---

## Monitoring configuration

### Prometheus SLO burn rate rules

Three PromQL recording rules, one per window pair, implementing the Google SRE Workbook
multi-window burn rate algorithm.

**Fast burn (14.4× — page within 2 hours):**
```promql
# 1-hour window
rate(nimbusnet_requests_total{status="error"}[1h])
  /
rate(nimbusnet_requests_total[1h])
  > (14.4 * (1 - 0.9995))

# AND 5-minute window must also be elevated
rate(nimbusnet_requests_total{status="error"}[5m])
  /
rate(nimbusnet_requests_total[5m])
  > (14.4 * (1 - 0.9995))
```

**Slow burn (6× — ticket within 5 hours):**
Same pattern with 6h/30m windows and 6× multiplier.

**Silent leak (1× — log it):**
Same pattern with 3d/6h windows and 1× multiplier.

### Alertmanager routing

```yaml
route:
  group_by: [region, severity]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  receiver: slack-ops

  routes:
    - match:
        severity: P0
      receiver: pagerduty-p0
      continue: true
    - match:
        severity: P1
      receiver: pagerduty-p1
      continue: true
    - match:
        alertname: NimbusNetSplitBrain
      receiver: pagerduty-p0
      group_wait: 0s
```

Split-brain alerts get `group_wait: 0s` — they fire immediately with no batching.

---

## Grafana dashboards (Phase 1)

### Dashboard 1 — Global topology

Panels:
- Region health status (4 stat panels, one per region — green/amber/red)
- Route 53 health check pass rate per region (time series, 24h)
- ALB request rate per region (time series)
- TGW bytes transferred per peering link (heatmap)
- Active alerts table

### Dashboard 2 — SLO burn rate

Panels:
- Current burn rate per window (3 gauge panels)
- Error budget remaining — 28-day (gauge with threshold bands)
- Burn rate history (time series, all three windows overlaid)
- Deploy freeze indicator (stat panel — green = deploys allowed, red = frozen)
- Incident count by severity (bar chart, 7-day)

Both dashboards are provisioned from JSON in `monitoring/grafana/dashboards/`.
They are auto-loaded on Grafana startup — no manual import required.
