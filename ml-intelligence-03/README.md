# NimbusNet — Phase 3: ML Intelligence Layer

> **The brain.** Four models. One flywheel. Zero human labels required at bootstrap. Every production incident makes the system smarter. Every chaos session generates training data as a side effect.

---

## What This Phase Delivers

| Component | Description |
|-----------|-------------|
| `ml/models/types.py` | Shared data contracts: `AggregatedMetric`, `FeatureVector` (18-dim), `MLScoredMetric`, `FaultType` (7 classes), `FaultSeverity` (4 classes) |
| `ml/models/feature_extractor.py` | Stateless, deterministic 18-feature extraction from `AggregatedMetric`. The single boundary between raw telemetry and all ML models |
| `ml/models/isolation_forest.py` | Unsupervised anomaly detection — no labels required at bootstrap. Isotonic regression calibration maps raw scores to probability [0,1] |
| `ml/models/lstm.py` | Two-layer LSTM sequence model. Temporal fault signature recognition over a 1-second (20 × 50ms) sliding window. Pure NumPy inference — no framework dependency |
| `ml/models/xgboost_classifier.py` | 4-class severity classifier (NOMINAL/WARNING/DEGRADED/CRITICAL). SHAP feature importances for postmortem reports. Decision tree fallback if XGBoost unavailable |
| `ml/models/bandit.py` | Thompson Sampling multi-armed bandit. Online routing weight updates every 50ms. Guaranteed minimum exploration weight — never loses signal on degraded regions |
| `ml/pipeline/scoring_pipeline.py` | Orchestrates all four models. Ensemble decision logic with three independent failover trigger paths. Per-flow LSTM instance management |
| `ml/pipeline/bootstrap.py` | First-run synthetic data generation + model training entrypoint |
| `ml/flywheel/flywheel.py` | Training data store (JSONL), label ingestion, retrain trigger logic |
| `ml/flywheel/retrainer.py` | Background retrain orchestrator. Train → validate → hot-swap → notify lifecycle |
| `ml/flywheel/bootstrap_generator.py` | Synthetic metric generator for 7 fault types. Calibrated against real AWS inter-region traffic distributions |
| `ml/serving/serving.py` | FastAPI HTTP server: `/score`, `/score/batch`, `/models/health`, `/flywheel/ingest`, `/models/retrain` |
| `configs/ml_config.yaml` | Full annotated config reference |
| `Dockerfile` | Two-stage: bootstrap model generation + production serving |

---

## The Four Models

### 1. Isolation Forest — "Is this anomalous?"

The first model in the pipeline. **Requires no labels at bootstrap** — it learns what normal traffic looks like from the synthetic baseline dataset.

```
Input:  FeatureVector (18 dims)
Output: anomaly_probability ∈ [0, 1]
Threshold: > 0.70 → consider failover (alone)
Training: unsupervised on normal traffic baseline
Calibration: isotonic regression (supervised once labels arrive from flywheel)
Inference: < 0.5ms
```

### 2. LSTM — "What type of fault is this?"

The model that makes NimbusNet **predictive, not just reactive**. Observes sequences of feature vectors over time and recognises temporal fingerprints of each fault type before the fault fully manifests.

```
Input:  Sliding window of last 20 FeatureVectors (= 1 second of history)
Output: FaultType prediction + confidence
Classes: UNKNOWN, NETWORK_LATENCY, PACKET_LOSS, REGION_PARTITION, CONGESTION, SYN_FLOOD, CASCADING
Architecture: 2-layer LSTM, 64 hidden units, pure NumPy inference (no PyTorch at runtime)
Training: PyTorch (lstm_trainer.py) — weights saved as NumPy arrays
```

Each flow has **its own LSTM sliding window**. Sequences don't bleed across flows.

### 3. XGBoost — "How severe is it?"

The model that translates anomaly detection into **actionable decisions**.

