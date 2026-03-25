"""
NimbusNet ML Intelligence Layer — Phase 3
==========================================

Four models, one flywheel, zero human labels required at bootstrap.

Architecture:
    AggregatedMetric (from Go agent Bus.Metrics)
        │
        ▼
    FeatureExtractor          → 18-dimensional feature vector
        │
        ├──► IsolationForest  → anomaly_probability (0.0–1.0)
        ├──► LSTM             → fault_type prediction (sequence model)
        ├──► XGBoost          → fault_severity classification
        └──► MultiArmedBandit → routing_weight recommendation (online)
        │
        ▼
    MLScoredMetric            → pushed to control plane (Phase 4)
        │
        ▼
    DataFlywheel              → labels ground truth, retrains models
"""

__version__ = "0.3.0"
