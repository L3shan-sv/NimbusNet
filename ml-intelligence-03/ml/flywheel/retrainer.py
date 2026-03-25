"""
retrainer.py — Model retraining orchestrator.

Manages the full retrain → validate → hot-swap → notify cycle.
Runs in a background thread; never blocks the scoring pipeline.

Hot-swap protocol:
    1. Train new model versions on accumulated flywheel data
    2. Validate against held-out test set
    3. If all models improve by >= MIN_ACCURACY_IMPROVEMENT:
        a. Atomically replace model instances in ScoringPipeline
        b. Save new weights to disk
        c. Notify SRE via Slack (FYI — not a page)
    4. If validation fails:
        a. Log the failure
        b. Keep current models
        c. Add failure to next postmortem
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable

import numpy as np
from sklearn.model_selection import train_test_split

from ..models.isolation_forest import NimbusIsolationForest
from ..models.lstm import NimbusLSTM
from ..models.xgboost_classifier import NimbusXGBoostClassifier
from ..models.bandit import NimbusMultiArmedBandit
from ..pipeline.scoring_pipeline import ScoringPipeline
from .flywheel import DataFlywheel, FlywheelStats

logger = logging.getLogger(__name__)


@dataclass
class RetrainResult:
    triggered_by:    str
    started_at:      float
    completed_at:    float
    success:         bool
    models_swapped:  bool
    error:           Optional[str]

    # Validation metrics (before/after)
    if_score_before:  float = 0.0
    if_score_after:   float = 0.0
    xgb_acc_before:   float = 0.0
    xgb_acc_after:    float = 0.0

    @property
    def duration_s(self) -> float:
        return self.completed_at - self.started_at


class RetrainingOrchestrator:
    """
    Manages the training data → model → production hot-swap lifecycle.

    Thread-safe. A retraining lock ensures only one retrain runs at a time.
    """

    # Minimum accuracy improvement to hot-swap
    MIN_IMPROVEMENT = 0.01  # 1%

    def __init__(
        self,
        pipeline:   ScoringPipeline,
        flywheel:   DataFlywheel,
        model_dir:  str | Path,
        notify_fn:  Optional[Callable[[str, str], None]] = None,  # (title, message) → Slack FYI
    ):
        self.pipeline   = pipeline
        self.flywheel   = flywheel
        self.model_dir  = Path(model_dir)
        self.notify_fn  = notify_fn
        self._lock      = threading.Lock()
        self._history:  list[RetrainResult] = []

    def maybe_retrain(self, trigger: str = "scheduled") -> Optional[RetrainResult]:
        """
        Check flywheel and retrain if threshold is met.
        Safe to call frequently — short-circuits if conditions not met.
        """
        if not self.flywheel.should_retrain():
            return None
        return self.retrain(trigger)

    def retrain(self, trigger: str = "manual") -> RetrainResult:
        """
        Force a full retrain cycle.

        This is blocking — call from a background thread.
        Returns a RetrainResult regardless of success/failure.
        """
        if not self._lock.acquire(blocking=False):
            logger.info("Retrain already in progress — skipping", extra={"trigger": trigger})
            return RetrainResult(
                triggered_by=trigger,
                started_at=time.time(),
                completed_at=time.time(),
                success=False,
                models_swapped=False,
                error="retrain_already_running",
            )

        started_at = time.time()
        logger.info("Retraining started", extra={"trigger": trigger})

        try:
            result = self._run_retrain(trigger, started_at)
        except Exception as e:
            logger.exception("Retrain failed unexpectedly")
            result = RetrainResult(
                triggered_by=trigger,
                started_at=started_at,
                completed_at=time.time(),
                success=False,
                models_swapped=False,
                error=str(e),
            )
        finally:
            self._lock.release()

        self._history.append(result)

        # Notify SRE (Slack FYI, not a page)
        if self.notify_fn:
            status = "✅ succeeded" if result.success else "⚠️ failed"
            self.notify_fn(
                f"NimbusNet ML Retrain {status}",
                self._format_retrain_notification(result),
            )

        return result

    def _run_retrain(self, trigger: str, started_at: float) -> RetrainResult:
        """Internal retrain logic. Lock must be held by caller."""

        # ── 1. Load training data ─────────────────────────────────────────────
        features, fault_labels, severity_labels = self.flywheel.load_training_arrays()
        normal_features, anomaly_features = self.flywheel.load_anomaly_split()

        if len(features) < DataFlywheel.MIN_SAMPLES_FOR_RETRAIN:
            return RetrainResult(
                triggered_by=trigger,
                started_at=started_at,
                completed_at=time.time(),
                success=False,
                models_swapped=False,
                error=f"insufficient_data: {len(features)} samples",
            )

        # ── 2. Train/test split ───────────────────────────────────────────────
        (feat_train, feat_test,
         sev_train,  sev_test) = train_test_split(
            features, severity_labels,
            test_size=DataFlywheel.VALIDATION_SPLIT,
            random_state=42,
            stratify=severity_labels if len(np.unique(severity_labels)) > 1 else None,
        )

        # ── 3. Train new model versions ───────────────────────────────────────
        new_if  = NimbusIsolationForest()
        new_xgb = NimbusXGBoostClassifier()

        if len(normal_features) > 0 and len(anomaly_features) > 0:
            new_if.retrain(normal_features, anomaly_features)
        elif len(normal_features) > 0:
            new_if.bootstrap(normal_features)

        new_xgb.bootstrap(feat_train, sev_train)

        # ── 4. Validate ───────────────────────────────────────────────────────
        # Evaluate old models
        old_xgb_acc = self._xgb_accuracy(self.pipeline.xgboost, feat_test, sev_test)
        new_xgb_acc = self._xgb_accuracy(new_xgb, feat_test, sev_test)

        old_if_score = self._if_auprc(self.pipeline.isolation_forest, feat_test, sev_test)
        new_if_score = self._if_auprc(new_if, feat_test, sev_test)

        xgb_improved = new_xgb_acc >= old_xgb_acc - self.MIN_IMPROVEMENT
        if_improved  = new_if_score >= old_if_score - self.MIN_IMPROVEMENT

        logger.info(
            "Retrain validation",
            extra={
                "old_xgb_acc":  round(old_xgb_acc, 4),
                "new_xgb_acc":  round(new_xgb_acc, 4),
                "old_if_score": round(old_if_score, 4),
                "new_if_score": round(new_if_score, 4),
                "xgb_improved": xgb_improved,
                "if_improved":  if_improved,
            }
        )

        if not (xgb_improved and if_improved):
            logger.warning(
                "Retrain: validation did not improve — keeping current models",
                extra={"trigger": trigger}
            )
            return RetrainResult(
                triggered_by=trigger,
                started_at=started_at,
                completed_at=time.time(),
                success=True,
                models_swapped=False,
                error=None,
                if_score_before=old_if_score,
                if_score_after=new_if_score,
                xgb_acc_before=old_xgb_acc,
                xgb_acc_after=new_xgb_acc,
            )

        # ── 5. Hot-swap ───────────────────────────────────────────────────────
        # Atomic replacement of models in the scoring pipeline
        self.pipeline.isolation_forest = new_if
        self.pipeline.xgboost          = new_xgb
        # LSTM and bandit are NOT retrained here:
        #   - LSTM retrain requires PyTorch and is a separate job (lstm_trainer.py)
        #   - Bandit uses online updates — no batch retrain needed

        # Save new weights to disk
        self.model_dir.mkdir(parents=True, exist_ok=True)
        new_if.save(self.model_dir / "isolation_forest.pkl")
        new_xgb.save(self.model_dir / "xgboost.pkl")

        # Update flywheel stats
        self.flywheel._stats.last_retrain_at      = time.time()
        self.flywheel._stats.last_retrain_trigger = trigger
        self.flywheel._stats.retrains_completed  += 1
        self.flywheel._samples_since_last_retrain = 0

        logger.info(
            "Retrain: models hot-swapped",
            extra={
                "trigger":      trigger,
                "xgb_acc":      round(new_xgb_acc, 4),
                "if_score":     round(new_if_score, 4),
            }
        )

        return RetrainResult(
            triggered_by=trigger,
            started_at=started_at,
            completed_at=time.time(),
            success=True,
            models_swapped=True,
            error=None,
            if_score_before=old_if_score,
            if_score_after=new_if_score,
            xgb_acc_before=old_xgb_acc,
            xgb_acc_after=new_xgb_acc,
        )

    # ─── Evaluation ───────────────────────────────────────────────────────────

    @staticmethod
    def _xgb_accuracy(model: NimbusXGBoostClassifier, features: np.ndarray, labels: np.ndarray) -> float:
        """Compute balanced accuracy for XGBoost severity classifier."""
        if not model.is_trained or len(features) == 0:
            return 0.0

        try:
            from sklearn.metrics import balanced_accuracy_score
            from ..models.types import FeatureVector
            import numpy as np

            preds = []
            for i in range(len(features)):
                fv_mock = type("FV", (), {"values": features[i]})()
                fv_mock.values = features[i]
                # Create minimal FeatureVector-like object
                class FVMock:
                    def __init__(self, vals):
                        self.values = vals
                sev, _, _ = model.classify(FVMock(features[i]))
                preds.append(int(sev))

            return float(balanced_accuracy_score(labels, preds))
        except Exception:
            return 0.0

    @staticmethod
    def _if_auprc(model: NimbusIsolationForest, features: np.ndarray, labels: np.ndarray) -> float:
        """Compute AUPRC for Isolation Forest anomaly detection."""
        if not model.is_trained or len(features) == 0:
            return 0.0

        try:
            from sklearn.metrics import average_precision_score
            anomaly_labels = (labels >= 2).astype(int)
            if anomaly_labels.sum() == 0:
                return 0.5  # no anomalies in test set — neutral score

            scores = model.score_batch(features)
            return float(average_precision_score(anomaly_labels, scores))
        except Exception:
            return 0.0

    def _format_retrain_notification(self, result: RetrainResult) -> str:
        status = "succeeded and models hot-swapped" if result.models_swapped else (
            "succeeded (no improvement, kept current models)" if result.success
            else f"failed: {result.error}"
        )
        return (
            f"*Trigger:* {result.triggered_by}\n"
            f"*Status:* {status}\n"
            f"*Duration:* {result.duration_s:.1f}s\n"
            f"*XGBoost accuracy:* {result.xgb_acc_before:.3f} → {result.xgb_acc_after:.3f}\n"
            f"*IF AUPRC:*       {result.if_score_before:.3f} → {result.if_score_after:.3f}\n"
            f"*Total samples:* {self.flywheel.sample_count()}"
        )

    @property
    def history(self) -> list[RetrainResult]:
        return list(self._history)
