# NimbusNet Phase 6 — Technical Documentation

## 1. Design Philosophy

Phase 6 enforces a single principle: **reliability is a property of the codebase, not the infrastructure**. If a code change causes the healing pipeline to regress by 200ms, that change does not ship. The chaos gate makes this mechanical — no human needs to remember to run chaos tests, and no discipline is required to block a bad merge.

The second principle is **zero-waste instrumentation**: the same tc qdisc sessions that test the system generate labeled training data for the ML layer. A chaos run that costs 5 minutes of CI time simultaneously improves the Isolation Forest, XGBoost, and LSTM models. One operation, two outputs.

---

## 2. tc qdisc Injection Deep Dive

### Why tc qdisc / netem

Linux Traffic Control (tc) with the Network Emulator (netem) discipline is the standard tool for network fault injection at the kernel level. It operates at the egress queueing layer — after routing decisions, before packets hit the NIC. This means:

- Faults affect actual TCP/IP flows, not simulated traffic
- The XDP/eBPF layer (Phase 2) observes the same anomalies it would see in production
- No application-layer shims or proxy overhead

### Injection isolation in CI

The injector never runs against the real host interface in CI. Instead, a veth pair is created inside a network namespace:

```
host namespace:  veth0 (tc rules applied here)
                   ↕
chaos namespace: veth1
```

This means a chaos run cannot affect GitHub Actions runner connectivity, even if the qdisc cleanup step fails. The namespace is deleted at job end regardless.

### Fault command construction

For netem-based faults, the injector builds a single `tc qdisc add dev <iface> root netem` command with the relevant parameters appended. This is intentional — netem processes all parameters atomically. A combined fault (latency + loss) is a single qdisc entry, not two layered qdiscs.

For bandwidth caps, TBF (Token Bucket Filter) is used instead of netem, as netem's rate limiting has different semantics.

### Ramp mode

The `ramp_seconds` parameter (default 5s) is reserved for future implementation using `tc qdisc change` to gradually increase the fault severity. Currently, faults are applied instantly on inject(). Gradual onset is planned for Phase 6.1 to better simulate real-world degradation patterns.

---

## 3. Gate Threshold Rationale

### P99 healing latency < 30s

The 30-second P99 threshold is derived from the Route 53 TTL architecture. The DNS plane has a minimum propagation time of ~15 seconds (R53 minimum TTL). The data plane (VPC route table) heals in <500ms (Phase 4). End-to-end healing P99 should be dominated by DNS propagation, not detection or decision time. A 30s ceiling gives 2x headroom over the theoretical minimum.

If P99 exceeds 30s, either:
- Detection is slow (eBPF anomaly scoring issue, Phase 2)
- The FSM is flapping (Phase 4 anti-flap thresholds too high)
- Route 53 is experiencing elevated propagation latency (external)

### 200ms regression threshold

Healing latency is measured against the baseline from the last clean main-branch run. A 200ms regression is chosen because:
- It exceeds normal measurement noise (±50ms from qdisc timing)
- It is large enough to signal a real algorithmic regression
- It is small enough to catch regressions before they accumulate into SLO breaches

### 95% heal rate

5% of scenarios are allowed to not auto-heal. This accommodates:
- Scenarios where the fault duration exceeds the healing timeout (expected)
- Transient CI environment issues (network namespace teardown races)
- The `packet_loss_severe` scenario in GameDay, which is designed to not auto-heal (expected_heal=False)

A heal rate below 95% on the CI suite (which excludes expected non-heal scenarios) indicates the autonomous remediation pipeline is broken.

### 80% error budget freeze

The 80% freeze threshold is derived from Google's SRE burn rate model. At 80% budget consumed with 30% of the window remaining, the burn rate would need to drop to ~0.3x normal to avoid exhaustion — effectively zero incidents. At that point, deploying new code is higher variance than the expected benefit. The freeze forces engineering attention to reliability before feature work resumes.

---

## 4. Flywheel Label Schema

Each chaos session produces a JSONL record with this structure:

```json
{
  "session_id": "a3f2b1c4",
  "source": "chaos_qdisc",
  "profile_name": "high_latency",
  "fault_type": "latency",
  "latency_ms": 200,
  "jitter_ms": 0,
  "loss_pct": 0.0,
  "bandwidth_kbps": 0,
  "duration_seconds": 120,
  "healing_triggered": true,
  "healing_latency_ms": 18420.0,
  "resolution": "HEALED",
  "severity_label": "DEGRADED",
  "fault_class_label": 1,
  "sample_count": 24,
  "timestamp": 1711234567.0
}
```

The `fault_class_label` (0–3) maps to:
- 0: WARNING (low packet loss)
- 1: DEGRADED (latency, jitter, reorder)
- 2: DEGRADED-HIGH (bandwidth cap, corruption)
- 3: CRITICAL (severe loss, combined faults)

This matches the XGBoost 4-class severity taxonomy from Phase 3. The bridge (`flywheel/bridge.py`) transforms this into the Phase 3 feature vector format before ingestion.

---

## 5. GameDay Session Design

### Why multi-phase scenarios

