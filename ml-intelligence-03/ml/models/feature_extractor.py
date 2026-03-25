"""
feature_extractor.py — Converts AggregatedMetric → FeatureVector.

This is the only place where raw telemetry becomes ML input.
All four models consume FeatureVector exclusively — they never
touch AggregatedMetric directly. This decouples feature engineering
from model implementation.

Design decisions:
    - All features are float32, normalised to reasonable [0, ~1] ranges
    - Log transforms on heavy-tailed distributions (retransmit rate)
    - Binary signal features for fast inline detection (rtt_spike etc.)
    - No external state — stateless, pure function, deterministic
"""

from __future__ import annotations

import math
import numpy as np
from .types import AggregatedMetric, FeatureVector


# Baseline throughput for window utilisation calculation
# 1 Gbps = 125,000,000 bytes/s = 125,000 bytes/ms
_BASELINE_BYTES_PER_MS = 125_000.0

# RTT thresholds (microseconds)
_RTT_SPIKE_THRESHOLD_US  = 100_000   # 100ms
_RTT_JITTER_THRESHOLD_US =  20_000   # 20ms

# Retransmit rate threshold for packet loss signal
_PACKET_LOSS_THRESHOLD = 0.05


def extract(metric: AggregatedMetric) -> FeatureVector:
    """
    Convert one AggregatedMetric into an 18-dimensional FeatureVector.

    All transformations are deterministic and stateless.
    Raises ValueError if metric contains obviously invalid data.
    """
    # Guard: avoid division by zero
    window_ms       = max(metric.window_size_ms, 1.0)
    total_packets   = max(metric.total_packets, 1)
    rtt_ewma_ms     = metric.rtt_ewma_us / 1_000.0
    rtt_jitter_ms   = metric.rtt_jitter_us / 1_000.0

    # ── Feature computation ───────────────────────────────────────────────────

    # [0] RTT in ms — capped at 2000ms (any higher = total failure, binary)
    f0_rtt_ewma_ms = min(rtt_ewma_ms, 2_000.0)

    # [1] RTT jitter in ms
    f1_rtt_jitter_ms = min(rtt_jitter_ms, 500.0)

    # [2] Jitter-to-RTT ratio — high ratio = unstable path
    f2_rtt_jitter_ratio = (
        rtt_jitter_ms / rtt_ewma_ms
        if rtt_ewma_ms > 0
        else 0.0
    )
    f2_rtt_jitter_ratio = min(f2_rtt_jitter_ratio, 5.0)  # cap at 5×

    # [3] Retransmit rate [0, 1]
    f3_retransmit_rate = min(metric.retransmit_rate, 1.0)

    # [4] Log1p of retransmit rate — de-skews the long tail
    # log1p(0) = 0, log1p(0.01) ≈ 0.01, log1p(1.0) ≈ 0.69
    f4_retransmit_rate_log1p = math.log1p(f3_retransmit_rate * 100) / math.log1p(100)

    # [5] Average anomaly score normalised to [0, 1]
    f5_avg_anomaly_norm = metric.avg_anomaly_score / 100.0

    # [6] Max anomaly score normalised to [0, 1]
    f6_max_anomaly_norm = metric.max_anomaly_score / 100.0

    # [7] Bytes per packet — low = control/overhead traffic; high = bulk data
    f7_bytes_per_packet = min(metric.total_bytes / total_packets, 65535.0) / 65535.0

    # [8] Packets per millisecond — traffic rate
    f8_packets_per_ms = min(metric.total_packets / window_ms, 100_000.0) / 100_000.0

    # [9] Connection initiation rate
    f9_connect_rate = min(metric.connects_observed / window_ms, 1_000.0) / 1_000.0

    # [10] Connection close rate
    f10_close_rate = min(metric.closes_observed / window_ms, 1_000.0) / 1_000.0

    # [11] Retransmits per packet (direct, not rate)
    f11_retrans_per_packet = min(metric.retrans_observed / total_packets, 1.0)

    # [12] Binary: RTT spike (> 100ms)
    f12_rtt_spike = 1.0 if metric.rtt_ewma_us > _RTT_SPIKE_THRESHOLD_US else 0.0

    # [13] Binary: High jitter (> 20ms)
    f13_high_jitter = 1.0 if metric.rtt_jitter_us > _RTT_JITTER_THRESHOLD_US else 0.0

    # [14] Binary: Packet loss signal (retransmit rate > 5%)
    f14_packet_loss = 1.0 if metric.retransmit_rate > _PACKET_LOSS_THRESHOLD else 0.0

    # [15] Binary: SYN flood signal (connects >> closes, ratio > 10:1)
    f15_syn_flood = (
        1.0
        if (metric.closes_observed > 0 and
            metric.connects_observed / max(metric.closes_observed, 1) > 10)
        else 0.0
    )

    # [16] Window bandwidth utilisation (fraction of 1Gbps baseline)
    f16_window_util = min(
        metric.total_bytes / (_BASELINE_BYTES_PER_MS * window_ms),
        2.0  # allow > 1 for > 1Gbps links
    ) / 2.0

    # [17] Anomaly trending: max > avg × 1.5 indicates a spike within the window
    f17_anomaly_trending = (
        1.0
        if (metric.avg_anomaly_score > 0 and
            metric.max_anomaly_score > metric.avg_anomaly_score * 1.5)
        else 0.0
    )

    values = np.array([
        f0_rtt_ewma_ms,
        f1_rtt_jitter_ms,
        f2_rtt_jitter_ratio,
        f3_retransmit_rate,
        f4_retransmit_rate_log1p,
        f5_avg_anomaly_norm,
        f6_max_anomaly_norm,
        f7_bytes_per_packet,
        f8_packets_per_ms,
        f9_connect_rate,
        f10_close_rate,
        f11_retrans_per_packet,
        f12_rtt_spike,
        f13_high_jitter,
        f14_packet_loss,
        f15_syn_flood,
        f16_window_util,
        f17_anomaly_trending,
    ], dtype=np.float32)

    assert len(values) == FeatureVector.N_FEATURES, (
        f"Feature count mismatch: got {len(values)}, expected {FeatureVector.N_FEATURES}"
    )

    return FeatureVector(values=values, source=metric)


def batch_extract(metrics: list[AggregatedMetric]) -> np.ndarray:
    """
    Batch feature extraction. Returns shape (N, 18) float32 array.
    More efficient than calling extract() in a loop for training.
    """
    if not metrics:
        return np.empty((0, FeatureVector.N_FEATURES), dtype=np.float32)

    return np.stack([extract(m).values for m in metrics])
