# NimbusNet Phase 7 — Technical Documentation

## 1. Integration Contract Design

### Why formal contracts

Every distributed system has implicit contracts between its components. eBPF emits a struct — ML expects specific fields. ML emits severity labels — FSM expects specific event names. When these implicit contracts drift, the system breaks silently. A metric stops being populated. A severity label hits an unhandled branch. The system looks healthy in Grafana while the healing pipeline has been broken for weeks.

Phase 7 makes these contracts explicit, checked, and blocking. The contract validator runs at startup before any traffic flows. A FATAL contract failure is a hard stop — the operator learns about the broken integration before a real incident exposes it.

### Contract taxonomy

**FATAL** — The system cannot function correctly without this contract. A broken FATAL contract means the healing pipeline will fail silently during a real incident. These always block startup.

**ERROR** — A component is missing or misconfigured, but the system can operate in a degraded mode. For example, if the postmortem writer cannot reach the ML flywheel, the system still heals — it just doesn't generate training data from the incident. Logged loudly, does not block startup.

**WARN** — A configuration is missing that would improve observability or operations but is optional. For example, the Grafana Prometheus datasource not being provisioned doesn't prevent healing — it just means operators can't see dashboards. Logged, never blocks.

### Contract validation in CI

The contract validator is designed to run in two modes:

**Schema-only mode** (CI, no services running): validates data structure compatibility — field names, enum values, integer ranges. Does not require any service to be running. This is what runs in the `contracts` CI job.

**Live mode** (integration testing, docker-compose full stack): additionally validates that services are reachable, Prometheus scrape targets respond, and Grafana datasources are configured. This is what runs in the E2E job.

---

## 2. E2E Test Design Principles

### Dependency-ordered execution

The test suite runs in dependency order and skips downstream tests if upstream phases fail. This prevents cascading false negatives:

