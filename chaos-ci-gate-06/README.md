# NimbusNet — Phase 6: Chaos CI Gate

> Reliability regressions block merges. Error budget exhaustion freezes deploys. Chaos engineering is not optional — it runs on every PR.

---

## What This Phase Does

Phase 6 closes the reliability loop. Every component built in Phases 1–5 gets continuously stress-tested through a CI gate that runs on every PR merge. Failures block merges. The same test sessions that gate deployments also generate labeled training data for the Phase 3 ML flywheel — one operation, two outputs.

---

## Architecture

```
PR Merge / Schedule
       │
       ▼
┌──────────────────┐     ┌─────────────────────┐
│  Budget Enforcer │────▶│ Error Budget State   │
│  (ci/budget.py)  │     │ (Phase 5 JSON + Prom)│
└──────────────────┘     └─────────────────────┘
       │ ALLOW / FREEZE
       ▼
┌──────────────────┐
│  Chaos Runner    │
│  (runner/)       │
└──────────────────┘
       │
       ├──────────────────────────────────────────┐
       ▼                                          ▼
┌─────────────────────┐               ┌──────────────────────┐
│  QdiscInjector      │               │  Flywheel Bridge     │
│  (injection/)       │               │  (flywheel/)         │
│                     │               │                      │
│  tc qdisc netem     │──JSONL──────▶│  → Phase 3 ML        │
│  + perf monitoring  │               │    /flywheel/ingest  │
└─────────────────────┘               └──────────────────────┘
       │
       ▼
┌─────────────────────┐
│  Healing Measure    │
│  latency baseline   │
│  regression check   │
└─────────────────────┘
       │
       ▼
┌─────────────────────┐
│  Gate Decision      │
│  PASS → merge       │
│  FAIL → block + PR  │
│         comment     │
└─────────────────────┘
```

---

## Components

### `injection/qdisc_injector.py`
The fault injection engine. Wraps `tc qdisc netem` commands:
- 7 fault types: latency, jitter, packet loss, corruption, reorder, bandwidth cap, combined
- Ramp mode for gradual fault onset
- Every session writes a labeled JSONL record to the flywheel directory
- Runs in a veth pair network namespace in CI — never touches the real interface

### `runner/chaos_runner.py`
Orchestrates injection sessions and evaluates the CI gate:
- **CI suite**: 5 standard scenarios on every PR merge
- **GameDay suite**: 7 scenarios on weekly schedule
- Baseline tracking — compares healing latency to last clean main-branch run
- Gate thresholds: P99 < 30s, heal rate ≥ 95%, regression < 200ms, budget < 80%

### `chaos/gameday.py`
Multi-phase fault scenarios for weekly GameDay sessions:
- 5 structured scenarios covering cascading degradation, bandwidth starvation, interface flapping, silent corruption, dual-region failure
- Each scenario evolves through multiple phases — generates fault combinations synthetic data cannot produce
- Always notifies SRE before start (planned chaos)
- Writes a structured GameDay report postmortem

### `flywheel/bridge.py`
The bridge between chaos sessions and Phase 3 ML training:
- Polls the chaos JSONL directory every 10s
- Transforms chaos session records into Phase 3 `flywheel.ingest()` format
- Deduplicates via processed log — safe to restart
- Falls back gracefully if Phase 3 ML serving is unavailable

### `ci/budget_enforcer.py`
The deploy freeze gate:
- Reads error budget state from Phase 5 incident manager
- < 60%: ALLOW
- 60–80%: WARN (deploy allowed with annotation)
- 80–95%: FREEZE
- > 95%: CRITICAL (immediate SRE page + freeze)

---

## CI Integration

### Gate thresholds (blocks merge if exceeded)

| Metric | Threshold |
|--------|-----------|
| P99 healing latency | 30,000ms |
| Healing latency regression | 200ms vs baseline |
| Auto-heal rate | ≥ 95% |
| Error budget consumed | < 80% |

