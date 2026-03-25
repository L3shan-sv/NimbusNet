"""
isolation_forest.py — Unsupervised anomaly detection.

The Isolation Forest is the first model in the scoring pipeline.
It requires NO labeled data — it learns what "normal" looks like
from the bootstrap synthetic dataset and improves via the flywheel.

Why Isolation Forest for this use case:
    - Works with zero labels at bootstrap time
    - Handles the high-dimensional (18-feature) telemetry naturally
    - O(n log n) training, O(log n) inference — fast enough for 50ms windows
    - Anomaly score maps cleanly to probability via sigmoid calibration
    - Robust to the class imbalance problem (anomalies are rare)

Calibration:
    Raw IF scores are in [-0.5, 0.5]. We apply isotonic regression
    calibration after initial training to map to probability [0, 1].
    Calibration data comes from the tc qdisc synthetic fault sessions.
"""

from __future__ import annotations

import os
import pickle
import time
import logging
from pathlib import Path
from typing import Optional

import numpy as np
from sklearn.ensemble import IsolationForest as SKLearnIF
from sklearn.calibration import CalibratedClassifierCV
from sklearn.isotonic import IsotonicRegression

from .types import FeatureVector

logger = logging.getLogger(__name__)


class NimbusIsolationForest:
    """
    Wrapped Isolation Forest with probability calibration and versioning.

    Lifecycle:
        1. bootstrap()    — train on synthetic normal traffic
        2. score()        — production inference (< 1ms per call)
        3. update()       — online retraining when flywheel provides new data
        4. save() / load()— persist to disk for agent restarts
    """

    VERSION = "1.0.0"

    # Anomaly probability threshold above which we flag for the ensemble
    ANOMALY_THRESHOLD = 0.70

    def __init__(
        self,
        n_estimators: int = 200,
        max_samples: int = 512,
        contamination: float = 0.05,
        random_state: int = 42,
    ):
        """
        Args:
            n_estimators:  Number of isolation trees. 200 balances accuracy vs speed.
            max_samples:   Subsampling per tree. 512 is the sweet spot from the original IF paper.
            contamination: Prior probability of anomalies. 5% is conservative for production.
            random_state:  Reproducibility seed.
        """
        self.n_estimators  = n_estimators
        self.max_samples   = max_samples
        self.contamination = contamination
        self.random_state  = random_state

        self._model: Optional[SKLearnIF] = None
        self._calibrator: Optional[IsotonicRegression] = None
        self._trained_at: Optional[float] = None
        self._training_samples: int = 0
        self._version_hash: str = ""

    # ─── Training ──────────────────────────────────────────────────────────────

    def bootstrap(self, normal_features: np.ndarray) -> None:
        """
        Initial training on synthetic normal traffic from tc qdisc sessions.

        Args:
            normal_features: shape (N, 18) — feature vectors from baseline sessions
                             (no faults injected). These are the "normal" examples.
        """
        if normal_features.shape[1] != FeatureVector.N_FEATURES:
            raise ValueError(
                f"Expected {FeatureVector.N_FEATURES} features, got {normal_features.shape[1]}"
            )

        logger.info(
            "IsolationForest bootstrap training",
            extra={"samples": len(normal_features), "n_estimators": self.n_estimators}
        )

        t0 = time.perf_counter()
        self._model = SKLearnIF(
            n_estimators=self.n_estimators,
            max_samples=min(self.max_samples, len(normal_features)),
            contamination=self.contamination,
            random_state=self.random_state,
            n_jobs=-1,  # use all cores for training
        )
        self._model.fit(normal_features)

        # Bootstrap calibrator with uniform prior (no labeled data yet)
        # Calibration is updated by the flywheel when fault labels arrive
        raw_scores = self._model.score_samples(normal_features)
        self._fit_calibrator_unsupervised(raw_scores)

        elapsed = (time.perf_counter() - t0) * 1000
        self._trained_at = time.time()
        self._training_samples = len(normal_features)
        self._version_hash = self._compute_hash()

        logger.info(
            "IsolationForest bootstrap complete",
            extra={"elapsed_ms": round(elapsed, 1), "samples": len(normal_features)}
        )

    def retrain(
        self,
        normal_features: np.ndarray,
        anomaly_features: np.ndarray,
    ) -> None:
        """
        Full retrain with both normal and labeled anomaly data from the flywheel.

        The IF model itself is unsupervised (ignores anomaly_features for training),
        but the anomaly features are used to calibrate the score → probability mapping.

        Args:
            normal_features:  shape (N, 18) — baseline traffic features
            anomaly_features: shape (M, 18) — features from confirmed fault sessions
        """
        all_features = np.vstack([normal_features, anomaly_features])
        labels = np.concatenate([
            np.zeros(len(normal_features)),
            np.ones(len(anomaly_features))
        ])

        logger.info(
            "IsolationForest retrain",
            extra={
                "normal_samples": len(normal_features),
                "anomaly_samples": len(anomaly_features),
            }
        )

        t0 = time.perf_counter()
        self._model = SKLearnIF(
            n_estimators=self.n_estimators,
            max_samples=min(self.max_samples, len(all_features)),
            contamination=len(anomaly_features) / len(all_features),
            random_state=self.random_state,
            n_jobs=-1,
        )
        self._model.fit(all_features)

        # Calibrate using labeled data — much better than unsupervised calibration
        raw_scores = self._model.score_samples(all_features)
        self._fit_calibrator_supervised(raw_scores, labels)

        elapsed = (time.perf_counter() - t0) * 1000
        self._trained_at = time.time()
        self._training_samples = len(all_features)
        self._version_hash = self._compute_hash()

        logger.info(
            "IsolationForest retrain complete",
            extra={"elapsed_ms": round(elapsed, 1)}
        )

    # ─── Inference ────────────────────────────────────────────────────────────

    def score(self, fv: FeatureVector) -> float:
        """
        Score a single feature vector. Returns anomaly probability [0, 1].
        Raises RuntimeError if model has not been trained.

        Target latency: < 0.5ms (well under the 50ms aggregation window).
        """
        if self._model is None:
            raise RuntimeError("Model not trained. Call bootstrap() first.")

        # score_samples returns negative values; more negative = more anomalous
        raw = self._model.score_samples(fv.values.reshape(1, -1))[0]

        if self._calibrator is not None:
            # Calibrate to probability using isotonic regression
            prob = float(self._calibrator.predict([raw])[0])
        else:
            # Fallback: normalise raw score to [0, 1] without calibration
            prob = self._raw_to_prob(raw)

        return float(np.clip(prob, 0.0, 1.0))

    def score_batch(self, features: np.ndarray) -> np.ndarray:
        """
        Score a batch of feature vectors. Returns shape (N,) float32.
        Used by the training pipeline to score historical data.
        """
        if self._model is None:
            raise RuntimeError("Model not trained.")

        raw = self._model.score_samples(features)

        if self._calibrator is not None:
            probs = self._calibrator.predict(raw)
        else:
            probs = np.vectorize(self._raw_to_prob)(raw)

        return np.clip(probs, 0.0, 1.0).astype(np.float32)

    def is_anomaly(self, fv: FeatureVector) -> tuple[bool, float]:
        """
        Returns (is_anomaly, probability) for quick boolean decisions.
        """
        prob = self.score(fv)
        return prob >= self.ANOMALY_THRESHOLD, prob

    # ─── Persistence ──────────────────────────────────────────────────────────

    def save(self, path: str | Path) -> None:
        """Serialize model to disk."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version":           self.VERSION,
            "model":             self._model,
            "calibrator":        self._calibrator,
            "trained_at":        self._trained_at,
            "training_samples":  self._training_samples,
            "version_hash":      self._version_hash,
            "hyperparams": {
                "n_estimators":  self.n_estimators,
                "max_samples":   self.max_samples,
                "contamination": self.contamination,
            }
        }
        with open(path, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        logger.info("IsolationForest saved", extra={"path": str(path)})

    @classmethod
    def load(cls, path: str | Path) -> "NimbusIsolationForest":
        """Deserialize model from disk."""
        with open(path, "rb") as f:
            payload = pickle.load(f)

        params = payload.get("hyperparams", {})
        inst = cls(
            n_estimators=params.get("n_estimators", 200),
            max_samples=params.get("max_samples", 512),
            contamination=params.get("contamination", 0.05),
        )
        inst._model             = payload["model"]
        inst._calibrator        = payload.get("calibrator")
        inst._trained_at        = payload.get("trained_at")
        inst._training_samples  = payload.get("training_samples", 0)
        inst._version_hash      = payload.get("version_hash", "")

        logger.info(
            "IsolationForest loaded",
            extra={
                "path": str(path),
                "samples": inst._training_samples,
                "version_hash": inst._version_hash,
            }
        )
        return inst

    # ─── Internals ────────────────────────────────────────────────────────────

    def _fit_calibrator_unsupervised(self, raw_scores: np.ndarray) -> None:
        """Bootstrap calibration without labels — maps raw score distribution to [0,1]."""
        # Rank-based calibration: bottom 5% → high anomaly probability
        percentiles = np.percentile(raw_scores, [5, 50, 95])
        # Map: [min_score, p5, p50, p95, max_score] → [1.0, 0.9, 0.3, 0.05, 0.0]
        x = np.array([raw_scores.min(), percentiles[0], percentiles[1], percentiles[2], raw_scores.max()])
        y = np.array([1.0, 0.9, 0.3, 0.05, 0.0])
        self._calibrator = IsotonicRegression(out_of_bounds="clip")
        self._calibrator.fit(x, y)

    def _fit_calibrator_supervised(self, raw_scores: np.ndarray, labels: np.ndarray) -> None:
        """Calibration with ground truth labels (from flywheel)."""
        self._calibrator = IsotonicRegression(out_of_bounds="clip")
        self._calibrator.fit(raw_scores, labels)

    @staticmethod
    def _raw_to_prob(raw: float) -> float:
        """Emergency fallback: map IF raw score to [0,1] via sigmoid."""
        # IF scores in [-0.5, 0.5]; more negative = more anomalous
        # Negate and apply sigmoid centred at 0
        return 1.0 / (1.0 + math.exp(raw * 10))

    def _compute_hash(self) -> str:
        """Compute a short version hash for model tracking."""
        import hashlib
        data = f"{self.n_estimators}:{self._training_samples}:{self._trained_at}"
        return hashlib.sha256(data.encode()).hexdigest()[:12]

    @property
    def is_trained(self) -> bool:
        return self._model is not None

    @property
    def metadata(self) -> dict:
        return {
            "version":          self.VERSION,
            "version_hash":     self._version_hash,
            "trained_at":       self._trained_at,
            "training_samples": self._training_samples,
            "n_estimators":     self.n_estimators,
            "contamination":    self.contamination,
            "threshold":        self.ANOMALY_THRESHOLD,
        }


import math  # for _raw_to_prob fallback
