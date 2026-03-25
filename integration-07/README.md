# NimbusNet — Phase 7: Integration & Hardening

> The final phase. Everything built in Phases 1–6 is wired together, contract-validated, and hardened for production.

---

## What This Phase Does

Phase 7 closes the system. It provides three things:

1. **Integration contract validation** — every cross-phase dependency is formally specified and checked at startup. The system fails fast if any contract is broken.
2. **End-to-end test suite** — 25 tests covering every integration point from eBPF packet capture to postmortem generation, run on every push to main.
3. **Full system orchestration** — a single `docker-compose.full.yml` that wires all 7 phases together for local integration testing and CI.

---

## System Architecture — Complete View

```
┌─────────────────────────────────────────────────────────────────┐
│                        Internet / VPC Traffic                     │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │   Phase 2: eBPF/XDP  │  kernel-bypass telemetry
                    │   Go Agent           │  50ms perf ring drain
                    └──────────┬──────────┘
                               │ FlowEvent (typed channel)
                    ┌──────────▼──────────┐
                    │   Phase 3: ML Layer  │  Isolation Forest
                    │   FastAPI /score     │  LSTM · XGBoost
                    │   Flywheel /ingest   │  Thompson Bandit
                    └──────────┬──────────┘
                               │ severity signal
                    ┌──────────▼──────────┐
                    │   Phase 4: Control   │  FSM (5 states)
                    │   Plane              │  Consistent hash ring
                    │                      │  Maglev traffic shaper
                    │                      │  Route 53 two-plane
                    └──────┬───────┬───────┘
                           │       │
            ┌──────────────▼─┐   ┌▼─────────────────┐
            │  Phase 5: SRE  │   │  Phase 1: Obs.   │
            │  Runbook Engine│   │  Prometheus/Loki  │
            │  Postmortem Gen│   │  Grafana/Tempo    │
            │  PagerDuty/    │   │  Alertmanager     │
            │  Slack         │   └──────────────────┘
            └──────────────┬─┘
                           │ error budget state
            ┌──────────────▼──────────────┐
            │   Phase 6: Chaos CI Gate     │
            │   tc qdisc injection         │
            │   Flywheel bridge            │
            │   GitHub Actions gate        │
            └──────────────┬──────────────┘
                           │
            ┌──────────────▼──────────────┐
            │   Phase 7: Integration       │
            │   Contract validator         │
            │   E2E test suite             │
            │   Full docker-compose        │
            │   GameDay controller         │
            └──────────────────────────────┘
```

---

## Components

### `hardening/contract_validator.py`
Validates all 8 cross-phase integration contracts at startup. Contracts specify:
- eBPF telemetry schema → ML feature vector compatibility
- ML severity enum → FSM event type mapping
- FSM terminal states → runbook coverage
- Postmortem writer → flywheel ingest connection
- Chaos JSONL labels → ML fault class schema alignment
- Budget enforcer → Phase 5 state file location
- Prometheus scrape target reachability
- Grafana datasource configuration

Fatal contracts block startup. Error contracts allow degraded operation. Warn contracts are logged only.

### `e2e/test_suite.py`
25 integration tests in dependency order. Tests skip if their upstream phase failed — no cascading false negatives.

Test groups: observability → phase2_ebpf → phase3_ml → phase4_control → phase5_sre → phase6_chaos → integration_contracts

Key tests:
- eBPF drain mode toggle (503 during drain, 200 after undrain)
- ML score returns CRITICAL for critical feature vectors
- ML flywheel ingest accepts labeled records
- Incident lifecycle: create → runbook → resolve → postmortem
- Chaos dry-run CI suite completes without error
- Budget enforcer defaults to ALLOW with no state file
- SLO burn rate metrics present in Prometheus

### `gameday-control/controller.py`
Real-time GameDay session control:
- `abort --reason "production incident"` — stops session, clears qdisc, writes postmortem, pages SRE
- `pause --reason "SRE investigating"` — pauses after current phase
- `resume` — continues from pause
- `skip` — skips current scenario
- `status` — prints live session state