The CI suite uses single-fault profiles because it must complete in under 10 minutes. GameDay sessions use multi-phase profiles because:

1. **Sequence detection**: The LSTM model detects failure patterns, not individual anomalies. A cascading failure (latency → jitter → loss) has a different sequence signature than a sudden combined fault. Multi-phase scenarios generate these sequences.

2. **Anti-flapping validation**: The FSM anti-flapping mechanism (Phase 4) requires the system to observe 3 consecutive CRITICAL signals before triggering failover. The `flapping_interface` scenario specifically tests this — rapid fault/recovery cycles that should not trigger unnecessary failovers.

3. **Flywheel richness**: A multi-phase scenario with 4 phases generates 4 labeled records, each with different fault parameters but correlated timing. This is impossible to generate synthetically.

### SRE observation protocol

GameDay always notifies SRE before start. During the session:
- SRE observes the Grafana dashboards live
- PagerDuty pages are suppressed for planned GameDay scenarios
- SRE documents observations for the GameDay report postmortem
- SRE can abort any scenario via `nimbusnet chaos abort`

After the session:
- GameDay report is written to `/tmp/nimbusnet/chaos/gameday/`
- Slack summary posted to `#nimbusnet-sre`
- Any failed scenarios trigger postmortem generation (Phase 5)

---

## 6. Baseline Management

The healing latency baseline is the reference point for regression detection. It is:
- Stored in `/tmp/nimbusnet/chaos/baseline.json` (cached in GitHub Actions between runs)
- Updated only on a successful CI suite run on the `main` branch
- Keyed by profile name: `{"high_latency": 18420.0, "packet_loss_5pct": 9240.0, ...}`

A PR run compares against the baseline from the last successful main-branch run. If no baseline exists (first run or cache miss), regression checking is skipped — only absolute P99 and heal rate gates apply.

---

## 7. Prometheus Metrics Emitted

Phase 6 exposes the following metrics for Grafana integration:

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `nimbusnet_chaos_sessions_total` | counter | profile, resolution | Total injection sessions |
| `nimbusnet_chaos_healing_latency_ms` | histogram | profile | Healing latency distribution |
| `nimbusnet_chaos_heal_rate` | gauge | suite | Heal rate for last suite run |
| `nimbusnet_chaos_regression_ms` | gauge | profile | Latency regression vs baseline |
| `nimbusnet_chaos_gate_passed` | gauge | suite | 1 if gate passed, 0 if failed |
| `nimbusnet_chaos_flywheel_records_total` | counter | source | Records pushed to ML flywheel |
| `nimbusnet_error_budget_consumed_pct` | gauge | slo | Error budget consumed this window |

---

## 8. Operational Runbooks

### RB-CHAOS-001: Chaos Gate Failing on Clean Code

**Symptoms**: CI chaos gate fails with healing latency regression on a PR that doesn't touch healing logic.

**Investigation**:
1. Check baseline staleness: `cat /tmp/nimbusnet/chaos/baseline.json`
2. If baseline > 7 days old, invalidate and re-establish: delete cache in GitHub Actions
3. Check Phase 4 FSM logs for unexpected state transitions during the chaos run
4. Check Phase 2 eBPF agent — if the Go agent was restarted during the chaos run, healing latency will be inflated

**Resolution**: If the baseline is stale, trigger a manual baseline update from a known-good commit on main.

### RB-CHAOS-002: Deploy Freeze Due to Budget Exhaustion

**Symptoms**: `ci/budget_enforcer.py` returns FREEZE. All PR merges blocked.

**Investigation**:
1. Read `error_budget.json`: `cat /tmp/nimbusnet/sre/error_budget.json`
2. Identify which incidents consumed the budget (Phase 5 incident log)
3. Review postmortems for the P0/P1 incidents in this window

**Resolution**:
1. Complete postmortem review for all P0/P1 incidents
2. Verify all action items from postmortems are tracked
3. SRE manually unfreezes: `nimbusnet budget unfreeze --reason "postmortems complete, action items tracked"`
4. Monitor burn rate for 24h before returning to normal deploy cadence

### RB-CHAOS-003: Flywheel Bridge Not Ingesting

**Symptoms**: Flywheel records accumulating in `/tmp/nimbusnet/flywheel/chaos/` but not appearing in Phase 3 training store.

**Investigation**:
1. Check Phase 3 ML serving is healthy: `curl http://localhost:8080/models/health`
2. Check bridge processed log: `cat /tmp/nimbusnet/flywheel/processed.jsonl | tail -20`
3. If ML serving is down, bridge will retry on next poll — records are not lost

**Resolution**: Restart Phase 3 ML serving. Bridge will ingest accumulated records within 10s of serving recovery.

---

## 9. Phase 7 Integration

Phase 7 (Integration & Hardening) will:
1. Wire the chaos gate into the complete deploy pipeline (not just PR CI)
2. Add chaos scenarios for Phase 3 ML model staleness detection
3. Implement the `nimbusnet chaos abort` command for GameDay session control
4. Add Grafana dashboard panels for chaos gate trends across PRs
5. Implement ramp mode for gradual fault onset
