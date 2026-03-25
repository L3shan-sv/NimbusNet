"""
types.py — Shared data contracts for the NimbusNet ML layer.

These mirror the Go AggregatedMetric type from Phase 2.
The feature vector is the single typed boundary between
raw telemetry and all four ML models.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional
import numpy as np


# ─── Enumerations ──────────────────────────────────────────────────────────────

class FaultType(IntEnum):
    """Predicted failure mode from LSTM sequence model."""
    UNKNOWN          = 0
    NETWORK_LATENCY  = 1   # RTT spike, no packet loss
    PACKET_LOSS      = 2   # high retransmit rate
    REGION_PARTITION = 3   # complete connectivity loss to a peer
    CONGESTION       = 4   # high latency + loss, bandwidth-limited
    SYN_FLOOD        = 5   # DDoS pattern
    CASCADING        = 6   # multiple fault types in sequence


class FaultSeverity(IntEnum):
    """Classification output from XGBoost."""
    NOMINAL  = 0   # no action required
    WARNING  = 1   # monitor closely
    DEGRADED = 2   # initiate soft failover
    CRITICAL = 3   # immediate hard failover


# ─── Wire Types (mirrors Go AggregatedMetric) ─────────────────────────────────

@dataclass
class AggregatedMetric:
    """
    Received from the Go agent Bus.Metrics channel via gRPC/protobuf.
    Represents one 50ms aggregation window for one flow.
    """
    # Window
    window_start_ns: int
    window_end_ns:   int
    window_size_ms:  float = 50.0

    # Flow identity
    src_ip:  str = ""
    dst_ip:  str = ""
    region:  str = "unknown"

    # Volume
    total_bytes:   int = 0
    total_packets: int = 0

    # Reliability signals
    retransmit_rate: float = 0.0   # fraction [0, 1]
    rtt_ewma_us:     int   = 0     # microseconds
    rtt_jitter_us:   int   = 0     # microseconds (std dev of RTT samples)

    # Anomaly (from BPF inline scorer)
    max_anomaly_score: int   = 0   # 0–100
    avg_anomaly_score: float = 0.0

    # TCP lifecycle
    connects_observed: int = 0
    closes_observed:   int = 0
    retrans_observed:  int = 0

    # Ownership
    owner_node_id: str  = ""
    is_local:      bool = False


@dataclass
class FeatureVector:
    """
    18-dimensional feature vector derived from AggregatedMetric.
    This is the single input type shared by all four ML models.

    Feature index map (for SHAP explanations):
        0:  rtt_ewma_ms
        1:  rtt_jitter_ms
        2:  rtt_jitter_ratio        (jitter / ewma, 0 if ewma==0)
        3:  retransmit_rate
        4:  retransmit_rate_log1p
        5:  avg_anomaly_score_norm  (/ 100)
        6:  max_anomaly_score_norm  (/ 100)
        7:  bytes_per_packet
        8:  packets_per_ms
        9:  connect_rate            (connects / window_ms)
        10: close_rate              (closes / window_ms)
        11: retrans_per_packet
        12: rtt_spike               (rtt_ewma_ms > 100)
        13: high_jitter             (jitter_ms > 20)
        14: packet_loss_signal      (retransmit_rate > 0.05)
        15: syn_flood_signal        (connects >> closes)
        16: window_utilisation      (bytes / (window_ms * 125_000) — 1Gbps baseline)
        17: anomaly_trending        (max > avg * 1.5)
    """
    values: np.ndarray  # shape (18,), dtype float32
    source: AggregatedMetric
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())

    FEATURE_NAMES = [
        "rtt_ewma_ms",
        "rtt_jitter_ms",
        "rtt_jitter_ratio",
        "retransmit_rate",
        "retransmit_rate_log1p",
        "avg_anomaly_score_norm",
        "max_anomaly_score_norm",
        "bytes_per_packet",
        "packets_per_ms",
        "connect_rate",
        "close_rate",
        "retrans_per_packet",
        "rtt_spike",
        "high_jitter",
        "packet_loss_signal",
        "syn_flood_signal",
        "window_utilisation",
        "anomaly_trending",
    ]
    N_FEATURES = 18


@dataclass
class MLScoredMetric:
    """
    Output of the ML scoring pipeline.
    Pushed to Bus.ScoredMetrics → control plane (Phase 4).
    Also written to the training store for flywheel labels.
    """
    # Source
    source: AggregatedMetric
    features: FeatureVector

    # Model outputs
    isolation_forest_score: float        = 0.0   # anomaly probability [0,1]; >0.7 = anomaly
    lstm_fault_type:        FaultType    = FaultType.UNKNOWN
    lstm_confidence:        float        = 0.0   # softmax max probability
    xgboost_severity:       FaultSeverity= FaultSeverity.NOMINAL
    xgboost_confidence:     float        = 0.0
    bandit_routing_weight:  float        = 1.0   # [0,1]; 0 = no traffic, 1 = full traffic

    # Ensemble decision
    should_trigger_failover: bool  = False
    failover_confidence:     float = 0.0

    # Metadata
    scored_at_ns:    int = field(default_factory=time.time_ns)
    model_versions:  dict = field(default_factory=dict)
    scoring_latency_us: float = 0.0
