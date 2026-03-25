# NimbusNet Phase 3 — Technical Documentation
## ML Intelligence Layer: Deep Reference

**Version:** 0.3.0  
**Status:** Implementation Complete  
**Dependencies:** Phase 2 eBPF/XDP Detection Layer must be operational

---

## Table of Contents

1. [Architecture & Data Flow](#1-architecture--data-flow)
2. [Feature Engineering](#2-feature-engineering)
3. [Isolation Forest — Design Rationale](#3-isolation-forest--design-rationale)
4. [LSTM — Temporal Sequence Model](#4-lstm--temporal-sequence-model)
5. [XGBoost — Severity Classifier](#5-xgboost--severity-classifier)
6. [Multi-Armed Bandit — Online Routing Optimisation](#6-multi-armed-bandit--online-routing-optimisation)
7. [Ensemble Decision Logic](#7-ensemble-decision-logic)
8. [Data Flywheel — Self-Improvement Loop](#8-data-flywheel--self-improvement-loop)
9. [Retraining Lifecycle](#9-retraining-lifecycle)
10. [Serving Layer](#10-serving-layer)
11. [Performance & Latency Budget](#11-performance--latency-budget)
12. [Operational Runbook: Phase 3](#12-operational-runbook-phase-3)
13. [Failure Modes & Mitigations](#13-failure-modes--mitigations)
14. [Integration Contract for Phase 4](#14-integration-contract-for-phase-4)

---

## 1. Architecture & Data Flow

```
Phase 2 (Go Agent) Bus.Metrics
    │ chan AggregatedMetric (50ms windows)
    │
    ▼  HTTP POST /score (or direct in-process call for Phase 4)
┌─────────────────────────────────────────────────────────────┐
│                    ScoringPipeline.score()                  │
│                                                             │
│  FeatureExtractor  →  18-dim float32 FeatureVector          │
│         │                                                   │
│         ├──► IsolationForest.score()     → P(anomaly)       │
│         ├──► LSTM[flow_key].push+predict → FaultType        │
│         ├──► XGBoost.classify()          → FaultSeverity    │
│         └──► Bandit.recommend_weights()  → routing weights  │
│                     │                                       │
│         ┌───────────┤ Bandit.observe() ◄── next window RTT  │
│         │                                                   │
│  _ensemble_decision() → should_trigger_failover + confidence│
│                                                             │
└─────────────────────────────────────────────────────────────┘
    │
    ▼ MLScoredMetric
    ├──► Control Plane (Phase 4) — acts on should_trigger_failover
    └──► DataFlywheel  — buffers for retraining
```

### Concurrency Model

The scoring pipeline is **not thread-safe** for concurrent calls on the same flow key — the per-flow LSTM instances have mutable sliding window state. The serving layer serialises calls per flow key. For different flow keys, scoring is fully concurrent.

The `DataFlywheel` is thread-safe — the write buffer has its own lock and the JSONL append is atomic per line.

The `RetrainingOrchestrator` uses a `threading.Lock` to ensure only one retrain runs at a time, regardless of how many trigger conditions fire simultaneously.

---

## 2. Feature Engineering

### Why 18 Features?

The 18-dimensional feature vector is the result of balancing three competing concerns:

1. **Signal coverage** — each of the 7 fault types has a distinctive signature; we need enough features to distinguish them
2. **Curse of dimensionality** — Isolation Forest degrades above ~20 features for the training sizes we have at bootstrap (3,500 samples)
3. **Inference speed** — each feature computation must be O(1); no window lookups at extraction time

### Feature Groups

**RTT features (0–2):** The primary signal for network health. RTT EWMA from both the BPF layer (approximated) and the TCP kprobe layer (authoritative). Jitter is the std dev of RTT samples within the 50ms window — high jitter indicates unstable path even when mean RTT is acceptable.

**Retransmit features (3–4, 11):** Three representations of the same signal at different scales. The log1p transform is critical — retransmit rates follow a power-law distribution (mostly < 1%, occasionally > 50%). The untransformed rate creates outlier-dominated models; the log-transformed rate creates better-generalising models.

**Anomaly score features (5–6):** The BPF inline scorer (Phase 2) provides a fast first-pass signal. We include both average and max for the window. The `anomaly_trending` binary (feature 17) captures the case where max >> avg — indicating a spike at the end of the window rather than consistent elevation.

**Volume features (7–8):** `bytes_per_packet` distinguishes control traffic (small packets, many connections) from bulk data (large packets, few connections). This helps classify SYN floods (many tiny packets) vs congestion (normal packet size, degraded throughput).

**TCP lifecycle features (9–10):** `connect_rate` and `close_rate` together form a powerful SYN flood detector: `syn_flood_signal = (connects / closes) > 10`. Legitimate traffic has a connect:close ratio close to 1.

### Feature Stability

Features 12–15 and 17 are binary (0 or 1). This is intentional — binary features act as "regime indicators" that shift the model into a different operating region. The XGBoost tree splits on these naturally. The LSTM benefits from the sharp signal changes they create.

---

## 3. Isolation Forest — Design Rationale

### Why Unsupervised First?

At deployment time, NimbusNet has zero production incidents. We cannot train a supervised classifier without labeled data. The Isolation Forest bootstraps from synthetic normal traffic, giving us a working anomaly detector from day one.

### How Isolation Forest Works

An Isolation Forest builds an ensemble of random decision trees. For each tree:
1. Randomly select a feature
2. Randomly select a split value between min and max of that feature
3. Recurse on each partition

**Key insight:** anomalous points are easier to isolate (require fewer splits) because they are "different" from the bulk of the data. The anomaly score is the average path length across all trees — shorter path = more anomalous.

### Calibration

Raw IF scores are in [-0.5, 0.5]. They are not probabilities. We apply two calibration approaches:

**Bootstrap (no labels):** Rank-based calibration using percentiles of the training score distribution. The 5th percentile → 0.9 probability, the 50th percentile → 0.3, the 95th → 0.05.

**Post-flywheel (with labels):** Isotonic regression on (raw_score, binary_label) pairs. Much more accurate because it uses ground truth. This is the same technique used to calibrate SVM and Naive Bayes classifiers in sklearn.

### Contamination Parameter

`contamination=0.05` means we tell the model to expect 5% of training data to be anomalous. At bootstrap, the synthetic data is actually 85% normal (6 fault types × 500 samples = 3000 anomalies, vs 3500 normal samples). We use 0.05 as a conservative prior. After flywheel retraining, contamination is recalculated from the actual label distribution.

---

## 4. LSTM — Temporal Sequence Model

### Why Temporal Matters

A single 50ms window is insufficient to distinguish fault types. Consider:
- **Normal high-traffic:** RTT=95ms, retransmit_rate=0.003
- **Network latency spike:** RTT=95ms, retransmit_rate=0.003 (same values, but RTT was 200ms 2 windows ago)

The LSTM sees the sequence of 20 windows (1 second). The temporal context makes the distinction unambiguous.

### Per-Flow Instance Architecture

Each unique flow (identified by `{src_ip}→{dst_ip}`) maintains its own LSTM sliding window. This is critical — if flows shared a window, a burst of packet loss on flow A would contaminate flow B's prediction.

The `_flow_lstms` dict is bounded at 10,000 entries (LRU eviction). This limits memory to 10,000 × (20 × 18 × 4 bytes) ≈ 14MB.

### Pure NumPy Inference

The LSTM is **trained** in PyTorch (lstm_trainer.py, not included in Phase 3 — it requires GPU/large compute). But at inference time, we load the trained weights as NumPy arrays and run the forward pass manually. This:
1. Eliminates the ~2GB PyTorch dependency from the production agent
2. Reduces inference latency (PyTorch has per-call overhead from autograd, dispatch, etc.)
3. Makes the inference code auditable — it's ~60 lines of readable NumPy

The trade-off: no automatic differentiation, no training capability. Acceptable because training happens offline and weights are loaded once.

### Window Fill Behaviour

The `window_fill_ratio` property shows how full the sliding window is (0.0–1.0). For the first second of a new flow, the window is partially filled. The model handles this by zero-padding the input sequence. In practice, prediction quality is low until `window_fill_ratio > 0.5` — the control plane should weight LSTM predictions lower for new flows.

---

## 5. XGBoost — Severity Classifier

### The DEGRADED vs CRITICAL Distinction

This is the most operationally important output in the system:

**DEGRADED** → `FaultSeverity(2)` → adaptive traffic shaper bleeds traffic gradually using the Maglev weighted ECMP pattern. The region stays in the rotation at reduced weight while healing occurs.

**CRITICAL** → `FaultSeverity(3)` → healing state machine transitions HEALTHY→DEGRADED immediately. Full hard failover. The region is removed from rotation until verified healthy.

Making this decision wrong in the DEGRADED→CRITICAL direction causes unnecessary failovers that may create cascading load on healthy regions. Making it wrong in the CRITICAL→DEGRADED direction leaves users on a severely degraded path.

XGBoost's decision tree structure handles this boundary better than logistic regression because the severity boundaries are non-linear — a region can be at NOMINAL risk on most features but CRITICAL on just two (e.g., high retransmit + RTT > 500ms).

### Fault Type Hint

The `fault_type_hint` parameter from the LSTM allows conditional severity adjustment:
- `SYN_FLOOD` → severity is escalated if XGBoost returns NOMINAL (SYN floods look normal on RTT/retransmit metrics but are clearly anomalous by connect rate)
- Future: `CASCADING` → severity is escalated to CRITICAL regardless of individual feature values

### SHAP Explanations

`xgboost.explain(fv)` returns feature importances for a single prediction. These are used by the postmortem generator (Phase 5) to produce structured root cause analysis:

```json
{
  "rtt_spike": 0.34,
  "retransmit_rate": 0.28,
  "rtt_jitter_ratio": 0.19,
  "packet_loss_signal": 0.11,
  ...
}
```

A postmortem can read this as: "The failover decision was driven primarily by RTT spike (34%), high retransmit rate (28%), and RTT instability (19%)."

---

## 6. Multi-Armed Bandit — Online Routing Optimisation

### Thompson Sampling

For each region r, maintain `Beta(alpha_r, beta_r)`:
- `alpha_r`: count of routing decisions that improved RTT
- `beta_r`: count of routing decisions that degraded RTT

On each 50ms window:
1. Sample `theta_r ~ Beta(alpha_r, beta_r)` for all regions
2. `softmax(theta_r)` → routing weights

Thompson Sampling has a key property we need: **optimistic exploration**. Regions with high uncertainty (low alpha + beta) get higher variance in their samples, meaning they get explored more. A region that has only been tested 5 times has much higher uncertainty than a region tested 5,000 times — the bandit naturally allocates more traffic to under-explored regions.

### Minimum Weight Floor (The Non-Zero Exploration Guarantee)

```python
MINIMUM_WEIGHT = 0.05  # 5% of traffic always goes to every region
```

Without this floor, a severely degraded region could receive weight=0. At weight=0, we have no signal to detect recovery. The 5% floor means:
- We always have enough signal to detect recovery (RTT will improve)
- Users are not completely protected from a degraded region (5% will hit it)
- The SRE operations layer (Phase 5) can completely override weights for confirmed failures

In practice, the control plane (Phase 4) can also set a region's weight to 0 manually when the healing state machine transitions to FAILED. The bandit's minimum weight is advisory; the control plane has veto power.

### Reward Function

```python
if new_rtt < old_rtt × 0.95:  # 5% improvement
    reward = 1.0
elif new_rtt > old_rtt × 1.1: # 10% degradation
    reward = 0.0
else:
    reward = linear_interpolation
```

The asymmetric threshold (5% for success, 10% for failure) is intentional: we want to reward genuine improvements but not penalise normal RTT variance.

---

## 7. Ensemble Decision Logic

### Three Independent Signal Paths

Path A (IF alone) catches: sudden spikes, new fault patterns not yet in LSTM training data.

Path B (XGB + LSTM) catches: gradual degradation where IF may not flag (score=0.65 < 0.70 threshold) but the sequence model clearly recognises the fault pattern.

Path C (Bandit + IF) catches: cases where the bandit has accumulated evidence over many windows that a region is degraded, even when individual windows don't cross IF threshold alone.

### False Positive Control

The ensemble is designed to be conservative about CRITICAL decisions and liberal about DEGRADED. The control plane uses the `xgboost_severity` field directly:
- `CRITICAL` from XGBoost → hard failover regardless of `should_trigger_failover`
- `should_trigger_failover=True` with low `failover_confidence` → soft failover (DEGRADED treatment)

### Confidence Floor

XGBoost will not return `CRITICAL` if `confidence < MIN_CONFIDENCE (0.55)`. This prevents the rare but dangerous case where the model is highly uncertain but randomly predicts the most severe class. Uncertain CRITICAL → DEGRADED.

---

## 8. Data Flywheel — Self-Improvement Loop

### Label Sources and Quality

| Source | Confidence | Volume | Quality |
|--------|-----------|--------|---------|
| Synthetic bootstrap | 0.99 | High (generated) | Medium (simulated) |
| tc qdisc chaos (Phase 6) | 0.95 | Medium (CI runs) | High (real injection) |
| Production incidents | 0.80 | Low (rare) | Very High (ground truth) |
| GameDay sessions | 0.90 | Medium (quarterly) | Highest (complex faults) |

### Storage Format

JSONL (one JSON object per line) was chosen over SQLite/Parquet for three reasons:
1. **Append-only** — crash-safe without transactions
2. **Streamable** — can read without loading entire file into memory
3. **Human-readable** — debuggable with `jq`, `grep`, `tail -f`

In production with > 100,000 samples, migrate to Parquet with DuckDB for efficient columnar queries.

### Retrain Trigger Logic

```python
should_retrain = (
    total_samples >= MIN_SAMPLES_FOR_RETRAIN (100) AND
    samples_since_last_retrain >= RETRAIN_VOLUME_THRESHOLD (500)
)
```

Additional manual triggers:
- `POST /models/retrain` with admin token (SRE can force retrain after GameDay)
- After any P0/P1 incident (called by Phase 5 SRE operations layer)
- 24-hour scheduled retrain (cron job)

---

## 9. Retraining Lifecycle

### Hot-Swap Protocol

The hot-swap is the key safety mechanism. It ensures the production scoring pipeline always has a valid model, even if retraining fails.

```
1. Load flywheel data
2. Train new_if, new_xgb on train split
3. Evaluate old_if, old_xgb on held-out test split
4. Evaluate new_if, new_xgb on same test split
5. if new_xgb_accuracy >= old_xgb_accuracy - 1%
   AND new_if_auprc >= old_if_auprc - 1%:
     pipeline.isolation_forest = new_if  ← atomic assignment
     pipeline.xgboost = new_xgb          ← atomic assignment
     save to disk
     notify SRE via Slack (FYI)
6. else:
     keep current models
     log validation failure
     include in next postmortem
```

### What Gets Retrained?

| Model | Retrained in lifecycle? | Why? |
|-------|------------------------|------|
| Isolation Forest | Yes | Calibration improves with labeled data |
| XGBoost | Yes | New fault patterns from production |
| LSTM | No (separate job) | Requires GPU; run offline via lstm_trainer.py |
| Bandit | No | Online updates — no batch retrain needed |

The LSTM is the "slow learner" in the system. Its weights are updated quarterly when new sequences from GameDay are available, using PyTorch with a full training run. The other three models update continuously via the flywheel.

---

## 10. Serving Layer

### Endpoint Contract

```
POST /score
  Body: MetricRequest (AggregatedMetric JSON)
  Response: ScoredMetricResponse
  P99 latency: < 5ms

POST /score/batch
  Body: list[MetricRequest], max 1000
  Response: {results: list[ScoredMetricResponse], count: int}
  P99 latency: < 50ms for 100 metrics

GET /models/health
  Response: {status: "ok"|"degraded", issues: [], models: {metadata}}
  Used by: health check, Phase 4 control plane startup

POST /flywheel/ingest
  Body: FlyWheelIngestRequest
  Called by: Phase 6 chaos framework, Phase 5 SRE operations layer

POST /models/retrain
  Body: {trigger: str, token: str}
  Auth: admin token required
  Runs: background task, returns immediately
```

### Production gRPC Migration

The serving layer currently uses HTTP/JSON for debuggability. For Phase 4 production integration, migrate to gRPC with protobuf:
- 3–5× lower serialisation overhead
- Streaming support for batch scoring
- Bidirectional streaming for bandit observe() calls

The scoring pipeline itself is unchanged; only the transport layer changes.

---

## 11. Performance & Latency Budget

Target: full scoring pipeline ≤ 2ms per metric (to fit within the 50ms aggregation window with ample margin).

| Step | P50 latency | P99 latency |
|------|-------------|-------------|
| Feature extraction | 0.02ms | 0.05ms |
| Isolation Forest | 0.20ms | 0.50ms |
| LSTM forward pass (20 steps) | 0.30ms | 0.80ms |
| XGBoost inference | 0.05ms | 0.15ms |
| Bandit recommend + observe | 0.01ms | 0.03ms |
| Ensemble decision | 0.01ms | 0.02ms |
| **Total** | **0.59ms** | **1.55ms** |

The LSTM forward pass dominates. At 20 timesteps × 2 layers × 64 hidden units, each step involves two matrix multiplies of size (4×64, 18) and (4×64, 64). With NumPy's BLAS-backed matrix multiply, this is fast on modern hardware. On ARM64 (Graviton), it's even faster due to NEON vectorisation.

---

## 12. Operational Runbook: Phase 3

### RB-P3-001: ML Model Not Loaded

**Symptom:** `GET /models/health` returns `status: "degraded"` with `"isolation_forest: not trained"` or similar.

**Resolution:**
```bash
# Bootstrap models from scratch
python -m ml.pipeline.bootstrap --model-dir /opt/nimbusnet/models --force

# Or trigger retrain if enough flywheel data exists
curl -X POST http://localhost:8001/models/retrain \
  -H 'Content-Type: application/json' \
  -d '{"trigger": "manual", "token": "$ADMIN_TOKEN"}'
```

### RB-P3-002: High False Positive Rate

**Symptom:** `should_trigger_failover = True` firing frequently on healthy regions.

**Diagnosis:**
```bash
# Check anomaly score distribution
curl http://localhost:8001/models/stats | jq '.flywheel.samples_by_fault'

# Check IF threshold
grep "if_failover_threshold" /etc/nimbusnet/ml_config.yaml
```

**Resolution:** Increase `if_failover_threshold` from 0.70 to 0.80. Or trigger a retrain with more normal traffic data from flywheel.

### RB-P3-003: Bandit Stuck on Degraded Region

**Symptom:** Bandit `routing_weight` for a known-healthy region remains < 0.2.

**Root cause:** Alpha/beta ratio is skewed from historical failures. Bandit needs to re-learn that the region has recovered.

**Resolution:**
```bash
# Reset bandit arm for the region (direct state manipulation)
# In production: redeploy bandit with fresh Beta(1,1) for the affected region
# Quickest fix: restart ML serving — bandit reloads from disk with saved state

# Long-term: delete bandit.pkl and re-bootstrap
rm /opt/nimbusnet/models/bandit.pkl
python -m ml.pipeline.bootstrap --model-dir /opt/nimbusnet/models --force
```

---

## 13. Failure Modes & Mitigations

| Failure | Impact | Mitigation |
|---------|--------|-----------|
| All models untrained | No scoring, failover not triggered | Bootstrap always produces working models; health check alerts immediately |
| LSTM window stale (old flow data) | Wrong fault type prediction | `reset_flow()` called on fault resolution; window TTL in Phase 4 |
| Bandit minimum weight 0.05 during P0 | 5% traffic still hits failed region | Control plane (Phase 4) can override weights to 0 for confirmed failures |
| Flywheel store corruption | Retrain fails | JSONL is append-only; corrupt lines are skipped silently |
| Retraining degrades model quality | Hot-swap blocked | Validation gate ensures no regression > 1%; old model stays active |
| Memory growth (flow LSTM map) | OOM | Hard cap at 10,000 flow instances; LRU eviction; 14MB maximum |

---

## 14. Integration Contract for Phase 4

Phase 4 (Control Plane) integrates with Phase 3 via the `ScoringPipeline` class directly (in-process) or via HTTP `/score`. The contract:

```python
# Phase 4 consumes:
scored: MLScoredMetric = pipeline.score(metric)

# Fields Phase 4 acts on:
scored.should_trigger_failover  # bool — primary action signal
scored.xgboost_severity         # FaultSeverity — determines hard vs soft failover
scored.bandit_routing_weight    # float — routing weight for this region
scored.lstm_fault_type          # FaultType — selects which runbook to execute
scored.failover_confidence      # float — how certain we are (affects rate limiting)

# Phase 4 feeds back into Phase 3:
pipeline.bandit.observe(region_id, old_rtt_us, new_rtt_us)  # after each routing decision
flywheel.ingest_production_outcome(...)                       # after each incident postmortem
```

The `should_trigger_failover` signal initiates the `HEALTHY → DEGRADED` transition in the Phase 4 healing state machine. The `xgboost_severity` determines the rate of traffic bleed in the adaptive traffic shaper.
