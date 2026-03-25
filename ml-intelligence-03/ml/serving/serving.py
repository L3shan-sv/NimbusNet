"""
serving.py — ML scoring HTTP server.

Receives AggregatedMetric JSON from the Go agent (Phase 2),
runs the full scoring pipeline, and returns MLScoredMetric JSON
to the control plane (Phase 4).

In production this would be gRPC with protobuf for performance.
HTTP/JSON is used here for debuggability and Phase 4 integration simplicity.

Endpoints:
    POST /score          — score one AggregatedMetric
    POST /score/batch    — score a batch (up to 1000 metrics)
    GET  /models/health  — model health check + version info
    GET  /models/stats   — flywheel stats + bandit arm state
    POST /flywheel/ingest — ingest a labeled training sample
    POST /models/retrain  — trigger manual retrain (protected)
    GET  /metrics         — Prometheus metrics
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Optional
import json

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

from ..models.types import (
    AggregatedMetric, MLScoredMetric, FaultType, FaultSeverity
)
from ..pipeline.scoring_pipeline import ScoringPipeline
from ..flywheel.flywheel import DataFlywheel, LabelSource
from ..flywheel.retrainer import RetrainingOrchestrator

logger = logging.getLogger(__name__)


# ─── Request / Response Models ─────────────────────────────────────────────────

class MetricRequest(BaseModel):
    window_start_ns: int
    window_end_ns:   int
    window_size_ms:  float = 50.0
    src_ip:          str   = ""
    dst_ip:          str   = ""
    region:          str   = "unknown"
    total_bytes:     int   = 0
    total_packets:   int   = 0
    retransmit_rate: float = 0.0
    rtt_ewma_us:     int   = 0
    rtt_jitter_us:   int   = 0
    max_anomaly_score: int   = 0
    avg_anomaly_score: float = 0.0
    connects_observed: int   = 0
    closes_observed:   int   = 0
    retrans_observed:  int   = 0
    owner_node_id:     str   = ""
    is_local:          bool  = False


class ScoredMetricResponse(BaseModel):
    isolation_forest_score:  float
    lstm_fault_type:         str
    lstm_confidence:         float
    xgboost_severity:        str
    xgboost_confidence:      float
    bandit_routing_weight:   float
    should_trigger_failover: bool
    failover_confidence:     float
    scoring_latency_us:      float
    model_versions:          dict


class FlyWheelIngestRequest(BaseModel):
    metric:        MetricRequest
    fault_type:    int = Field(description="FaultType int value")
    severity:      int = Field(description="FaultSeverity int value")
    session_id:    str
    label_source:  str = "synthetic_qdisc"
    confidence:    float = 0.95


class RetrainRequest(BaseModel):
    trigger: str = "manual"
    token:   str = ""  # simple auth token — replace with proper auth in production


# ─── App Factory ──────────────────────────────────────────────────────────────

def create_app(
    pipeline:   ScoringPipeline,
    flywheel:   DataFlywheel,
    retrainer:  RetrainingOrchestrator,
    admin_token: str = "",
) -> FastAPI:
    """Create the FastAPI application with all routes configured."""

    app = FastAPI(
        title="NimbusNet ML Scoring Service",
        description="Phase 3 — ML Intelligence Layer",
        version="0.3.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── /score ─────────────────────────────────────────────────────────────────

    @app.post("/score", response_model=ScoredMetricResponse)
    async def score_metric(req: MetricRequest):
        """Score a single AggregatedMetric through the full ML pipeline."""
        metric = _metric_from_request(req)
        try:
            result = pipeline.score(metric)
            return _response_from_scored(result)
        except Exception as e:
            logger.exception("Scoring failed")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/score/batch")
    async def score_batch(requests: list[MetricRequest]):
        """Score a batch of up to 1000 metrics."""
        if len(requests) > 1000:
            raise HTTPException(status_code=400, detail="Batch size > 1000 not supported")

        results = []
        for req in requests:
            metric = _metric_from_request(req)
            result = pipeline.score(metric)
            results.append(_response_from_scored(result))

        return {"results": results, "count": len(results)}

    # ── /models ────────────────────────────────────────────────────────────────

    @app.get("/models/health")
    async def models_health():
        """Health check — returns 200 if all models are loaded."""
        issues = []
        if not pipeline.isolation_forest.is_trained:
            issues.append("isolation_forest: not trained")
        if not pipeline.xgboost.is_trained:
            issues.append("xgboost: not trained")
        if not pipeline.lstm_template.is_loaded:
            issues.append("lstm: weights not loaded")

        status = "ok" if not issues else "degraded"
        code   = 200 if not issues else 503

        return {
            "status": status,
            "issues": issues,
            "models": {
                "isolation_forest": pipeline.isolation_forest.metadata,
                "lstm":             pipeline.lstm_template.metadata,
                "xgboost":          pipeline.xgboost.metadata,
                "bandit":           pipeline.bandit.metadata,
            }
        }

    @app.get("/models/stats")
    async def models_stats():
        """Flywheel stats + bandit arm state."""
        stats = flywheel.stats()
        return {
            "flywheel": {
                "total_samples":       stats.total_samples,
                "samples_by_source":   stats.samples_by_source,
                "samples_by_fault":    stats.samples_by_fault,
                "last_retrain_at":     stats.last_retrain_at,
                "retrains_completed":  stats.retrains_completed,
            },
            "bandit": pipeline.bandit.metadata,
            "retrain_history": [
                {
                    "triggered_by":   r.triggered_by,
                    "success":        r.success,
                    "models_swapped": r.models_swapped,
                    "duration_s":     round(r.duration_s, 1),
                    "xgb_before":     round(r.xgb_acc_before, 4),
                    "xgb_after":      round(r.xgb_acc_after, 4),
                }
                for r in retrainer.history[-10:]  # last 10 retrains
            ]
        }

    # ── /flywheel ─────────────────────────────────────────────────────────────

    @app.post("/flywheel/ingest")
    async def ingest_sample(req: FlyWheelIngestRequest):
        """Ingest a labeled training sample into the flywheel store."""
        metric = _metric_from_request(req.metric)

        if req.label_source == LabelSource.SYNTHETIC_QDISC.value:
            flywheel.ingest_synthetic(
                metric=metric,
                fault_type=FaultType(req.fault_type),
                severity=FaultSeverity(req.severity),
                session_id=req.session_id,
                confidence=req.confidence,
            )
        else:
            flywheel.ingest_production_outcome(
                metric=metric,
                actual_fault_type=FaultType(req.fault_type),
                actual_severity=FaultSeverity(req.severity),
                incident_id=req.session_id,
                confidence=req.confidence,
            )

        should = flywheel.should_retrain()
        return {
            "ingested": True,
            "total_samples": flywheel.sample_count(),
            "retrain_recommended": should,
        }

    # ── /models/retrain ───────────────────────────────────────────────────────

    @app.post("/models/retrain")
    async def trigger_retrain(req: RetrainRequest, background_tasks: BackgroundTasks):
        """Trigger a model retrain in the background."""
        if admin_token and req.token != admin_token:
            raise HTTPException(status_code=403, detail="Invalid admin token")

        background_tasks.add_task(retrainer.retrain, req.trigger)
        return {"status": "retrain_queued", "trigger": req.trigger}

    # ── /metrics ──────────────────────────────────────────────────────────────

    @app.get("/metrics")
    async def prometheus_metrics():
        """Prometheus-compatible metrics endpoint."""
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        from fastapi.responses import Response
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _metric_from_request(req: MetricRequest) -> AggregatedMetric:
    return AggregatedMetric(
        window_start_ns=req.window_start_ns,
        window_end_ns=req.window_end_ns,
        window_size_ms=req.window_size_ms,
        src_ip=req.src_ip,
        dst_ip=req.dst_ip,
        region=req.region,
        total_bytes=req.total_bytes,
        total_packets=req.total_packets,
        retransmit_rate=req.retransmit_rate,
        rtt_ewma_us=req.rtt_ewma_us,
        rtt_jitter_us=req.rtt_jitter_us,
        max_anomaly_score=req.max_anomaly_score,
        avg_anomaly_score=req.avg_anomaly_score,
        connects_observed=req.connects_observed,
        closes_observed=req.closes_observed,
        retrans_observed=req.retrans_observed,
        owner_node_id=req.owner_node_id,
        is_local=req.is_local,
    )


def _response_from_scored(result: MLScoredMetric) -> ScoredMetricResponse:
    return ScoredMetricResponse(
        isolation_forest_score=round(result.isolation_forest_score, 4),
        lstm_fault_type=result.lstm_fault_type.name,
        lstm_confidence=round(result.lstm_confidence, 4),
        xgboost_severity=result.xgboost_severity.name,
        xgboost_confidence=round(result.xgboost_confidence, 4),
        bandit_routing_weight=round(result.bandit_routing_weight, 4),
        should_trigger_failover=result.should_trigger_failover,
        failover_confidence=round(result.failover_confidence, 4),
        scoring_latency_us=round(result.scoring_latency_us, 1),
        model_versions=result.model_versions,
    )


# ─── Entrypoint ───────────────────────────────────────────────────────────────

def serve(
    pipeline:    ScoringPipeline,
    flywheel:    DataFlywheel,
    retrainer:   RetrainingOrchestrator,
    host:        str  = "0.0.0.0",
    port:        int  = 8001,
    admin_token: str  = "",
) -> None:
    app = create_app(pipeline, flywheel, retrainer, admin_token)
    uvicorn.run(app, host=host, port=port, log_level="info")
