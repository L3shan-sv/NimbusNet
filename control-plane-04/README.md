# NimbusNet — Phase 4: Control Plane

> **The muscle.** This is where detection becomes action. The healing state machine consumes ML signals from Phase 3 and translates them into real infrastructure changes — VPC route tables updated in milliseconds, Route 53 drained over 35 seconds. No binary flip. Traffic bleeds proportionally to degradation. The system heals itself before a human is ever involved.

---

## What This Phase Delivers

| Component | Description |
|-----------|-------------|
| `statemachine/fsm.go` | Formally specified healing FSM per region. Five states (HEALTHY/DEGRADED/HEALING/RECOVERED/FAILED), every transition guarded and timed, every state with a rollback path |
| `trafficshaper/shaper.go` | Maglev-inspired adaptive traffic shaper. Soft decay over 2 minutes on DEGRADED. Hard snap to minimum on FAILED. Linear ramp-back on RECOVERED. Minimum 5% floor — never loses signal |
| `route53/client.go` | DNS two-plane integration. DynamoDB conditional write lock prevents competing drains from dual-region agents. /healthz 503 → R53 stops routing within 35s |
| `routetable/manager.go` | VPC route table data plane. Sub-500ms failover by replacing TGW attachment routes across all affected subnets. Weighted ECMP approximation via subnet redistribution |
| `controller.go` | Central orchestrator. Wires FSM → shaper → Route53 → route table. Anti-flapping via consecutive-signal counting. Runbook integration hooks for Phase 5 |
| `metrics.go` | Full Prometheus coverage: FSM state, time-in-state, routing weights, R53 health check status, DynamoDB lock contention, SLO burn rate, ML signal confidence |
| `cmd/main.go` | Entrypoint. AWS SDK init, config loading, Prometheus server, graceful shutdown |
| `configs/controlplane.yaml` | Full annotated config reference |
| `Dockerfile` | Single-stage Go build + minimal runtime |

---

## The Two-Plane Healing Design

```
                    ML SIGNAL: should_trigger_failover = true
                                        │
                                        ▼
                              ┌─────────────────┐
                              │  Controller     │
                              │  Ingest()       │
                              └────────┬────────┘
                                       │
                            ┌──────────▼──────────┐
                            │    RegionFSM        │
                            │  HEALTHY→DEGRADED   │
                            └──────────┬──────────┘
                                       │
                    ┌──────────────────┴──────────────────┐
                    │                                     │
           DATA PLANE (fast)                    DNS PLANE (slow)
                    │                                     │
    VPC Route Table updated                  /healthz returns 503
    TGW attachment replaced              Route 53 health check fails
        < 500ms                          ~35s convergence (10s check
                                         interval + 30s DNS TTL)
                    │                                     │
                    └──────────────┬──────────────────────┘
                                   │
                         No traffic to failed region
                         (data plane: immediate)
                         (DNS plane: 35s)
```

The data plane heals first — no new packets route through the failed TGW attachment. The DNS plane heals second — no new connections are even attempted to the failed region after TTL expires.

---

## Healing State Machine

```
                    EventAnomalyDetected
                    EventSeverityCritical
    ┌─────────────────────────────────────────────────────┐
    │                                                     │
    ▼                                                     │
┌─────────┐  EventAnomalyDetected  ┌───────────┐         │
│ HEALTHY │ ──────────────────────►│ DEGRADED  │         │
└─────────┘                        └─────┬─────┘         │
    ▲                                    │ EventRunbookStarted     │
    │                                    │ or TTL expired          │
    │                                    ▼                         │
    │                              ┌───────────┐                   │
    │    EventHealthVerified        │  HEALING  │                   │
    │◄── (60s observation) ─────── └─────┬─────┘                   │
    │                                    │                         │
    │                    ┌───────────────┼────────────────┐        │
    │                    │               │                │        │
    │             EventRunbook    EventRunbook      EventTTL       │
    │             Succeeded       Failed            Expired        │
    │                    │               │                │        │
    │                    ▼               └────────────────┘        │
    │              ┌───────────┐                  │                │
    │              │ RECOVERED │                  ▼                │
    │◄─────────────┴───────────┘          ┌──────────────┐         │
    │   EventHealthVerified                │    FAILED    │         │
    │   EventTTLExpired                    └──────┬───────┘         │
    │   (observation window)                      │                │
    │                                             │ EventManualOverride
    └─────────────────────────────────────────────┘ (SRE resolves)
```

### State Timeouts (TTL escalation)

| State | Timeout | On Expiry |
|-------|---------|-----------|
| DEGRADED | 5 minutes | → HEALING (auto-start runbook) |
| HEALING | 10 minutes | → FAILED (healing took too long) |
| RECOVERED | 60 seconds | → HEALTHY (observation window passed) |
| FAILED | 30 minutes | → DEGRADED (auto-retry after SRE window) |

---

## Traffic Shaper Behaviour

### Soft Failover (DEGRADED state)
Traffic decays linearly from current weight to 5% (minimum) over **2 minutes**. No binary flip. Users see degraded but not broken service. Other regions absorb the shed traffic proportionally.

### Hard Failover (FAILED state)
Traffic snaps immediately to **5% minimum**. The VPC route table is simultaneously updated to reroute all flows. The 5% floor keeps the signal alive for recovery detection.

