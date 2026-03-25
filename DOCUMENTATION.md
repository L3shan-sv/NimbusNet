# NimbusNet — System Architecture Documentation

**Version:** 1.0.0  
**Last updated:** Phase 1  
**Owner:** nimbusnet-sre

---

## Table of contents

1. [System overview](#1-system-overview)
2. [Global topology](#2-global-topology)
3. [Telemetry pipeline](#3-telemetry-pipeline)
4. [Healing state machine](#4-healing-state-machine)
5. [ML intelligence layer](#5-ml-intelligence-layer)
6. [Consistent-hash control plane](#6-consistent-hash-control-plane)
7. [Adaptive traffic shaper](#7-adaptive-traffic-shaper)
8. [SLO and burn-rate engine](#8-slo-and-burn-rate-engine)
9. [Chaos engineering](#9-chaos-engineering)
10. [Observability stack](#10-observability-stack)
11. [SRE operational loop](#11-sre-operational-loop)
12. [Runbook catalog](#12-runbook-catalog)
13. [Postmortem process](#13-postmortem-process)
14. [Cold standby trigger contract](#14-cold-standby-trigger-contract)
15. [Security model](#15-security-model)
16. [Dependency map](#16-dependency-map)

---

## 1. System overview

NimbusNet is an autonomous global network platform built on four AWS regions. The system
self-heals routing failures without human intervention using a pipeline that runs from
kernel-level packet inspection through ML anomaly detection through a formally specified
Go state machine to Route 53 DNS failover.

The SRE operational model follows Google's SRE principles: the system handles everything
it can autonomously, records every action in postmortems, and escalates to a human only
when its remediation budget is exhausted.

### Design principles

- **Detect at wire speed.** XDP hooks run before the kernel network stack. Detection
  latency is milliseconds, not polling intervals.
- **Heal in the data plane first.** Route table flips happen in the VPC before DNS
  converges. The fast path is always the data plane.
- **Poison DNS to drain the slow path.** Once the data plane heals, the Go agent sets
  `/healthz → 503` so Route 53 stops sending new clients to the degraded region.
- **Predict before you react.** The LSTM forecaster gives 30-second congestion lookahead.
  The XGBoost pre-warm predictor fires cold standby provisioning before it is needed.
- **Own your failure modes.** Every component has a formally specified failure mode,
  a rollback path, and a chaos experiment that validates the claim.

---

## 2. Global topology

### Regions

| Region | Role | BGP ASN | Terraform workspace |
|--------|------|---------|-------------------|
| us-east-1 | Active-primary | 64512 | nimbusnet-us-east-1 |
| eu-west-1 | Active-primary | 64513 | nimbusnet-eu-west-1 |
| ap-southeast-1 | Cold standby | 64514 | nimbusnet-ap-southeast-1 |
| sa-east-1 | Cold standby | 64515 | nimbusnet-sa-east-1 |

### Route 53 policy

Active-active regions use latency-based routing as the primary policy with health check
failover as the safety net. The Go agent updates weighted routing records as a secondary
signal when the adaptive traffic shaper adjusts regional weights.

TTL is set to 30 seconds on all NimbusNet records — low enough to drain traffic quickly,
high enough to avoid excessive DNS load.

### Transit Gateway mesh

All four regions are connected via Transit Gateway with full-mesh peering. Each region
has a unique BGP ASN. Route propagation is controlled per-attachment.

Active regions: attachment accepts all routes.  
Cold standby regions: attachment accepts only management routes until provisioned.

---

## 3. Telemetry pipeline

The telemetry pipeline is the nervous system of NimbusNet. Every downstream consumer —
the healing state machine, all four ML models, the SLO engine — reads from this pipeline.

### Data flow

```
XDP hook (pre-stack)
  → tc egress hook (flow RTT + loss)
    → BPF_HASH map (per-flow ring buffer)
      → perf_event buffer (50ms flush)
        → Go agent reader (userspace)
          → 1s window aggregator (p50/p99/loss per flow)
            → [ONNX runtime → anomaly score]
            → [signal bus channels]
              → healing state machine
              → XGBoost pre-warm model
              → contextual bandit router
              → LSTM forecaster
              → SLO burn-rate engine
```

### Signal bus channels

| Channel | Type | Producer | Consumers |
|---------|------|----------|-----------|
| `anomaly_score` | `float32` per region | ONNX runtime | State machine, SLO engine |
| `raw_metrics` | RTT/loss/retx struct | Aggregator | XGBoost, bandit, SLO engine |
| `lstm_features` | 30s rolling window | Aggregator | LSTM forecaster |

All channels are typed Go channels. Nothing polls — everything is push.

### eBPF kernel requirements

- Linux kernel >= 5.15
- `CONFIG_BPF=y`, `CONFIG_BPF_SYSCALL=y`, `CONFIG_XDP_SOCKETS=y`
- Privileged container or `CAP_BPF` + `CAP_NET_ADMIN`

---

## 4. Healing state machine

The Go agent runs one state machine per region. Each instance is independent.
Consistent-hash ownership is enforced before any transition — only the agent that
owns the flow key for a given region may initiate a state transition.

### States

| State | Entry condition | Exit conditions |
|-------|----------------|-----------------|
| `HEALTHY` | Default | anomaly_score ≥ 0.4 OR p99 > 180ms → DEGRADED |
| `DEGRADED` | Score/RTT threshold | Recovery within 15s → HEALTHY; score ≥ 0.7 OR LSTM confirms → HEALING |
| `HEALING` | Score ≥ 0.7 | API ACK < 2s → RECOVERED; API timeout > 2s → FAILED |
| `RECOVERED` | API ACK received | 3 clean probe windows + score < 0.3 → HEALTHY |
| `FAILED` | API timeout or chaos abort | Manual ACK → HEALTHY (with postmortem) |

### HEALING actions (in order)

1. VPC route table flip — AWS API call, logged with timestamp
2. `/healthz` endpoint → 503 — poisons Route 53 health check
3. Record flip timestamp in DynamoDB incident store

### SLO gate on HEALING

The HEALING → RECOVERED transition is gated by the healing latency SLI. If the API
call takes > 2 seconds, the transition goes to FAILED instead and the chaos abort
path fires. This is how the p99 < 2s SLO is enforced in the data plane.

---

## 5. ML intelligence layer

### Model catalog

| Model | Algorithm | Training data | Retrain cadence | Deployment |
|-------|-----------|---------------|-----------------|------------|
| Anomaly detector | Isolation Forest | Normal traffic baselines | Weekly | ONNX, per-region |
| Congestion forecaster | LSTM autoencoder | tc qdisc synthetic + production | Daily | ONNX, per-region |
| Pre-warm predictor | XGBoost | Tabular: hour-of-day, RTT trend, failure history | Weekly | ONNX, per-region |
| Routing optimizer | Contextual bandit (LinUCB) | Live routing outcomes | Online, every event | In-process Go |

### Feature store

Location: S3 bucket `nimbusnet-feature-store-{account-id}`  
Format: Parquet, partitioned by `region` + `fault_type` + `date`  
Schema versioned — breaking changes require migration script.

### Model registry

MLflow tracking server. Models promoted to production only after shadow-mode validation
against the current production model. Rollback in < 30 seconds by reverting the ONNX
file pointer in SSM Parameter Store.

### Confidence gating

No model signal alone triggers a failover. The Go agent requires:
- Anomaly score ≥ threshold, AND
- At least one corroborating signal (raw metric breach OR second model agreement)

This prevents a miscalibrated model from routing in production.

---

## 6. Consistent-hash control plane

### Why consistent hashing?

With two active agents seeing the same telemetry, competing AWS API calls for the same
route table during a failover event would cause split-brain. A Dynamo-style consistent
hash ring assigns each flow key to exactly one agent deterministically — ownership is
enforced by the ring, not by coordination.

### Ring configuration

- 150 virtual nodes total, distributed across active regions
- US-East-1: 38 vnodes, EU-West-1: 37 vnodes
- Cold standby regions: 0 vnodes (not on ring until provisioned, then 37/38 each)
- Hash function: xxHash64 on `{region}:{flow_src_ip}:{flow_dst_port}`

### On region loss

When a region goes dark, its virtual nodes are vacated. The next clockwise owner
on the ring absorbs those flows. Only the affected vnodes rehash — all other flows
are undisturbed. This is O(k/n) disruption where k is failed vnodes and n is total.

---

## 7. Adaptive traffic shaper

### Weight decay function

| p99 RTT | Anomaly score | R53 weight | Bandit action |
|---------|--------------|------------|---------------|
| < 120ms | < 0.40 | 50% | Normal exploration |
| 120–200ms | 0.40–0.70 | 20–30% | Reduced exploration |
| > 200ms | > 0.70 | 0% (drained) | Exploit only, trigger standby |

### Transition rules

- Weight updates have a 15-second cooldown to prevent oscillation.
- Weight changes are applied to Route 53 weighted records via AWS API.
- The contextual bandit continues to receive low-weight traffic for exploration
  signal — a region is never fully cut until it hits 0% threshold.
- Recovery: weight ramps back up in 10% increments per clean probe window.

---

## 8. SLO and burn-rate engine

### SLIs

| SLI | Definition | Target |
|-----|-----------|--------|
| Availability | Successful requests / total requests | 99.95% |
| Healing latency | Time from HEALING entry to RECOVERED entry | p99 < 2s |
| Error budget burn | Actual error rate / (1 - SLO) | See burn matrix |
| Split-brain | Count of simultaneous dual-authoritative states | 0 |

### Multi-window burn rate matrix (Google SRE Workbook)

| Long window | Short window | Burn multiplier | Alert severity | Budget exhaustion |
|-------------|-------------|-----------------|----------------|-------------------|
| 1 hour | 5 minutes | 14.4× | PAGE (P0) | < 2 hours |
| 6 hours | 30 minutes | 6× | TICKET (P1) | < 5 hours |
| 3 days | 6 hours | 1× | LOG (P2) | Budget leaking |

An alert fires only when BOTH the long and short window exceed the multiplier threshold.
This eliminates transient spike alerts while catching genuine burns.

### Error budget policy

- Budget > 30%: normal operations, deploys allowed
- Budget 10–30%: heightened awareness, deploy approval required
- Budget < 10%: deploy freeze, all changes require incident commander sign-off
- Budget 0%: incident declared, P0 paged, postmortem mandatory

---

## 9. Chaos engineering

### Experiment catalog

| ID | Name | Blast radius | Frequency | Blocking |
|----|------|-------------|-----------|----------|
| EXP-001 | tc qdisc packet loss | Single host | Every PR | Yes — detection regression |
| EXP-002 | Slow RTT ramp | Single host | Every PR | Yes — anomaly score regression |
| EXP-003 | Route table flip e2e | Single region | Weekly | Yes — healing latency regression |
| EXP-004 | ML signal vs raw signal conflict | Single region | Weekly | No — observe |
| EXP-005 | R53 flip race condition | Single region | Weekly | No — observe |
| EXP-006 | Dual-region blackout | Both active regions | Monthly GameDay | No — full postmortem |

### CI gate

EXP-001 and EXP-002 run on every PR merge to `main`. Gate conditions:

- Detection latency must not exceed 80ms (EXP-001)
- Isolation Forest must fire within 3 probe windows of ramp start (EXP-002)
- Healing latency p99 must not regress by more than 200ms vs 7-day baseline (EXP-003, weekly)

A merge is blocked if any gate condition fails.

### Abort policy

| SLO breach | Action |
|-----------|--------|
| Healing time > 2s | Auto-stop, FAILED state, page SRE |
| Error rate > 0.1% | Observe (P2 log) |
| Split-brain detected | Auto-stop, immediate P0 page |

---

## 10. Observability stack

### Stack

| Component | Role | Port |
|-----------|------|------|
| Prometheus | Metrics scrape + storage | 9090 |
| Thanos / Mimir | Long-term metrics, S3 backend | 9091 |
| Loki | Log aggregation | 3100 |
| Tempo | Distributed traces | 3200 |
| Grafana | Unified dashboards | 3000 |
| Alertmanager | Alert routing + dedup | 9093 |

### Signal sources

- eBPF/XDP: per-flow RTT, loss rate, retransmit count (50ms resolution)
- Go agent: state transition events, action outcomes (structured JSON logs)
- ML runtime: inference latency, anomaly score distribution, model drift metrics
- AWS infra: Route 53 health check status, VPC flow logs, ALB request metrics

### Key Grafana dashboards

| Dashboard | Description |
|-----------|-------------|
| Global topology | Real-time region health, Route 53 weights, active state per region |
| SLO burn rate | All three burn windows, error budget remaining, deploy freeze indicator |
| ML health | Model inference latency, score distribution per region, drift alerts |
| Incident timeline | Per-incident state machine trace, runbook step execution, SRE engagement log |
| Chaos results | Per-experiment outcome history, regression trend, CI gate pass rate |

---

## 11. SRE operational loop

### Principles

1. The system is the first responder — always.
2. The SRE is in the loop passively at all times via Slack notifications.
3. The SRE is engaged actively only when the system cannot resolve within the runbook TTL.
4. Every incident — auto-resolved or escalated — generates a postmortem.
5. Every postmortem feeds the ML training pipeline, the runbook catalog, the SLO calibration,
   and the chaos experiment suite.

### Incident lifecycle

```
Detection (XDP + ML + SLO)
  → Incident declared + severity assigned
    → Automated runbook executor selects runbook by incident type
      → Runbook executes (TTL: 300s default)
        → Resolved within TTL?
            YES → Resolved · SRE notified via Slack (FYI) · postmortem auto-generated
            NO  → Escalate → PagerDuty P0/P1 → SRE engaged → manual resolution
                  → postmortem auto-generated + SRE writes root cause narrative
        → Postmortem feeds: ML retrain · runbook update · SLO recalibration · new chaos exp
```

### SRE notification matrix

| Event | Channel | Required action | Severity |
|-------|---------|----------------|----------|
| Auto-resolved incident | Slack #nimbusnet-ops | None — FYI | P2/P3 |
| Runbook TTL breached | Slack + PagerDuty | Engage within 15 min | P1 |
| Both active regions HEALING | PagerDuty immediate | Engage within 5 min | P0 |
| Error budget < 10% | Slack #sre-budget-alert | Deploy freeze + review | P1 |
| Postmortem published | Slack #postmortems | Review within 48h (P0/P1 only) | P2 |
| ML model accuracy drift | Slack #ml-health | Approve retrain | P2 |

---

## 12. Runbook catalog

| ID | Name | Trigger | TTL | Phase |
|----|------|---------|-----|-------|
| RB-001 | Region failover | anomaly_score ≥ 0.70 AND HEALING AND route_flip_failed | 300s | 4 |
| RB-002 | ML model degradation | inference_latency_p99 > 100ms OR accuracy_drift > 0.15 | 600s | 6 |
| RB-003 | Cold standby provision | XGBoost score ≥ 0.70 AND one hard signal | 600s | 4 |
| RB-004 | SLO fast burn | Burn rate > 14.4× sustained > 5min | 300s | 5 |
| RB-005 | Split-brain recovery | Split-brain SLI breached | 120s | 4 |

Full runbook specifications are in `docs/runbooks/`.

---

## 13. Postmortem process

### Auto-populated fields (always)

- Incident ID, severity, start/end timestamps (ms precision)
- ML anomaly score at T=0
- Runbook ID and step trace
- SLO budget delta (before and after)
- Healing latency p50/p99
- Region states at each state machine transition
- SRE engaged: Y/N, time-to-engage if Y
- Chaos experiment active at time of incident: Y/N

### Human-written fields (P0/P1 escalations only)

- Root cause narrative
- Contributing factors
- Why the runbook TTL was exceeded
- What the system got wrong
- Action items with owner and due date
- Runbook gaps identified
- SLO threshold recalibration recommendation
- Model retraining recommendation
- New chaos experiment to add

### Postmortem feedback routing

Every published postmortem automatically:
1. Emits a labelled training sample to the feature store (incident type + outcome)
2. Creates a Jira ticket for any identified runbook gaps
3. Triggers a review of the relevant SLO window thresholds
4. Proposes a new chaos experiment if the failure mode was not covered

---

## 14. Cold standby trigger contract

### Trigger conditions

Both of the following must be true:

1. XGBoost pre-warm score ≥ 0.70 (probability standby needed in next 15 min), AND
2. At least one hard signal: both active regions in HEALING state OR fast burn rate ≥ 14.4×

### Idempotency guarantee

The trigger is implemented as a DynamoDB conditional write:

```
PutItem {
  TableName: nimbusnet-standby-locks
  Item: { region: "ap-southeast-1", status: "PENDING", ttl: now+300 }
  ConditionExpression: "attribute_not_exists(region)"
}
```

If both US-East and EU-West agents fire simultaneously, only one acquires the lock.
The second drops silently. No duplicate Terraform runs. No split provisioning.

### Provisioning flow

1. Lock acquired → POST to Terraform Cloud API `/runs` for the target workspace
2. Terraform applies pre-validated module from S3 remote state
3. EC2 instances warm up (< 90s target)
4. DynamoDB lock updated: `status = "ACTIVE"`
5. Route 53 weighted record updated: target region weight += 50%
6. SRE notified via Slack #nimbusnet-ops

---

## 15. Security model

### IAM principle of least privilege

Each component has its own IAM role with only the permissions it needs:

| Component | IAM permissions |
|-----------|----------------|
| Go agent | `ec2:ReplaceRoute`, `route53:ChangeResourceRecordSets`, `dynamodb:PutItem/GetItem` |
| Terraform runner | Full infra permissions, scoped to NimbusNet resource prefix |
| ML runtime | `s3:GetObject` on feature store and model registry buckets only |
| Chaos runner | `ec2:*` on chaos-tagged instances only |

### Network security

- All inter-region traffic via Transit Gateway (no public internet)
- VPC endpoints for S3, DynamoDB, SSM — no NAT gateway for control plane traffic
- Security groups: principle of least port — only explicitly required ports open
- All secrets in AWS Secrets Manager, rotated every 90 days

---

## 16. Dependency map

```
Phase 1 (this phase):
  Terraform modules → VPC, ALB, Route 53, Transit Gateway, Security Groups
  Monitoring stack  → Prometheus, Grafana, Alertmanager, Loki, Tempo

Phase 2 depends on Phase 1:
  eBPF probes       → VPC (EC2 instances), IAM roles
  Go agent skeleton → DynamoDB tables, SSM parameters

Phase 3 depends on Phase 1 + 2:
  ML runtime        → Feature store S3 bucket, Go agent telemetry channels
  MLflow            → RDS (tracking server), S3 (artifact store)

Phase 4 depends on Phase 1 + 2 + 3:
  Healing engine    → All ML models, DynamoDB lock table, Route 53

Phase 5 depends on Phase 1–4:
  Chaos suite       → All EC2 instances, healing engine, Go agent
  SLO engine        → Prometheus metrics, Alertmanager

Phase 6 depends on Phase 1–5:
  Runbook executor  → All healing engine APIs
  Postmortem engine → Incident store DynamoDB, Slack, PagerDuty, MLflow
```