If the ML serving layer is down, there is no point running the FSM tests (they'd fail because the ML→FSM signal is broken) or the SRE tests (they'd fail because incidents aren't triggered). The skip-on-upstream-failure pattern gives the operator a clean signal: "ML serving is down — fix that first."

### Skip vs Fail

A `SKIP` result means the test could not be run (service unavailable, dry-run mode) and the pass/fail gate is not affected. A `FAIL` result means the test ran and the contract was violated. The CI gate only fails on `FAIL` results.

This is intentional: in local development, most services won't be running. Running `python -m e2e.test_suite --dry-run` should produce all SKIPs, not all FAILs. The gate only fires in CI with the full docker-compose stack.

### The drain mode toggle test

This test specifically validates the Route 53 DNS poisoning mechanism:
1. POST /drain to the eBPF agent
2. Verify /healthz returns 503 (Route 53 health check fails → DNS drains)
3. POST /undrain
4. Verify /healthz returns 200 (Route 53 health check passes → DNS restores)

This is the most important test in the suite because it validates the mechanism that makes the two-plane design work. If this test fails, the data plane can heal (VPC route table) but the DNS plane cannot drain, meaning old DNS records will keep routing traffic to a degraded region for the full TTL.

---

## 3. GameDay Controller Signal Protocol

The GameDay controller uses a simple file-based signal protocol rather than a network API. This is intentional:

- **No port needed** — no firewall rules, no service discovery
- **Crash-safe** — if the orchestrator crashes, the signal file persists and the new process reads it on restart
- **Auditable** — the signal file is a JSON record with command, reason, and timestamp. It can be read by any operator without running any code

The signal flow:
```
SRE runs: python -m gameday_control.controller abort --reason "..."
  │
  ▼
Writes: /tmp/nimbusnet/gameday/signal.json
  {"command": "abort", "reason": "...", "ts": 1711234567.0}
  │
  ▼
GameDay orchestrator polls every 1s
  │
  ▼
On abort: clears qdisc, writes partial postmortem, notifies Slack
On pause: waits at phase boundary, sets state=PAUSED
On resume: continues from next phase, sets state=RUNNING
On skip:  clears current scenario, moves to next
```

The orchestrator never aborts mid-phase — it always completes the current tc qdisc injection phase before acting on the signal. This prevents leaving fault injection rules in an unknown state.

---

## 4. Grafana Dashboard Architecture

The dashboard is organized in 6 rows matching the 6 operational layers. The ordering is deliberate:

1. **SLO & Error Budget** — the highest-level view. An on-call engineer should see the error budget gauge first. If it's green, everything below is operational detail. If it's yellow/red, the rows below tell the story.

2. **eBPF Detection Layer** — the nervous system. Packets/s and anomaly score are the raw signals. An anomaly score spike here should precede a severity escalation in row 3 by 50–200ms.

3. **ML Intelligence** — the decision layer. Score decisions/s shows traffic volume. Inference latency P99 should stay under 10ms. Flywheel records show the learning rate.

4. **Control Plane** — the action layer. FSM state is the single most important indicator during an incident. Traffic weights show the real-time routing split.

5. **SRE Operations** — the human interface. Auto-heal rate is the headline metric for the SRE layer. If it drops below 95%, something in the runbook library or Phase 4 is broken.

6. **Chaos CI Gate** — the quality gate. Gate pass/fail and regression trend are the leading indicators of pipeline health. A healing latency regression in chaos predicts a real-incident regression within 1–2 deploys.

---

## 5. The Data Flywheel — Complete Picture

Every component in the system contributes to the ML training store. Here is the complete picture:

| Source | Trigger | Records/week (est.) | Label quality |
|--------|---------|--------------------|-|
| Bootstrap generator | First startup | 10,000 | Synthetic — good coverage, low realism |
| tc qdisc chaos (CI) | Every PR merge | ~35 (7 PRs × 5 scenarios) | Real healing traces, canonical fault types |
| tc qdisc chaos (GameDay) | Weekly | ~35 (7 scenarios × phases) | Rich multi-phase sequences, highest value |
| Production incidents (P0/P1) | As they occur | 1–5 | Real production patterns, rarest |
| Production incidents (P2/P3) | As they occur | 5–20 | High volume, good diversity |

The models retrain when the flywheel accumulates 50 new labeled records. On a team running 7 PRs/week and 1 GameDay/week, that's roughly:

- Bootstrap: one-time 10,000 record train (all 4 models)
- Week 1+: retraining every ~1.5 days as chaos records accumulate
- Production incidents: ad-hoc retraining when high-value records arrive

The LSTM and Multi-armed Bandit are excluded from batch retraining:
- **LSTM**: retrains offline with GPU on a weekly schedule (sequence modeling is expensive)
- **Bandit**: updates online on every routing decision (never needs batch retraining)

---

## 6. Production Readiness Checklist

Before considering NimbusNet production-ready, complete these items:

### Infrastructure (Phase 1)
- [ ] Terraform state backend configured in S3 with DynamoDB locking
- [ ] All IAM roles reviewed against principle of least privilege
- [ ] CloudTrail enabled on all regions
- [ ] VPC flow logs enabled and shipping to Loki

### eBPF Agent (Phase 2)
- [ ] BPF program compiled and tested against target kernel version
- [ ] perf ring buffer size tuned to traffic volume
- [ ] Agent runs as non-root with CAP_NET_ADMIN + CAP_BPF only
- [ ] Graceful drain tested end-to-end (503 → R53 drain → 200)

### ML Layer (Phase 3)
- [ ] Bootstrap training completed and model artifacts validated
- [ ] Flywheel S3 bucket configured with lifecycle policy
- [ ] Model retrain CI job tested end-to-end
- [ ] LSTM GPU retrain job scheduled

### Control Plane (Phase 4)
- [ ] Consistent hash ring tested with region failure simulation
- [ ] Route 53 TTL set to minimum (60s)
- [ ] DynamoDB conditional write tested for race condition (simultaneous triggers)
- [ ] Cold standby provisioning tested end-to-end in staging

### SRE Operations (Phase 5)
- [ ] PagerDuty integration tested (real page fired and acknowledged)
- [ ] Slack webhook configured for all three channels
- [ ] All three runbooks dry-run tested in staging
- [ ] Postmortem template reviewed by SRE team

### Chaos Gate (Phase 6)
- [ ] Baseline established from 3+ clean main-branch runs
- [ ] Error budget initial value set correctly
- [ ] GameDay session run in staging before production
- [ ] Flywheel S3 sync configured and tested

### Integration (Phase 7)
- [ ] All FATAL contracts pass in production environment
- [ ] E2E suite passing against production-like staging
- [ ] Grafana dashboard imported and datasource verified
- [ ] GameDay abort command tested