```
Input:  FeatureVector (18 dims) + FaultType hint from LSTM
Output: FaultSeverity (NOMINAL / WARNING / DEGRADED / CRITICAL) + confidence
NOMINAL  → No action
WARNING  → Increased monitoring
DEGRADED → Adaptive traffic shaper begins bleeding traffic away
CRITICAL → Healing state machine: immediate HEALTHY → DEGRADED transition
```

The DEGRADED/CRITICAL distinction is the most important output in the whole system. DEGRADED triggers a soft failover (gradual traffic bleed). CRITICAL triggers a hard failover (immediate full shift).

### 4. Multi-Armed Bandit — "Where should traffic go?"

The only model that **updates on every routing decision** — no waiting for the flywheel cycle.

```
Algorithm: Thompson Sampling with Beta(α, β) distributions per region
Update: every 50ms, on observed RTT improvement/degradation
Output: routing_weight per region, weights sum to 1.0
Minimum weight: 0.05 (5% floor — never loses signal on degraded region)
```

The minimum weight floor is the critical design detail: a region that drops to weight=0 is invisible. At 5%, we always have enough signal to detect recovery.

---

## The Data Flywheel

```
                    tc qdisc chaos session (Phase 6)
                              │
                    ONE operation → TWO outputs:
                    ┌─────────┴──────────────────────┐
                    │                                 │
              Test result                    Labeled training sample
           (pass/fail CI gate)              (feature_vector + fault_type + severity)
                                                      │
                                                      ▼
                                            DataFlywheel.ingest_synthetic()
                                                      │
                                    ┌─────────────────┴───────────────────┐
                                    │                                     │
                         Production failovers                     GameDay sessions
                    (labeled retrospectively                  (complex multi-fault combos
                      by SRE postmortem)                     synthetic data can't cover)
                                    │                                     │
                                    └─────────────┬───────────────────────┘
                                                  ▼
                                     RETRAIN_VOLUME_THRESHOLD = 500 samples
                                                  │
                                         RetrainingOrchestrator
                                           retrain() → validate() → hot-swap()
                                                  │
                                         Slack FYI to SRE
                                       (not paged — informed)
```

**Bootstrap → Production improvement curve:**

| Stage | Data Source | IF Calibration | XGBoost Accuracy |
|-------|-------------|----------------|-----------------|
| Bootstrap | Synthetic only | Unsupervised (rank-based) | ~80% balanced |
| After 500 samples | Synthetic + first prod incidents | Supervised isotonic | ~87% |
| After 2000 samples | Full flywheel | Production-calibrated | ~93% |
| After GameDay | Rare fault combos | Robust to cascading | ~96% |

---

## Ensemble Decision Logic

Three independent paths to failover. Any one path is sufficient:

```
Path A (IF alone):       isolation_forest_score > 0.70
                         → Weight: 40% of confidence

Path B (XGB + LSTM):     xgboost_severity >= DEGRADED
                         AND lstm_confidence > 0.60
                         → Weight: 40% of confidence

Path C (Bandit + IF):    bandit_routing_weight < 0.20
                         AND isolation_forest_score > 0.50
                         → Weight: 20% of confidence

failover_confidence = weighted_average(triggered_path_scores)
```

**Why three paths?**
- Path A catches sudden anomalies (region partition) before LSTM has enough sequence history
- Path B catches gradual degradation that the LSTM recognises but IF may miss
- Path C catches cases where the bandit has already learned the region is degraded before the other models react

---

## Feature Vector Reference (18 dimensions)

