"""
scoring_pipeline.py — Orchestrates all four ML models into a single scored output.

This is the single entry point for the ML layer. The control plane (Phase 4)
calls score() on every AggregatedMetric received from the Go agent.

Pipeline:
    AggregatedMetric
        │
        ▼ feature_extractor.extract()
    FeatureVector (18 dims)
        │
        ├──► IsolationForest.score()    → anomaly_probability
        ├──► LSTM.push() + predict()    → fault_type, lstm_confidence
        ├──► XGBoost.classify()         → severity, xgb_confidence
        └──► Bandit.recommend_weights() → routing_weights
        │
        ▼ _ensemble_decision()
    MLScoredMetric
        │
        ├──► Bus.ScoredMetrics  (→ control plane, Phase 4)
        └──► DataStore          (→ flywheel training buffer)

Ensemble decision logic:
    should_trigger_failover = True if ANY of:
        - isolation_forest_score   > 0.70
        - xgboost_severity        >= DEGRADED  AND  lstm_confidence > 0.60
        - bandit_routing_weight    < 0.20       AND  isolation_forest_score > 0.50

    failover_confidence = weighted average of model confidences
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .types import AggregatedMetric, MLScoredMetric, FaultSeverity, FaultType
from .feature_extractor import extract
from .isolation_forest import NimbusIsolationForest
from .lstm import NimbusLSTM
from .xgboost_classifier import NimbusXGBoostClassifier
from .bandit import NimbusMultiArmedBandit

logger = logging.getLogger(__name__)


class ScoringPipeline:
    """
    Orchestrates all four ML models. Thread-safe for concurrent scoring calls.
    Maintains per-flow LSTM windows keyed by flow identity string.
    """

    # Ensemble thresholds
    IF_FAILOVER_THRESHOLD     = 0.70   # IF score above this → consider failover
    LSTM_CONFIDENCE_MIN       = 0.60   # LSTM must be this confident to influence ensemble
    XGB_DEGRADED_THRESHOLD    = FaultSeverity.DEGRADED
    BANDIT_LOW_WEIGHT         = 0.20   # bandit weight below this = region probably bad
    IF_CORROBORATE_THRESHOLD  = 0.50   # IF score needed to corroborate bandit signal

    def __init__(
        self,
        isolation_forest: NimbusIsolationForest,
        lstm: NimbusLSTM,
        xgboost: NimbusXGBoostClassifier,
        bandit: NimbusMultiArmedBandit,
    ):
        self.isolation_forest = isolation_forest
        self.lstm_template    = lstm   # used as a template; per-flow instances are cloned
        self.xgboost          = xgboost
        self.bandit           = bandit

        # Per-flow LSTM instances (keyed by flow fingerprint)
        # Each flow has its own sliding window so sequences don't bleed across flows
        self._flow_lstms: dict[str, NimbusLSTM] = {}

        # Previous RTT per region for bandit observe() calls
        self._prev_rtt_by_region: dict[str, float] = {}

    def score(self, metric: AggregatedMetric) -> MLScoredMetric:
        """
        Score one AggregatedMetric through all four models.

        This is the hot path — called every 50ms per flow.
        Target latency: < 2ms end-to-end.
        """
        t0 = time.perf_counter()

        # ── 1. Feature extraction ─────────────────────────────────────────────
        fv = extract(metric)

        # ── 2. Isolation Forest ───────────────────────────────────────────────
        if_score = self.isolation_forest.score(fv)

        # ── 3. LSTM ───────────────────────────────────────────────────────────
        flow_key = f"{metric.src_ip}→{metric.dst_ip}"
        lstm     = self._get_flow_lstm(flow_key)
        lstm.push(fv)
        fault_type, lstm_conf, _ = lstm.predict()

        # ── 4. XGBoost ────────────────────────────────────────────────────────
        severity, xgb_conf, _ = self.xgboost.classify(fv, fault_type_hint=fault_type)

        # ── 5. Bandit ─────────────────────────────────────────────────────────
        weights = self.bandit.recommend_weights()
        region_weight = weights.get(metric.region, 1.0)

        # Observe the previous routing decision's outcome
        if metric.region in self._prev_rtt_by_region and metric.rtt_ewma_us > 0:
            self.bandit.observe(
                region_id=metric.region,
                old_rtt_us=self._prev_rtt_by_region[metric.region],
                new_rtt_us=float(metric.rtt_ewma_us),
            )
        if metric.rtt_ewma_us > 0:
            self._prev_rtt_by_region[metric.region] = float(metric.rtt_ewma_us)

        # ── 6. Ensemble decision ──────────────────────────────────────────────
        should_failover, failover_conf = self._ensemble_decision(
            if_score=if_score,
            severity=severity,
            xgb_conf=xgb_conf,
            lstm_conf=lstm_conf,
            region_weight=region_weight,
        )

        scoring_latency_us = (time.perf_counter() - t0) * 1_000_000

        result = MLScoredMetric(
            source=metric,
            features=fv,
            isolation_forest_score=if_score,
            lstm_fault_type=fault_type,
            lstm_confidence=lstm_conf,
            xgboost_severity=severity,
            xgboost_confidence=xgb_conf,
            bandit_routing_weight=region_weight,
            should_trigger_failover=should_failover,
            failover_confidence=failover_conf,
            model_versions={
                "isolation_forest": self.isolation_forest._version_hash,
                "lstm":             lstm.metadata["version_hash"],
                "xgboost":          self.xgboost._version_hash,
            },
            scoring_latency_us=scoring_latency_us,
        )

        if should_failover:
            logger.warning(
                "ML pipeline: failover triggered",
                extra={
                    "region":         metric.region,
                    "if_score":       round(if_score, 3),
                    "severity":       severity.name,
                    "fault_type":     fault_type.name,
                    "confidence":     round(failover_conf, 3),
                    "latency_us":     round(scoring_latency_us, 1),
                }
            )

        return result

    def update_bandit(self, region_id: str, old_rtt_us: float, new_rtt_us: float) -> None:
        """External hook for the control plane to push bandit observations."""
        self.bandit.observe(region_id, old_rtt_us, new_rtt_us)

    def reset_flow(self, flow_key: str) -> None:
        """Clear LSTM window for a flow after fault resolution."""
        if flow_key in self._flow_lstms:
            self._flow_lstms[flow_key].reset()

    # ─── Ensemble Logic ───────────────────────────────────────────────────────

    def _ensemble_decision(
        self,
        if_score:      float,
        severity:      FaultSeverity,
        xgb_conf:      float,
        lstm_conf:     float,
        region_weight: float,
    ) -> tuple[bool, float]:
        """
        Combine model outputs into a single failover decision.

        Three independent paths to failover:
            Path A (IF alone):       if_score > 0.70
            Path B (XGB + LSTM):     severity >= DEGRADED AND lstm_conf > 0.60
            Path C (Bandit + IF):    region_weight < 0.20 AND if_score > 0.50

        Confidence is the weighted average across triggered paths.
        """
        triggered_signals = []

        # Path A: Isolation Forest alone
        if if_score >= self.IF_FAILOVER_THRESHOLD:
            triggered_signals.append(("isolation_forest", if_score, 0.40))

        # Path B: XGBoost severity + LSTM confirmation
        if (severity >= self.XGB_DEGRADED_THRESHOLD and
                lstm_conf >= self.LSTM_CONFIDENCE_MIN):
            score_b = (xgb_conf + lstm_conf) / 2.0
            triggered_signals.append(("xgb_lstm", score_b, 0.40))

        # Path C: Bandit signals region degradation + IF corroborates
        if (region_weight < self.BANDIT_LOW_WEIGHT and
                if_score >= self.IF_CORROBORATE_THRESHOLD):
            score_c = (1.0 - region_weight / self.BANDIT_LOW_WEIGHT) * if_score
            triggered_signals.append(("bandit_if", score_c, 0.20))

        if not triggered_signals:
            return False, 0.0

        # Weighted confidence across triggered paths
        total_weight  = sum(w for _, _, w in triggered_signals)
        failover_conf = sum(score * weight for _, score, weight in triggered_signals) / total_weight

        return True, float(failover_conf)

    # ─── LSTM Instance Management ─────────────────────────────────────────────

    def _get_flow_lstm(self, flow_key: str) -> NimbusLSTM:
        """Return (or create) the per-flow LSTM instance."""
        if flow_key not in self._flow_lstms:
            # Clone the template — same weights, fresh sliding window
            lstm = NimbusLSTM(
                seq_len=self.lstm_template.seq_len,
                hidden_size=self.lstm_template.hidden_size,
                n_classes=self.lstm_template.n_classes,
            )
            lstm._lstm1   = self.lstm_template._lstm1
            lstm._lstm2   = self.lstm_template._lstm2
            lstm._W_out   = self.lstm_template._W_out
            lstm._b_out   = self.lstm_template._b_out

            # Evict oldest if at capacity (prevent unbounded growth)
            MAX_FLOW_LSTMS = 10_000
            if len(self._flow_lstms) >= MAX_FLOW_LSTMS:
                oldest_key = next(iter(self._flow_lstms))
                del self._flow_lstms[oldest_key]

            self._flow_lstms[flow_key] = lstm

        return self._flow_lstms[flow_key]