### Recovery Ramp (RECOVERED state)
Traffic ramps linearly from 5% back to 100% over **5 minutes**. If metrics worsen during ramp (EventRecoveryRollback), traffic decays back to minimum and the FSM returns to DEGRADED.

### Bandit Integration
The Phase 3 multi-armed bandit provides advisory weight recommendations every 50ms. These influence weight targets for healthy regions but **never override FSM state**. A region the FSM has marked FAILED stays at minimum weight regardless of the bandit's recommendation.

---

## Split-Brain Safety

**The problem:** Two agents (US-East and EU-West) both detect the same failure simultaneously and both try to drain Route 53 and update route tables. Competing writes corrupt the routing state.

**The solution:** DynamoDB conditional write lock.

```
Agent US-East:  PutItem(lock_key="drain:us-west-2", condition: attribute_not_exists)
                → SUCCEEDS. Acquires lock. Proceeds with drain.

Agent EU-West:  PutItem(lock_key="drain:us-west-2", condition: attribute_not_exists)
                → FAILS with ConditionalCheckFailedException.
                → Logs "another agent is draining". Returns nil. Does nothing.

Lock TTL:       60 seconds. Auto-expires if US-East crashes mid-drain.
```

The consistent hash ring (Phase 2) reduces the probability of this race to near zero for flow-level decisions. The DynamoDB lock is the safety net for region-level infrastructure changes.

---

## Anti-Flapping

A single anomalous 50ms window shouldn't trigger a failover. The controller counts consecutive CRITICAL signals per region:

```
Default threshold: 3 consecutive CRITICAL signals (= 150ms of sustained severity)

Signal 1: CRITICAL — criticalCounts[region]++ → 1/3
Signal 2: CRITICAL — criticalCounts[region]++ → 2/3
Signal 3: CRITICAL — criticalCounts[region]++ → 3/3 → TRIGGER FAILOVER
Any NOMINAL signal: criticalCounts[region] = 0  (reset)
```

For anomaly-only signals (not CRITICAL), the ensemble confidence threshold applies: `failover_confidence < 0.65` → monitor only, no action.

---

## Quick Start

```bash
# Build
docker build -t nimbusnet-controlplane:latest .

# Configure
cp configs/controlplane.yaml /etc/nimbusnet/controlplane.yaml
# Edit: region, hosted_zone_id, health_check_ids, tgw_attachment_ids, subnet_ids

# Run (requires AWS credentials with EC2, Route53, DynamoDB permissions)
docker run \
  -v /etc/nimbusnet:/etc/nimbusnet \
  -e AWS_REGION=us-east-1 \
  -p 9091:9091 \
  nimbusnet-controlplane:latest

# Check status
curl http://localhost:9091/metrics | grep nimbusnet_controlplane_region_fsm_state
```

---

## IAM Requirements

The control plane EC2 role requires these permissions:

```json
{
  "ec2:ReplaceRoute",
  "ec2:CreateRoute",
  "ec2:AssociateRouteTable",
  "ec2:DisassociateRouteTable",
  "ec2:DescribeRouteTables",
  "route53:ChangeResourceRecordSets",
  "route53:GetHealthCheckStatus",
  "dynamodb:PutItem",
  "dynamodb:DeleteItem",
  "cloudwatch:PutMetricData"
}
```

All permissions are scoped to NimbusNet resources via resource ARN conditions in the Phase 1 IAM Terraform.

---

## Key Metrics Reference

| Metric | Type | Alert Condition |
|--------|------|----------------|
| `nimbusnet_controlplane_region_fsm_state` | Gauge | `== 4` (FAILED) → PagerDuty |
| `nimbusnet_controlplane_region_fsm_time_in_state_seconds` | Gauge | DEGRADED > 300s → warning |
| `nimbusnet_controlplane_region_routing_weight` | Gauge | Any region `< 0.10` → warning |
| `nimbusnet_controlplane_region_failover_total` | Counter | Rate > 2/hour → warning |
| `nimbusnet_controlplane_r53_health_check_healthy` | Gauge | `== 0` → critical |
| `nimbusnet_controlplane_dynamo_lock_acquire_total{result="contended"}` | Counter | Rate > 5/min → investigate |
| `nimbusnet_controlplane_slo_budget_consumed_fraction{window="1h"}` | Gauge | `> 14.4` → fast-burn alert |

---

## File Tree

```
phase-04-control-plane/
├── README.md                              ← you are here
├── DOCUMENTATION.md                      ← deep technical reference
├── Dockerfile
├── controlplane/
│   ├── go.mod
│   ├── controller.go                     ← central orchestrator
│   ├── metrics.go                        ← Prometheus metric definitions
│   ├── cmd/
│   │   └── main.go                       ← entrypoint
│   ├── statemachine/
│   │   └── fsm.go                        ← 5-state healing FSM
│   ├── trafficshaper/
│   │   └── shaper.go                     ← adaptive ECMP weight management
│   ├── route53/
│   │   └── client.go                     ← DNS plane + DynamoDB lock
│   └── routetable/
│       └── manager.go                    ← VPC route table data plane
└── configs/
    └── controlplane.yaml                 ← annotated config reference
```

---

## What's Next — Phase 5

Phase 5 is the SRE Operations Layer — the runbook executor, postmortem generator, and escalation pipeline. It calls `controller.NotifyRunbookStarted/Succeeded/Failed()` to drive the FSM from DEGRADED→HEALING→RECOVERED. It generates postmortems from the FSM transition history. It pages SREs only when the FSM reaches FAILED.
