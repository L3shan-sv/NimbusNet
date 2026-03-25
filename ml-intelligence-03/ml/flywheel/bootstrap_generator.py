"""
bootstrap_generator.py — Synthetic training data generator.

Generates the initial training dataset before any production data is available.
Simulates the statistical properties of each fault type and severity level,
producing labeled (feature_vector, fault_type, severity) tuples.

This is used:
    1. At first deployment — no production data exists yet
    2. During tc qdisc chaos sessions — augments real injection data
    3. During unit tests — deterministic, reproducible

The generator is carefully calibrated against real AWS inter-region traffic
patterns. Distributions are based on empirical measurements:
    - Baseline RTT us-east-1 → eu-west-1: 80-100ms
    - Normal retransmit rate: 0.001-0.005 (0.1-0.5%)
    - Healthy jitter: 2-10ms
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator
import numpy as np

from ..models.types import AggregatedMetric, FaultType, FaultSeverity

logger = logging.getLogger(__name__)


@dataclass
class FaultProfile:
    """Statistical profile for generating synthetic traffic of a given fault type."""
    fault_type:  FaultType
    severity:    FaultSeverity
    # RTT: (mean_us, std_us, min_us, max_us)
    rtt:         tuple
    jitter:      tuple  # (mean_us, std_us)
    retransmit:  tuple  # (mean, std, min, max) — fraction
    anomaly_score: tuple  # (mean, std)


FAULT_PROFILES: list[FaultProfile] = [
    # ─── Normal baseline ───────────────────────────────────────────────
    FaultProfile(
        fault_type=FaultType.UNKNOWN,
        severity=FaultSeverity.NOMINAL,
        rtt=(85_000, 8_000, 60_000, 110_000),       # 85ms ± 8ms
        jitter=(4_000, 1_500),                       # 4ms jitter
        retransmit=(0.002, 0.001, 0.0, 0.008),       # 0.2%
        anomaly_score=(5, 3),
    ),

    # ─── Latency spike (warning) ────────────────────────────────────────
    FaultProfile(
        fault_type=FaultType.NETWORK_LATENCY,
        severity=FaultSeverity.WARNING,
        rtt=(120_000, 20_000, 95_000, 180_000),     # 120ms ± 20ms
        jitter=(15_000, 5_000),
        retransmit=(0.01, 0.005, 0.002, 0.03),
        anomaly_score=(30, 10),
    ),

    # ─── Packet loss (degraded) ─────────────────────────────────────────
    FaultProfile(
        fault_type=FaultType.PACKET_LOSS,
        severity=FaultSeverity.DEGRADED,
        rtt=(150_000, 30_000, 100_000, 250_000),
        jitter=(25_000, 8_000),
        retransmit=(0.08, 0.03, 0.03, 0.20),        # 8% retransmit
        anomaly_score=(60, 12),
    ),

    # ─── Region partition (critical) ────────────────────────────────────
    FaultProfile(
        fault_type=FaultType.REGION_PARTITION,
        severity=FaultSeverity.CRITICAL,
        rtt=(800_000, 200_000, 400_000, 2_000_000),  # 800ms — near timeout
        jitter=(80_000, 40_000),
        retransmit=(0.40, 0.15, 0.20, 0.80),
        anomaly_score=(85, 8),
    ),

    # ─── Congestion (degraded) ──────────────────────────────────────────
    FaultProfile(
        fault_type=FaultType.CONGESTION,
        severity=FaultSeverity.DEGRADED,
        rtt=(200_000, 50_000, 130_000, 400_000),
        jitter=(40_000, 15_000),
        retransmit=(0.05, 0.02, 0.01, 0.15),
        anomaly_score=(50, 15),
    ),

    # ─── SYN flood (critical) ───────────────────────────────────────────
    FaultProfile(
        fault_type=FaultType.SYN_FLOOD,
        severity=FaultSeverity.CRITICAL,
        rtt=(90_000, 10_000, 70_000, 130_000),       # RTT looks normal — flood is subtle
        jitter=(8_000, 3_000),
        retransmit=(0.003, 0.001, 0.0, 0.01),
        anomaly_score=(75, 10),
    ),

    # ─── Cascading failure (critical) ───────────────────────────────────
    FaultProfile(
        fault_type=FaultType.CASCADING,
        severity=FaultSeverity.CRITICAL,
        rtt=(350_000, 100_000, 150_000, 1_000_000),
        jitter=(60_000, 30_000),
        retransmit=(0.25, 0.10, 0.10, 0.60),
        anomaly_score=(90, 5),
    ),
]


class BootstrapGenerator:
    """
    Generates synthetic AggregatedMetric samples for each fault profile.

    All samples are tagged with region="us-east-1" by default.
    In production these would be spread across all three regions.
    """

    def __init__(self, seed: int = 42):
        self._rng = np.random.default_rng(seed)

    def generate(
        self,
        profile:  FaultProfile,
        n:        int,
        region:   str = "us-east-1",
    ) -> list[AggregatedMetric]:
        """Generate n synthetic metrics matching the given fault profile."""
        samples = []
        for _ in range(n):
            metric = self._sample_metric(profile, region)
            samples.append(metric)
        return samples

    def generate_balanced_dataset(
        self,
        samples_per_class: int = 500,
        region: str = "us-east-1",
    ) -> list[tuple[AggregatedMetric, FaultType, FaultSeverity]]:
        """
        Generate a balanced dataset across all fault profiles.

        Returns list of (metric, fault_type, severity) tuples ready for
        DataFlywheel.ingest_*() calls.
        """
        dataset = []
        for profile in FAULT_PROFILES:
            metrics = self.generate(profile, samples_per_class, region)
            for m in metrics:
                dataset.append((m, profile.fault_type, profile.severity))

        # Shuffle so the order doesn't bias training
        indices = self._rng.permutation(len(dataset))
        return [dataset[i] for i in indices]

    def _sample_metric(self, profile: FaultProfile, region: str) -> AggregatedMetric:
        """Sample one metric from a fault profile's distribution."""
        rng = self._rng

        # RTT
        rtt_mean, rtt_std, rtt_min, rtt_max = profile.rtt
        rtt_us = int(np.clip(rng.normal(rtt_mean, rtt_std), rtt_min, rtt_max))

        # Jitter
        jitter_mean, jitter_std = profile.jitter
        jitter_us = int(max(0, rng.normal(jitter_mean, jitter_std)))

        # Retransmit
        rt_mean, rt_std, rt_min, rt_max = profile.retransmit
        retransmit_rate = float(np.clip(rng.normal(rt_mean, rt_std), rt_min, rt_max))

        # Anomaly score
        score_mean, score_std = profile.anomaly_score
        avg_score = float(np.clip(rng.normal(score_mean, score_std), 0, 100))
        max_score = int(np.clip(avg_score + rng.exponential(10), avg_score, 100))

        # Volume — realistic inter-region traffic
        total_packets = int(rng.integers(50, 5000))
        total_bytes   = total_packets * int(rng.integers(200, 1400))

        # TCP lifecycle
        connects = int(rng.integers(1, 20))
        closes   = connects if profile.severity == FaultSeverity.NOMINAL else max(1, connects // 3)
        retrans  = int(total_packets * retransmit_rate)

        # SYN flood: many connects, few closes
        if profile.fault_type == FaultType.SYN_FLOOD:
            connects = int(rng.integers(500, 5000))
            closes   = int(rng.integers(1, 10))

        now_ns = int(1_700_000_000_000_000_000 + rng.integers(0, 1_000_000_000_000))

        return AggregatedMetric(
            window_start_ns=now_ns,
            window_end_ns=now_ns + 50_000_000,  # 50ms window
            src_ip=f"10.{rng.integers(0,255)}.{rng.integers(0,255)}.{rng.integers(1,254)}",
            dst_ip=f"10.{rng.integers(0,255)}.{rng.integers(0,255)}.{rng.integers(1,254)}",
            region=region,
            total_bytes=total_bytes,
            total_packets=total_packets,
            retransmit_rate=retransmit_rate,
            rtt_ewma_us=rtt_us,
            rtt_jitter_us=jitter_us,
            max_anomaly_score=max_score,
            avg_anomaly_score=avg_score,
            connects_observed=connects,
            closes_observed=closes,
            retrans_observed=retrans,
            is_local=True,
        )