### `grafana/dashboards/nimbusnet-main.json`
Complete Grafana dashboard with 6 row sections:
- SLO & Error Budget (gauge + multi-window burn rate chart)
- eBPF Detection Layer (packets/s, anomaly score P99, RTT distribution)
- ML Intelligence (score decisions/s, inference latency P99, flywheel records)
- Control Plane & Traffic Shaper (FSM state, traffic weights, FSM transitions)
- SRE Operations & Incidents (incidents by severity, auto-heal rate, healing latency)
- Chaos CI Gate (gate pass/fail, heal rate, latency trend, regression)

### `docker-compose.full.yml`
Full system orchestration. All 7 phases, one command:
```bash
docker compose -f phase-07-integration/docker-compose.full.yml up -d
```

Services: prometheus, grafana, loki, tempo, alertmanager, ebpf-agent, ml-serving, control-plane, sre-ops, flywheel-bridge, contract-validator, e2e-tests.

---

## Running the Full System

```bash
# Start observability + ML + SRE (no privileged eBPF)
docker compose -f phase-07-integration/docker-compose.full.yml up -d \
  prometheus grafana loki alertmanager ml-serving sre-ops control-plane flywheel-bridge

# Run contract validation
docker compose -f phase-07-integration/docker-compose.full.yml run --rm contract-validator

# Run E2E tests
docker compose -f phase-07-integration/docker-compose.full.yml --profile test run --rm e2e-tests

# Full stack including eBPF agent (requires privileged / CAP_NET_ADMIN)
docker compose -f phase-07-integration/docker-compose.full.yml --profile full up -d

# Teardown
docker compose -f phase-07-integration/docker-compose.full.yml down -v
```

---

## CI Pipeline

`.github/workflows/integration-ci.yml` runs 5 jobs in dependency order:

```
contracts → phase-tests (parallel) → chaos-gate → e2e → update-baseline
```

| Job | When | Blocks |
|-----|------|--------|
| Contract validation | Every push/PR | All downstream jobs |
| Phase unit tests | Every push/PR | Chaos gate |
| Chaos CI gate | Every push/PR | E2E, baseline update |
| E2E suite | main push + manual | Baseline update |
| Update baseline | main push only | Nothing |

---

## GameDay Abort Protocol

During a live GameDay session, if a real production incident fires:

```bash
# SRE runs this immediately
python -m gameday_control.controller abort --reason "Production P0 in us-east-1 — pausing GameDay"
```

What happens within 5 seconds:
1. Signal file written to `/tmp/nimbusnet/gameday/signal.json`
2. GameDay orchestrator picks up signal on next poll
3. Current phase completes (no mid-phase abort — never leave qdisc rules dangling)
4. `tc qdisc del` clears all fault injection rules
5. Partial GameDay postmortem written with scenarios completed so far
6. Slack notification sent: "GameDay ABORTED — Production P0 in us-east-1"
7. SRE is free to focus on the incident

---

## Integration Contract Table

| Contract | Severity | Phase From | Phase To |
|----------|----------|------------|----------|
| eBPF telemetry schema → ML feature vector | FATAL | phase2 | phase3 |
| ML severity enum → FSM event types | FATAL | phase3 | phase4 |
| FSM terminal states → runbook coverage | FATAL | phase4 | phase5 |
| Postmortem writer → ML flywheel ingest | ERROR | phase5 | phase3 |
| Chaos JSONL labels → ML fault class schema | FATAL | phase6 | phase3 |
| Budget enforcer → Phase 5 state file | WARN | phase6 | phase5 |
| Prometheus scrape targets reachable | ERROR | observability | all |
| Grafana Prometheus datasource configured | WARN | observability | grafana |

---

## The Complete Phase Map

| Phase | What It Does | Key Files |
|-------|-------------|-----------|
| 1 | AWS infrastructure + LGTM observability stack | terraform/, docker-compose.yml |
| 2 | XDP packet capture + eBPF telemetry pipeline | bpf/xdp_probe.c, telemetry/bus.go |
| 3 | ML scoring + data flywheel + 4 models | scoring_pipeline.py, serving.py, flywheel.py |
| 4 | FSM + consistent hash ring + traffic shaper + Route 53 | statemachine/fsm.go, controller.go |
| 5 | Runbooks + postmortems + escalation + PagerDuty | executor.py, generator.py, notifier.py |
| 6 | tc qdisc chaos injection + CI gate + flywheel bridge | qdisc_injector.py, chaos_runner.py |
| 7 | Contract validation + E2E tests + full orchestration | contract_validator.py, test_suite.py |

**NimbusNet is complete.**