### GitHub Actions

```
.github/workflows/chaos-gate.yml
```

Two jobs:
- **chaos-gate** — runs on every push/PR to main. Posts result as PR comment. Blocks merge on failure.
- **gameday** — runs every Sunday at 02:00 UTC. Posts report to Slack.

---

## Fault Profiles

| Profile | Fault | Duration | Expected Severity |
|---------|-------|----------|-------------------|
| `high_latency` | 200ms delay | 120s | DEGRADED |
| `packet_loss_5pct` | 5% loss | 90s | WARNING |
| `packet_loss_severe` | 25% loss | 60s | CRITICAL |
| `jitter_burst` | 50ms + 100ms jitter | 90s | WARNING |
| `bandwidth_constrained` | 512Kbps cap | 120s | DEGRADED |
| `combined_worst_case` | 300ms + 15% loss | 60s | CRITICAL |
| `packet_reorder` | 30% reorder | 90s | WARNING |

---

## Running Locally

```bash
# CI suite (dry run — no real qdisc injection)
python -m runner.chaos_runner --suite ci --dry-run

# Single scenario
python -m runner.chaos_runner --suite single --scenario high_latency --dry-run

# GameDay suite
python -m runner.chaos_runner --suite gameday --dry-run

# Budget gate check
python -m ci.budget_enforcer

# With real injection (requires CAP_NET_ADMIN)
sudo python -m runner.chaos_runner --suite ci --interface eth0
```

---

## Network Namespace Setup (CI)

```bash
# Creates isolated veth pair — tc rules never touch real interface
sudo ip netns add nimbusnet-chaos
sudo ip link add veth0 type veth peer name veth1
sudo ip link set veth1 netns nimbusnet-chaos
sudo ip link set veth0 up
```

---

## Flywheel Data Flow

```
Chaos session ends
      │
      ▼
JSONL label written → /tmp/nimbusnet/flywheel/chaos/chaos_<id>.jsonl
      │
      ▼
FlywheelBridge picks up (10s poll)
      │
      ▼
POST /flywheel/ingest → Phase 3 ML serving layer
      │
      ▼
Isolation Forest + XGBoost retrain trigger (if threshold met)
```

Every chaos session in development or CI generates a training record. Every production incident generates one too (Phase 5). GameDay sessions generate the most complex records. The models improve continuously with zero manual labeling.

---

## Phase Integration

| Phase | Integration Point |
|-------|------------------|
| Phase 2 (eBPF) | Chaos faults trigger XDP anomaly scores — validates detection sensitivity |
| Phase 3 (ML) | Flywheel bridge pushes labeled records to /flywheel/ingest |
| Phase 4 (Control Plane) | Healing latency measured against FSM state transitions |
| Phase 5 (SRE Ops) | Error budget state drives deploy freeze decision |

---

## Sequence: PR Merge Flow

```
Developer opens PR
      │
      ▼
GitHub Actions triggers chaos-gate job
      │
      ▼
Budget enforcer checks error budget → ALLOW / FREEZE
      │ (ALLOW)
      ▼
Network namespace created (veth pair)
      │
      ▼
CI suite: 5 scenarios in sequence
      │
      ├── high_latency → inject → measure healing → compare baseline
      ├── packet_loss_5pct → inject → measure → compare
      ├── jitter_burst → inject → measure → compare
      ├── bandwidth_constrained → inject → measure → compare
      └── combined_worst_case → inject → measure → compare
      │
      ▼
Gate evaluation:
  P99 latency < 30s? ✓
  Heal rate ≥ 95%? ✓
  Max regression < 200ms? ✓
  Budget < 80%? ✓
      │
      ▼
PR comment posted with metrics
      │
      ├── PASS → merge allowed
      └── FAIL → merge blocked
      │
      ▼
Flywheel labels synced to S3
Baseline updated (main branch only on pass)
```
