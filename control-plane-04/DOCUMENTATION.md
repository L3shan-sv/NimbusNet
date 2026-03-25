# NimbusNet Phase 4 — Technical Documentation
## Control Plane: Deep Reference

**Version:** 0.4.0
**Status:** Implementation Complete
**Dependencies:** Phase 1 (infrastructure), Phase 2 (eBPF agent), Phase 3 (ML scoring)

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Healing State Machine — Deep Dive](#2-healing-state-machine--deep-dive)
3. [Adaptive Traffic Shaper — Deep Dive](#3-adaptive-traffic-shaper--deep-dive)
4. [Route 53 Two-Plane Integration](#4-route-53-two-plane-integration)
5. [VPC Route Table Data Plane](#5-vpc-route-table-data-plane)
6. [Split-Brain Safety](#6-split-brain-safety)
7. [Anti-Flapping Design](#7-anti-flapping-design)
8. [ML Signal Integration](#8-ml-signal-integration)
9. [SLO Burn Rate Tracking](#9-slo-burn-rate-tracking)
10. [Prometheus Metrics Specification](#10-prometheus-metrics-specification)
11. [Operational Runbook: Phase 4](#11-operational-runbook-phase-4)
12. [Failure Modes & Mitigations](#12-failure-modes--mitigations)
13. [Integration Contract for Phase 5](#13-integration-contract-for-phase-5)

---

## 1. System Overview

The control plane is the enforcement layer between ML signals and infrastructure reality. It answers three questions on every 50ms window:

1. **Should we act?** — confidence threshold + anti-flapping
2. **How fast?** — FSM state determines soft (2min decay) vs hard (immediate) failover
3. **What exactly?** — Route 53 drain (DNS plane) + route table update (data plane)

### Concurrency Model

One `Controller` instance per agent process. One `RegionFSM` per region (3 total). All FSM mutations are serialised via mutex — only one goroutine drives state transitions at a time. The shaper runs its own tick goroutine; all other AWS API calls happen synchronously in the controller's event loop.

This simplicity is intentional. The control plane makes infrastructure changes — these must be linearisable. The performance cost of serialisation is negligible (FSM transitions are rare; the hot path is the `processScoredMetric` no-op path when signals are below threshold).

---

## 2. Healing State Machine — Deep Dive

### Formal Transition Table

```
(HEALTHY,   ANOMALY_DETECTED)   → DEGRADED
(HEALTHY,   SEVERITY_CRITICAL)  → DEGRADED
(HEALTHY,   MANUAL_OVERRIDE)    → DEGRADED

(DEGRADED,  RUNBOOK_STARTED)    → HEALING
(DEGRADED,  TTL_EXPIRED)        → HEALING   ← auto-start runbook after 5m
(DEGRADED,  MANUAL_OVERRIDE)    → FAILED    ← SRE can force-fail

(HEALING,   RUNBOOK_SUCCEEDED)  → RECOVERED
(HEALING,   RUNBOOK_FAILED)     → FAILED
(HEALING,   TTL_EXPIRED)        → FAILED    ← healing took > 10m
(HEALING,   MANUAL_OVERRIDE)    → FAILED

(RECOVERED, HEALTH_VERIFIED)    → HEALTHY   ← normal recovery path
(RECOVERED, TTL_EXPIRED)        → HEALTHY   ← observation window elapsed
(RECOVERED, ANOMALY_DETECTED)   → DEGRADED  ← rollback: metrics worsened
(RECOVERED, RECOVERY_ROLLBACK)  → DEGRADED

(FAILED,    MANUAL_OVERRIDE)    → HEALTHY   ← SRE marks resolved
(FAILED,    TTL_EXPIRED)        → DEGRADED  ← auto-retry after 30m
```

### Why These Specific Timeouts?

**DEGRADED → HEALING (5 minutes):**
The runbook executor (Phase 5) should start within seconds of DEGRADED. The 5-minute TTL is a safety net — if Phase 5 is itself failing, the FSM auto-starts healing after 5 minutes. This prevents indefinite stuck-in-DEGRADED scenarios.

**HEALING → FAILED (10 minutes):**
A runbook that can't heal within 10 minutes is either wrong (wrong runbook for this fault type) or the fault is too severe. At 10 minutes, the SRE is better positioned to help than the automation.

**RECOVERED → HEALTHY (60 seconds):**
The observation window exists because metrics can look good immediately after healing while the underlying issue is still resolving. 60 seconds of clean metrics is sufficient confidence that healing is genuine.

**FAILED → DEGRADED (30 minutes):**
30 minutes gives the SRE time to diagnose and resolve the issue manually. After 30 minutes, the system assumes either: (a) the SRE resolved it but forgot to clear the FAILED state, or (b) the fault resolved itself. Either way, a fresh DEGRADED state is appropriate.

### Action Failure Handling

Exit actions (OnExit) log failures but don't block transitions. A failed exit action is a problem but not catastrophic — we should still move to the next state.

Enter actions (OnEnter) propagate failures. If the Route 53 drain fails on DEGRADED entry, the error is returned to the caller. The FSM has already transitioned — we don't roll back (AWS API failures are rare and retrying the same transition would be wrong). The error is logged and the next Prometheus scrape will alert.

---

## 3. Adaptive Traffic Shaper — Deep Dive

### Weighted ECMP Approximation

AWS VPC route tables don't support weighted ECMP natively. We approximate it with subnet-level routing:

**Example:** 10 subnets, weights {us-east-1: 0.5, us-west-2: 0.3, eu-west-1: 0.2}

```
Subnet assignment:
  subnet-0 → rtb-use1 (→ TGW us-east-1)
  subnet-1 → rtb-use1
  subnet-2 → rtb-use1
  subnet-3 → rtb-use1
  subnet-4 → rtb-use1   ← 5 subnets = 50%
  subnet-5 → rtb-usw2
  subnet-6 → rtb-usw2
  subnet-7 → rtb-usw2   ← 3 subnets = 30%
  subnet-8 → rtb-euw1
  subnet-9 → rtb-euw1   ← 2 subnets = 20%
```

When weights change (shaper tick every 5s), subnets are reassigned. The AssociateRouteTable API call takes ~100ms per subnet. With 10 subnets, weight redistribution takes ~1s. This is fast enough for the 5s tick interval.

### Decay Mathematics

For a soft decay from weight W to minimum M over period T:

```
rate_per_sec = -(W - M) / T
weight(t) = W + rate_per_sec × t
weight reaches M when t = T
```

Example: W=0.5, M=0.05, T=120s (2 minutes):
```
rate_per_sec = -(0.5 - 0.05) / 120 = -0.00375/s
weight(60s) = 0.5 + (-0.00375)(60) = 0.275
weight(120s) = 0.5 + (-0.00375)(120) = 0.05  ← minimum reached
```

### Minimum Weight Floor

The 5% floor (MinimumWeight=0.05) is the most important design decision in the shaper. Without it:
- A region drops to 0% weight
- The bandit stops receiving observations for that region
- Recovery detection depends entirely on active health checks
- If health checks have a bug, we never detect recovery

With 5% floor:
- The bandit always receives RTT observations from the region
- Recovery is detected by the ML layer within one aggregation window (50ms)
- The SLO impact of the 5% floor is minimal (5% of traffic on a degraded path)

The SRE (via ManualOverride) can set weight to 0.0 for confirmed total failures. This is a deliberate human decision, not an automated one.

---

## 4. Route 53 Two-Plane Integration

### Health Check Association Pattern

NimbusNet uses Route 53 **latency-based routing** with health check associations. Each region has:
1. A latency record pointing to its ALB
2. A health check polling `/healthz` on the Go agent (Phase 2)
3. When the health check fails → R53 stops routing to that region automatically

The NimbusNet control plane doesn't need to disable the record directly — it just needs to make the health check fail. This is done by the Phase 2 agent's `/healthz` returning 503 when draining.

The `DrainRegion()` call in route53/client.go is an explicit UPSERT that reinforces the health check association and leaves a comment in the R53 change log for audit trails.

### Convergence Timeline

```
T=0     FSM: HEALTHY → DEGRADED
        Data plane: route table updated (< 500ms)
        DNS plane: /healthz begins returning 503

T=10s   R53 health check fires → sees 503
        (first failure — not enough to mark unhealthy)

T=20s   R53 health check fires again → sees 503
        (second consecutive failure — region marked unhealthy)
        R53 stops including region in latency routing responses

T=30s   DNS TTL expires for clients that cached the failed region's IP
        Clients switch to healthy region on next DNS query

T=35s   All new connections are routing to healthy regions
        Phase 2 agent drain_delay_ms expires → process can exit

T=35s   Old connections that were in-flight have completed
        (or timed out and reconnected to healthy region)
```

### The 30-Second DNS TTL

The TTL of 30 seconds is a balance:
- Too short (< 10s): DNS resolver overhead, increased query rate, cost
- Too long (> 60s): clients are stuck on failed region too long after failover

30 seconds means the worst-case DNS plane convergence is 20s (health check) + 30s (TTL) = 50s. The `drain_delay_ms: 35000` in the Phase 2 agent config covers this.

---

## 5. VPC Route Table Data Plane

### Transit Gateway Routing

In the NimbusNet topology, inter-region traffic flows through Transit Gateway attachments:

```
us-east-1 VPC
  Subnet A → Route Table use1
              10.2.0.0/16 via tgw-attach-usw2   ← us-west-2 traffic
              10.3.0.0/16 via tgw-attach-euw1   ← eu-west-1 traffic

On us-west-2 failure:
  Subnet A → Route Table use1
              10.2.0.0/16 via tgw-attach-euw1   ← REPLACED (< 500ms)
              10.3.0.0/16 via tgw-attach-euw1   ← unchanged
```

The `ReplaceRoute` EC2 API call is the fastest infrastructure change available in AWS. It typically completes in 100-300ms. With parallelisation across multiple route tables, a complete failover can be achieved in under 500ms.

### Route Table Idempotency

`replaceRoute()` first tries `ReplaceRoute`. If the route doesn't exist (e.g., first deployment), it falls back to `CreateRoute`. Both operations are idempotent — calling them multiple times with the same parameters has no additional effect.

This means the control plane can safely retry failed route table updates without risk of double-applying.

---

## 6. Split-Brain Safety

### The Race Condition

Consider two agents simultaneously detecting the same regional failure:

```
t=0ms:  Agent US-East: MLScorer returns should_trigger_failover=true for us-west-2
t=0ms:  Agent EU-West: MLScorer returns should_trigger_failover=true for us-west-2

t=10ms: Agent US-East: FSM transitions HEALTHY→DEGRADED
t=10ms: Agent EU-West: FSM transitions HEALTHY→DEGRADED

t=15ms: Agent US-East: FailoverRegion(us-west-2) called
t=15ms: Agent EU-West: FailoverRegion(us-west-2) called

Without lock: Both agents update route tables simultaneously
              us-west-2's routes are set to different next-hops by each agent
              Route table is in an inconsistent state
```

### The DynamoDB Solution

```
t=15ms: Agent US-East: PutItem(lock_key="drain:us-west-2", condition: not_exists)
        → SUCCEEDS. Proceeds with FailoverRegion.

t=15ms: Agent EU-West: PutItem(lock_key="drain:us-west-2", condition: not_exists)
        → ConditionalCheckFailedException.
        → acquireLock() returns (false, nil).
        → DrainRegion() logs "another agent is draining" and returns nil.
```

The second agent's no-op is correct — the first agent is handling the failover. Both agents' FSMs are in DEGRADED state independently (each manages its own local FSM), but only one makes the infrastructure change.

### Lock Expiry

The lock has a 60-second TTL stored as a DynamoDB attribute. The condition expression checks `expires_at < :now`. If the locking agent crashes mid-failover, the lock auto-expires after 60 seconds and the next agent can acquire it.

---

## 7. Anti-Flapping Design

### The Problem

Network metrics are noisy. A single 50ms window with high RTT is almost always transient — TCP backoff, GC pause, momentary congestion. Triggering a failover on a single anomalous window would cause constant flapping.

### Three Layers of Protection

**Layer 1: ML confidence threshold (65%)**
`failover_confidence < 0.65` → no action. The ensemble decision logic in Phase 3 already requires multiple models to agree. This adds a confidence floor on top.

**Layer 2: CRITICAL signal counting (3 consecutive)**
CRITICAL-severity signals require 3 consecutive windows (150ms) before triggering. Any nominal signal in between resets the counter. This eliminates single-spike false positives.

**Layer 3: FSM state gating**
`EventAnomalyDetected` only has a defined transition from HEALTHY. If the region is already DEGRADED, additional anomaly signals are silently ignored — the FSM is already acting.

### Why Not a Time Window?

"3 consecutive signals" is more robust than "3 signals in 60 seconds" because:
- Consecutive signals indicate sustained degradation
- Non-consecutive signals with gaps suggest intermittent issues (don't warrant failover)
- Time windows can be gamed by alternating good/bad signals

---

## 8. ML Signal Integration

### Signal → Action Mapping

| ML Signal | Condition | Action |
|-----------|-----------|--------|
| `should_trigger_failover=true` + `confidence >= 0.65` | FSM in HEALTHY | HEALTHY → DEGRADED |
| `xgboost_severity=CRITICAL` + `confidence > 0.70` | 3 consecutive | HEALTHY/DEGRADED → DEGRADED (hard path) |
| `should_trigger_failover=false` + `if_score < 0.30` | FSM in RECOVERED | RECOVERED → HEALTHY |
| Any signal | FSM in HEALING/FAILED | Ignored (FSM is already acting) |

### Bandit Weight Updates

Every scored metric updates the shaper's bandit advisory weights. This is the only path where ML continuously influences routing without requiring a state transition. The shaper uses these weights for minor adjustments to healthy regions (± 20% from baseline) without triggering the FSM.

---

## 9. SLO Burn Rate Tracking

### Google Multi-Window Model

Phase 4 publishes the raw availability signals that Phase 1's Prometheus rules convert into multi-window burn rates:

```
nimbusnet_controlplane_region_fsm_state == 0 → region is serving (HEALTHY)
nimbusnet_controlplane_region_fsm_state > 0  → region is partially unavailable

Error rate for SLO = weighted_sum(failed_regions) / total_regions
```

### Three Windows

| Window | Burn Rate Multiplier | Alert Meaning |
|--------|---------------------|---------------|
| 1 hour | 14.4× | Consuming monthly budget 14× faster than sustainable |
| 6 hours | 6× | Sustained degradation over hours |
| 72 hours | 1× | Chronic low-level issues |

The 1-hour fast-burn alert (`> 14.4`) fires when a region has been DEGRADED/FAILED for `> 4.2 minutes` in the last hour. This is what would page an SRE at 3am.

---

## 10. Prometheus Metrics Specification

### Alert Rules (Alertmanager Integration)

```yaml
# FSM in FAILED state → immediate PagerDuty
- alert: NimbusNetRegionFailed
  expr: nimbusnet_controlplane_region_fsm_state == 4
  for: 0m
  labels:
    severity: critical
  annotations:
    summary: "Region {{ $labels.region }} has FAILED"

# Region degraded > 5 minutes → warning
- alert: NimbusNetRegionDegradedTooLong
  expr: nimbusnet_controlplane_region_fsm_time_in_state_seconds{state="DEGRADED"} > 300
  for: 1m
  labels:
    severity: warning

# Route table update failing
- alert: NimbusNetRouteTableUpdateFailing
  expr: rate(nimbusnet_controlplane_route_table_update_total{result="error"}[5m]) > 0.1
  labels:
    severity: warning
```

---

## 11. Operational Runbook: Phase 4

### RB-P4-001: Region Stuck in DEGRADED

**Symptom:** `nimbusnet_controlplane_region_fsm_time_in_state_seconds{state="DEGRADED"} > 600`

**Cause candidates:**
1. Phase 5 runbook executor not receiving DEGRADED signal
2. Runbook executor failing to call `NotifyRunbookStarted()`
3. Network partition between control plane and Phase 5

**Resolution:**
```bash
# Check Phase 5 connection
curl http://phase5-sre-ops:8002/health

# Manual FSM nudge — start healing manually
curl -X POST http://localhost:9091/admin/fsm/heal \
  -d '{"region": "us-west-2", "event": "RUNBOOK_STARTED"}'

# Or force FAILED to page SRE
curl -X POST http://localhost:9091/admin/fsm/override \
  -d '{"region": "us-west-2", "target_state": "FAILED"}'
```

### RB-P4-002: Route Table Update Failing

**Symptom:** `rate(nimbusnet_controlplane_route_table_update_total{result="error"}[5m]) > 0`

**Cause candidates:**
1. IAM permission missing `ec2:ReplaceRoute`
2. Wrong TGW attachment ID in config
3. Route table ID doesn't exist in this region

**Resolution:**
```bash
# Check IAM
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::ACCOUNT:role/NimbusNetControlPlane \
  --action-names ec2:ReplaceRoute

# Verify TGW attachment
aws ec2 describe-transit-gateway-attachments \
  --filters Name=transit-gateway-attachment-id,Values=tgw-attach-...

# Update config with correct IDs
# Restart control plane — it will re-attempt on next shaper tick
```

### RB-P4-003: DynamoDB Lock Contention High

**Symptom:** `rate(nimbusnet_controlplane_dynamo_lock_acquire_total{result="contended"}[5m]) > 0.5`

**This is expected during a failover** — two agents racing is the designed behaviour. If sustained beyond 2 minutes, investigate:

```bash
# Check lock table for stuck locks
aws dynamodb scan --table-name nimbusnet-control-plane-locks

# Manually delete a stuck lock (only if agent that holds it is dead)
aws dynamodb delete-item \
  --table-name nimbusnet-control-plane-locks \
  --key '{"lock_key": {"S": "drain:us-west-2"}}'
```

---

## 12. Failure Modes & Mitigations

| Failure | Impact | Mitigation |
|---------|--------|-----------|
| Control plane crashes during failover | Route table partially updated | Idempotent API calls; next restart re-applies correct state from FSM |
| DynamoDB unavailable | Lock acquisition fails; both agents may race | `acquireLock` fails open — both agents proceed. Last-write-wins for route table. Acceptable. |
| Route 53 API throttled | DNS plane drain delayed | Exponential backoff (AWS SDK default); data plane already healed |
| EC2 API throttled | Route table update delayed | Data plane update fails; DNS plane still drains via /healthz 503 |
| Phase 3 ML service unavailable | No scored metrics | FSM stays in current state; timeout-based escalation still fires at TTL |
| FSM stuck (software bug) | Manual override required | SRE can send EventManualOverride via admin endpoint |

---

## 13. Integration Contract for Phase 5

Phase 5 (SRE Operations Layer) integrates with Phase 4 via three method calls:

```go
// Called when Phase 5 runbook begins execution:
controller.NotifyRunbookStarted(ctx, region)
// → FSM: DEGRADED → HEALING

// Called when Phase 5 runbook verifies healing success:
controller.NotifyRunbookSucceeded(ctx, region)
// → FSM: HEALING → RECOVERED

// Called when Phase 5 runbook exhausts all retries:
controller.NotifyRunbookFailed(ctx, region)
// → FSM: HEALING → FAILED → PagerDuty fires

// Called when SRE marks incident resolved:
controller.ManualOverride(ctx, region, statemachine.StateHealthy)
// → FSM: FAILED → HEALTHY
```

Phase 5 also reads `controller.AllRegionStatus()` to populate postmortem templates with the full FSM transition history, time spent in each state, and failover count.

The FSM transition history (`fsm.TransitionHistory()`) is the source of truth for all timing information in the postmortem: when the anomaly was detected, how long healing took, how many times the region has failed in the past 30 days.
