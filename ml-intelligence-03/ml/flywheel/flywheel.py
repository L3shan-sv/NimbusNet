"""
flywheel.py — The self-improving data loop.

This is the component that makes NimbusNet's ML layer autonomous.
The flywheel closes the loop between production incidents and model improvement:

    1. tc qdisc fault injection (Phase 6) runs in CI/dev
       → generates labeled (features, fault_type, severity) training samples
       → ONE operation (chaos test) produces TWO outputs (test result + training data)

    2. Production failovers are observed
       → the MLScoredMetric at the time of failover is labeled with the actual outcome
       → "did the failover fix it?" = ground truth label

    3. GameDay sessions run quarterly
       → complex multi-fault combinations that synthetic data never covers
       → 10x label quality of synthetic data for rare fault types

    4. When retraining threshold is reached:
       → ALL THREE models are retrained with accumulated data
       → Models are validated against held-out test set
       → If validation passes: hot-swap production models
       → SRE is notified via Slack (FYI, not paged)

Retraining triggers:
    - Every 24 hours (scheduled)
    - After 500 new labeled samples accumulate (volume trigger)
    - After any P0/P1 incident (quality trigger — learn fast)
    - After GameDay session (manual trigger)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional
import numpy as np

from ..models.types import AggregatedMetric, FaultType, FaultSeverity
from ..models.feature_extractor import extract, batch_extract

logger = logging.getLogger(__name__)


# ─── Label Sources ─────────────────────────────────────────────────────────────

class LabelSource(str, Enum):
    SYNTHETIC_QDISC  = "synthetic_qdisc"   # tc qdisc chaos injection (Phase 6)
    PRODUCTION       = "production"        # real production failover, retrospectively labeled
    GAMEDAY          = "gameday"           # manual GameDay session
    BOOTSTRAP        = "bootstrap"         # initial synthetic normal traffic


@dataclass
class TrainingSample:
    """One labeled training example in the flywheel store."""
    features:        list       # 18-float list (serialisable)
    fault_type:      int        # FaultType int value
    severity:        int        # FaultSeverity int value
    is_anomaly:      int        # 1 = anomaly, 0 = normal
    label_source:    str        # LabelSource value
    confidence:      float      # How confident is the label? [0,1]
    timestamp_ns:    int        # When was this sample collected
    session_id:      str        # tc qdisc session or incident ID
    region:          str        # Which region


@dataclass
class FlywheelStats:
    total_samples:    int  = 0
    samples_by_source: dict = field(default_factory=dict)
    samples_by_fault:  dict = field(default_factory=dict)
    last_retrain_at:   Optional[float] = None
    last_retrain_trigger: Optional[str] = None
    retrains_completed: int = 0


class DataFlywheel:
    """
    Manages the training data store and retraining lifecycle.

    Storage: JSONL files (one sample per line) — simple, appendable,
             resumable after crashes, human-readable for debugging.

    Retraining: triggered by schedule, volume, or manual call.
                Runs in a background thread to avoid blocking scoring.
    """

    # How many new samples before triggering a retrain
    RETRAIN_VOLUME_THRESHOLD = 500

    # Minimum samples needed before first retrain
    MIN_SAMPLES_FOR_RETRAIN  = 100

    # Fraction of data held out for validation
    VALIDATION_SPLIT = 0.15

    # Minimum accuracy improvement required to hot-swap models
    MIN_ACCURACY_IMPROVEMENT = 0.01  # 1% improvement threshold

    def __init__(self, store_dir: str | Path):
        self.store_dir   = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)

        self._store_path = self.store_dir / "training_samples.jsonl"
        self._stats_path = self.store_dir / "flywheel_stats.json"
        self._stats      = self._load_stats()
        self._buffer: list[TrainingSample] = []   # in-memory write buffer
        self._samples_since_last_retrain = 0

    # ─── Sample Ingestion ─────────────────────────────────────────────────────

    def ingest_synthetic(
        self,
        metric: AggregatedMetric,
        fault_type:  FaultType,
        severity:    FaultSeverity,
        session_id:  str,
        confidence:  float = 0.95,
    ) -> None:
        """
        Ingest a labeled sample from a tc qdisc chaos injection session.

        Called by Phase 6 (chaos framework) after each fault injection.
        Confidence is high (0.95) because the fault was deliberately injected
        and the label is known with certainty.
        """
        fv = extract(metric)
        sample = TrainingSample(
            features=fv.values.tolist(),
            fault_type=int(fault_type),
            severity=int(severity),
            is_anomaly=int(severity > FaultSeverity.NOMINAL),
            label_source=LabelSource.SYNTHETIC_QDISC.value,
            confidence=confidence,
            timestamp_ns=time.time_ns(),
            session_id=session_id,
            region=metric.region,
        )
        self._append(sample)
        logger.debug(
            "Flywheel: synthetic sample ingested",
            extra={"fault_type": fault_type.name, "severity": severity.name, "session": session_id}
        )

    def ingest_production_outcome(
        self,
        metric: AggregatedMetric,
        actual_fault_type: FaultType,
        actual_severity:   FaultSeverity,
        incident_id:       str,
        confidence:        float = 0.80,
    ) -> None:
        """
        Ingest a sample labeled from a real production incident.

        Called by the SRE operations layer (Phase 5) after postmortem confirms
        the fault type and severity. Confidence is slightly lower (0.80) because
        retrospective labeling can be imprecise.
        """
        fv = extract(metric)
        sample = TrainingSample(
            features=fv.values.tolist(),
            fault_type=int(actual_fault_type),
            severity=int(actual_severity),
            is_anomaly=1,
            label_source=LabelSource.PRODUCTION.value,
            confidence=confidence,
            timestamp_ns=time.time_ns(),
            session_id=incident_id,
            region=metric.region,
        )
        self._append(sample)
        logger.info(
            "Flywheel: production outcome ingested",
            extra={"incident": incident_id, "fault_type": actual_fault_type.name}
        )

    def ingest_normal_baseline(
        self,
        metrics: list[AggregatedMetric],
        session_id: str = "bootstrap",
    ) -> None:
        """
        Ingest normal (no-fault) traffic samples for baseline training.
        Used during bootstrap and after each chaos session's baseline period.
        """
        for metric in metrics:
            fv = extract(metric)
            sample = TrainingSample(
                features=fv.values.tolist(),
                fault_type=int(FaultType.UNKNOWN),
                severity=int(FaultSeverity.NOMINAL),
                is_anomaly=0,
                label_source=LabelSource.BOOTSTRAP.value,
                confidence=0.99,
                timestamp_ns=time.time_ns(),
                session_id=session_id,
                region=metric.region,
            )
            self._append(sample)

        logger.info(
            "Flywheel: normal baseline ingested",
            extra={"count": len(metrics), "session": session_id}
        )

    # ─── Data Loading ─────────────────────────────────────────────────────────

    def load_training_arrays(
        self,
        source_filter: Optional[list[LabelSource]] = None,
        min_confidence: float = 0.60,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Load training data as NumPy arrays.

        Returns:
            features:  shape (N, 18) float32
            fault_labels: shape (N,) int — FaultType
            severity_labels: shape (N,) int — FaultSeverity
        """
        self._flush_buffer()

        samples = self._read_store(source_filter, min_confidence)
        if not samples:
            return (
                np.empty((0, 18), dtype=np.float32),
                np.empty((0,), dtype=np.int32),
                np.empty((0,), dtype=np.int32),
            )

        features         = np.array([s.features for s in samples], dtype=np.float32)
        fault_labels     = np.array([s.fault_type for s in samples], dtype=np.int32)
        severity_labels  = np.array([s.severity for s in samples], dtype=np.int32)

        return features, fault_labels, severity_labels

    def load_anomaly_split(
        self,
        min_confidence: float = 0.70,
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        Load data split into normal/anomaly for Isolation Forest training.

        Returns:
            normal_features:  shape (N_normal, 18)
            anomaly_features: shape (N_anomaly, 18)
        """
        features, _, severity_labels = self.load_training_arrays(min_confidence=min_confidence)

        if len(features) == 0:
            return np.empty((0, 18), dtype=np.float32), np.empty((0, 18), dtype=np.float32)

        normal_mask  = severity_labels == FaultSeverity.NOMINAL
        anomaly_mask = severity_labels >= FaultSeverity.DEGRADED

        return features[normal_mask], features[anomaly_mask]

    def should_retrain(self) -> bool:
        """Check whether any retrain trigger condition is met."""
        total = self._stats.total_samples
        return (
            total >= self.MIN_SAMPLES_FOR_RETRAIN and
            self._samples_since_last_retrain >= self.RETRAIN_VOLUME_THRESHOLD
        )

    # ─── Stats ────────────────────────────────────────────────────────────────

    def stats(self) -> FlywheelStats:
        return self._stats

    def sample_count(self) -> int:
        return self._stats.total_samples

    # ─── Internal ─────────────────────────────────────────────────────────────

    def _append(self, sample: TrainingSample) -> None:
        """Add sample to write buffer. Flush buffer when it reaches 100 samples."""
        self._buffer.append(sample)
        self._stats.total_samples += 1
        self._samples_since_last_retrain += 1

        src = sample.label_source
        self._stats.samples_by_source[src] = self._stats.samples_by_source.get(src, 0) + 1

        ft = FaultType(sample.fault_type).name
        self._stats.samples_by_fault[ft] = self._stats.samples_by_fault.get(ft, 0) + 1

        if len(self._buffer) >= 100:
            self._flush_buffer()

    def _flush_buffer(self) -> None:
        if not self._buffer:
            return
        with open(self._store_path, "a") as f:
            for sample in self._buffer:
                f.write(json.dumps(asdict(sample)) + "\n")
        self._buffer.clear()
        self._save_stats()

    def _read_store(
        self,
        source_filter: Optional[list[LabelSource]],
        min_confidence: float,
    ) -> list[TrainingSample]:
        if not self._store_path.exists():
            return []

        allowed_sources = {s.value for s in source_filter} if source_filter else None
        samples = []

        with open(self._store_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("confidence", 0) < min_confidence:
                        continue
                    if allowed_sources and d.get("label_source") not in allowed_sources:
                        continue
                    samples.append(TrainingSample(**d))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue

        return samples

    def _load_stats(self) -> FlywheelStats:
        if self._stats_path.exists():
            try:
                with open(self._stats_path) as f:
                    d = json.load(f)
                return FlywheelStats(**d)
            except Exception:
                pass
        return FlywheelStats()

    def _save_stats(self) -> None:
        with open(self._stats_path, "w") as f:
            json.dump(asdict(self._stats), f, indent=2, default=str)