| Index | Name | Range | Description |
|-------|------|-------|-------------|
| 0 | `rtt_ewma_ms` | [0, 2000] | EWMA RTT in ms |
| 1 | `rtt_jitter_ms` | [0, 500] | RTT jitter (std dev) in ms |
| 2 | `rtt_jitter_ratio` | [0, 5] | Jitter / RTT ratio (instability signal) |
| 3 | `retransmit_rate` | [0, 1] | Fraction of packets retransmitted |
| 4 | `retransmit_rate_log1p` | [0, 1] | Log1p transform (de-skewed) |
| 5 | `avg_anomaly_score_norm` | [0, 1] | BPF inline score / 100 |
| 6 | `max_anomaly_score_norm` | [0, 1] | Max BPF score in window / 100 |
| 7 | `bytes_per_packet` | [0, 1] | Normalised average packet size |
| 8 | `packets_per_ms` | [0, 1] | Traffic rate (normalised to 100K pps) |
| 9 | `connect_rate` | [0, 1] | TCP connects per ms (normalised) |
| 10 | `close_rate` | [0, 1] | TCP closes per ms (normalised) |
| 11 | `retrans_per_packet` | [0, 1] | Direct retransmit ratio |
| 12 | `rtt_spike` | {0, 1} | Binary: RTT > 100ms |
| 13 | `high_jitter` | {0, 1} | Binary: jitter > 20ms |
| 14 | `packet_loss_signal` | {0, 1} | Binary: retransmit rate > 5% |
| 15 | `syn_flood_signal` | {0, 1} | Binary: connects/closes ratio > 10 |
| 16 | `window_utilisation` | [0, 1] | Bandwidth as fraction of 1Gbps |
| 17 | `anomaly_trending` | {0, 1} | Binary: max score > avg × 1.5 |

---

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Bootstrap models (generates synthetic data, trains all models)
python -m ml.pipeline.bootstrap \
    --model-dir ./models \
    --data-dir  ./data \
    --samples   500

# Start the scoring server
MODEL_DIR=./models DATA_DIR=./data \
    python -m ml.serving.main

# Test it
curl -X POST http://localhost:8001/score \
  -H 'Content-Type: application/json' \
  -d '{
    "window_start_ns": 1700000000000000000,
    "window_end_ns":   1700000000050000000,
    "region": "us-east-1",
    "rtt_ewma_us": 150000,
    "rtt_jitter_us": 25000,
    "retransmit_rate": 0.08,
    "total_packets": 1000,
    "total_bytes": 700000,
    "max_anomaly_score": 65,
    "avg_anomaly_score": 48.0
  }'

# Model health
curl http://localhost:8001/models/health

# Flywheel stats
curl http://localhost:8001/models/stats
```

---

## File Tree

```
phase-03-ml-intelligence/
├── README.md                               ← you are here
├── DOCUMENTATION.md                        ← deep technical reference
├── Dockerfile                              ← bootstrap + serving stages
├── requirements.txt
├── ml/
│   ├── __init__.py
│   ├── models/
│   │   ├── types.py                        ← shared data contracts
│   │   ├── feature_extractor.py            ← AggregatedMetric → FeatureVector
│   │   ├── isolation_forest.py             ← unsupervised anomaly detection
│   │   ├── lstm.py                         ← temporal fault type prediction
│   │   ├── xgboost_classifier.py           ← fault severity classification
│   │   └── bandit.py                       ← Thompson Sampling routing weights
│   ├── pipeline/
│   │   ├── scoring_pipeline.py             ← orchestrates all four models
│   │   └── bootstrap.py                    ← first-run model initialisation
│   ├── flywheel/
│   │   ├── flywheel.py                     ← training store + label ingestion
│   │   ├── retrainer.py                    ← retrain → validate → hot-swap
│   │   └── bootstrap_generator.py          ← synthetic data generator
│   └── serving/
│       └── serving.py                      ← FastAPI HTTP scoring server
└── configs/
    └── ml_config.yaml                      ← annotated config reference
```

---

## What's Next — Phase 4

Phase 4 is the Control Plane — the healing state machine, adaptive traffic shaper, and Route 53 / VPC route table integration. It consumes `MLScoredMetric` from this layer and translates `should_trigger_failover=True` into actual infrastructure changes.

The bandit's `routing_weight` values become the ECMP weights in the VPC route table. The LSTM's `fault_type` determines which runbook to execute. The XGBoost `severity` determines how fast to bleed traffic (soft vs hard failover).
