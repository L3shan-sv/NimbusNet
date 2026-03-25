#!/usr/bin/env python3
"""
bootstrap.py — First-run model initialisation.

Generates synthetic training data, trains all models, saves weights to disk.
Run this once before starting the ML serving layer for the first time.

Usage:
    python -m ml.pipeline.bootstrap --model-dir /opt/nimbusnet/models --samples 500
    python -m ml.pipeline.bootstrap --model-dir ./models --samples 1000 --regions us-east-1,us-west-2,eu-west-1
"""

import argparse
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("bootstrap")


def main():
    parser = argparse.ArgumentParser(description="NimbusNet ML Bootstrap")
    parser.add_argument("--model-dir",  default="./models", help="Model output directory")
    parser.add_argument("--data-dir",   default="./data",   help="Flywheel data directory")
    parser.add_argument("--samples",    type=int, default=500, help="Samples per fault class")
    parser.add_argument("--regions",    default="us-east-1,us-west-2,eu-west-1")
    parser.add_argument("--seed",       type=int, default=42)
    parser.add_argument("--force",      action="store_true", help="Overwrite existing models")
    args = parser.parse_args()

    model_dir = Path(args.model_dir)
    data_dir  = Path(args.data_dir)
    regions   = [r.strip() for r in args.regions.split(",")]

    # Check if models already exist
    if_path  = model_dir / "isolation_forest.pkl"
    xgb_path = model_dir / "xgboost.pkl"

    if if_path.exists() and xgb_path.exists() and not args.force:
        logger.info("Models already exist. Use --force to overwrite.")
        return 0

    logger.info("=" * 60)
    logger.info("NimbusNet ML Bootstrap")
    logger.info(f"  Model dir:  {model_dir}")
    logger.info(f"  Data dir:   {data_dir}")
    logger.info(f"  Samples:    {args.samples} per class × {len(FAULT_PROFILES)} classes × {len(regions)} regions")
    logger.info(f"  Seed:       {args.seed}")
    logger.info("=" * 60)

    # Late imports to keep startup fast
    from ml.flywheel.bootstrap_generator import BootstrapGenerator, FAULT_PROFILES
    from ml.flywheel.flywheel import DataFlywheel
    from ml.models.isolation_forest import NimbusIsolationForest
    from ml.models.xgboost_classifier import NimbusXGBoostClassifier
    from ml.models.bandit import NimbusMultiArmedBandit
    from ml.models.lstm import NimbusLSTM

    # ── 1. Generate synthetic training data ───────────────────────────────────
    logger.info("Generating synthetic training data...")
    t0 = time.perf_counter()
    gen = BootstrapGenerator(seed=args.seed)
    flywheel = DataFlywheel(data_dir)

    for region in regions:
        dataset = gen.generate_balanced_dataset(
            samples_per_class=args.samples,
            region=region,
        )
        for metric, fault_type, severity in dataset:
            from ml.models.types import FaultSeverity
            if severity == FaultSeverity.NOMINAL:
                flywheel.ingest_normal_baseline([metric], session_id=f"bootstrap_{region}")
            else:
                flywheel.ingest_synthetic(
                    metric=metric,
                    fault_type=fault_type,
                    severity=severity,
                    session_id=f"bootstrap_{region}",
                )

    elapsed = time.perf_counter() - t0
    logger.info(f"Generated {flywheel.sample_count()} samples in {elapsed:.1f}s")

    # ── 2. Load training arrays ───────────────────────────────────────────────
    logger.info("Loading training arrays...")
    features, fault_labels, severity_labels = flywheel.load_training_arrays()
    normal_features, anomaly_features = flywheel.load_anomaly_split()
    logger.info(f"  Total:   {len(features)} samples")
    logger.info(f"  Normal:  {len(normal_features)}")
    logger.info(f"  Anomaly: {len(anomaly_features)}")

    # ── 3. Train Isolation Forest ─────────────────────────────────────────────
    logger.info("Training Isolation Forest...")
    t0 = time.perf_counter()
    iso_forest = NimbusIsolationForest(n_estimators=200, max_samples=512, contamination=0.05)
    iso_forest.retrain(normal_features, anomaly_features)
    iso_forest.save(model_dir / "isolation_forest.pkl")
    logger.info(f"  Done in {time.perf_counter() - t0:.1f}s → {model_dir}/isolation_forest.pkl")

    # ── 4. Train XGBoost ─────────────────────────────────────────────────────
    logger.info("Training XGBoost severity classifier...")
    t0 = time.perf_counter()
    xgboost = NimbusXGBoostClassifier(n_estimators=300, max_depth=6)
    xgboost.bootstrap(features, severity_labels)
    xgboost.save(model_dir / "xgboost.pkl")
    logger.info(f"  Done in {time.perf_counter() - t0:.1f}s → {model_dir}/xgboost.pkl")

    # Feature importances
    importances = xgboost.explain(type("FV", (), {"values": features[0]})())
    logger.info("  Top 5 features by importance:")
    for name, imp in list(importances.items())[:5]:
        logger.info(f"    {name}: {imp:.4f}")

    # ── 5. Initialise LSTM with random weights ────────────────────────────────
    logger.info("Initialising LSTM with random weights (train with lstm_trainer.py)...")
    lstm = NimbusLSTM.from_random_weights(seq_len=20, hidden_size=64)
    lstm.save(model_dir / "lstm.pkl")
    logger.info(f"  Saved → {model_dir}/lstm.pkl")

    # ── 6. Initialise Bandit ─────────────────────────────────────────────────
    logger.info("Initialising multi-armed bandit...")
    bandit = NimbusMultiArmedBandit()
    for region in regions:
        bandit.add_region(region)
    bandit.save(model_dir / "bandit.pkl")
    logger.info(f"  Regions: {regions} → {model_dir}/bandit.pkl")

    # ── Summary ───────────────────────────────────────────────────────────────
    logger.info("")
    logger.info("=" * 60)
    logger.info("Bootstrap complete. Artefacts:")
    for path in sorted(model_dir.glob("*.pkl")):
        size_kb = path.stat().st_size / 1024
        logger.info(f"  {path.name}: {size_kb:.1f} KB")
    logger.info("")
    logger.info("Next steps:")
    logger.info("  1. Train LSTM properly: python -m ml.pipeline.lstm_trainer")
    logger.info("  2. Start serving:       python -m ml.serving.main")
    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
