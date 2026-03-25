"""
server.py — SRE Operations FastAPI server.

This is the coordination hub for Phase 5. It:
  - Receives Phase 4 FSM state change events
  - Triggers runbook execution
  - Manages incident lifecycle
  - Generates postmortems
  - Routes notifications (Slack FYI vs PagerDuty)
  - Exposes a read API for dashboards and postmortem review

Endpoints:
  POST /fsm/degraded          — Phase 4 notifies: region entered DEGRADED
  POST /fsm/failed            — Phase 4 notifies: region entered FAILED
  POST /fsm/healthy           — Phase 4 notifies: region returned to HEALTHY
  POST /fsm/runbook-succeeded — Runbook executor notifies: runbook succeeded
  POST /fsm/runbook-failed    — Runbook executor notifies: runbook failed

  GET  /incidents             — list all incidents
  GET  /incidents/{id}        — get one incident
  POST /incidents/{id}/ack    — SRE acknowledges an escalation
  POST /incidents/{id}/resolve — SRE manually resolves an incident

  GET  /postmortems           — list postmortems
  GET  /postmortems/{id}      — get one postmortem
  PUT  /postmortems/{id}      — SRE updates contributing_factors/action_items
  POST /postmortems/{id}/publish — SRE signs off on postmortem

  GET  /runbooks              — list available runbooks
  GET  /health                — service health check
  GET  /metrics               — Prometheus metrics
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ..types import IncidentSeverity
from ..runbooks.incident_manager import IncidentManager
from ..runbooks.executor import RunbookExecutor
from ..runbooks.library import RUNBOOK_REGISTRY
from ..postmortem.generator import PostmortemGenerator
from ..notifications.notifier import Notifier, NotificationConfig
from .phase4_client import Phase4Client

logger = logging.getLogger(__name__)


# ─── Request Models ────────────────────────────────────────────────────────────

class FSMDegradedEvent(BaseModel):
    region:          str
    detected_at_ns:  int
    fault_type:      str = "UNKNOWN"
    xgb_severity:    str = "DEGRADED"
    if_score:        float = 0.0
    lstm_confidence: float = 0.0
    rtt_ewma_us:     int = 0
    retransmit_rate: float = 0.0

class FSMFailedEvent(BaseModel):
    region:      str
    incident_id: Optional[str] = None
    reason:      str = "Healing TTL exhausted"

class FSMHealthyEvent(BaseModel):
    region:      str
    incident_id: Optional[str] = None

class SREAckRequest(BaseModel):
    sre_name: str = ""
    note:     str = ""

class PostmortemUpdateRequest(BaseModel):
    contributing_factors: str = ""
    action_items: list[dict] = []
    sre_author:  str = ""


# ─── App Factory ──────────────────────────────────────────────────────────────

def create_app(
    incident_manager:    IncidentManager,
    executor:            RunbookExecutor,
    postmortem_gen:      PostmortemGenerator,
    notifier:            Notifier,
    phase4_client:       Phase4Client,
) -> FastAPI:

    app = FastAPI(
        title="NimbusNet SRE Operations Service",
        description="Phase 5 — Runbook Executor, Postmortem Generator, Escalation Pipeline",
        version="0.5.0",
    )
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    # ── FSM Event Endpoints ───────────────────────────────────────────────────
    # These are called by Phase 4 when the healing state machine transitions.

    @app.post("/fsm/degraded")
    async def fsm_degraded(event: FSMDegradedEvent):
        """
        Phase 4 FSM entered DEGRADED.
        Open incident + trigger runbook executor + notify Slack (not PagerDuty yet).
        """
        incident = incident_manager.open(
            region=event.region,
            detected_at_ns=event.detected_at_ns,
            fault_type=event.fault_type,
            xgb_severity=event.xgb_severity,
            if_score=event.if_score,
            lstm_confidence=event.lstm_confidence,
            rtt_ewma_us=event.rtt_ewma_us,
            retransmit_rate=event.retransmit_rate,
        )

        # Notify Slack: incident opened (not a page)
        notifier.on_incident_opened(incident)

        # Start runbook executor in background
        executor.execute_for_incident(incident)

        return {
            "status":      "ok",
            "incident_id": incident.id,
            "runbook":     "started",
            "severity":    incident.severity.value,
        }

    @app.post("/fsm/failed")
    async def fsm_failed(event: FSMFailedEvent):
        """
        Phase 4 FSM entered FAILED (healing TTL exhausted).
        Escalate to SRE via PagerDuty.
        """
        incident = incident_manager.get_active(event.region)
        if not incident:
            # Create a minimal incident record if one doesn't exist
            incident = incident_manager.open(
                region=event.region,
                detected_at_ns=time.time_ns(),
                fault_type="UNKNOWN",
                xgb_severity="CRITICAL",
                if_score=0.9,
                lstm_confidence=0.0,
                rtt_ewma_us=0,
                retransmit_rate=0.0,
            )

        incident_manager.record_escalation(event.region)
        notifier.on_escalation(incident, event.reason)

        logger.error(
            "SRE Operations: region FAILED — SRE paged",
            extra={"region": event.region, "incident_id": incident.id}
        )

        return {"status": "ok", "incident_id": incident.id, "pagerduty": "fired"}

    @app.post("/fsm/healthy")
    async def fsm_healthy(event: FSMHealthyEvent):
        """
        Phase 4 FSM returned to HEALTHY.
        Resolve incident + generate postmortem + notify Slack FYI.
        """
        incident = incident_manager.resolve(event.region)
        if not incident:
            return {"status": "ok", "message": "no active incident"}

        # Generate postmortem — always fires, regardless of how incident resolved
        pm = postmortem_gen.generate(incident)

        # Notify appropriately
        if incident.was_auto_resolved:
            notifier.on_incident_auto_resolved(incident, pm)
        # Escalated incidents already paged — just update Slack
        elif incident.escalated_to_sre:
            # Slack FYI that it's resolved now
            notifier.on_incident_auto_resolved(incident, pm)

        # Postmortem review notification for P0/P1
        if pm.requires_human_review:
            notifier.on_postmortem_ready(pm)

        # Trigger ML retrain if postmortem flagged it
        if pm.ml_retrain_triggered:
            try:
                await _trigger_ml_retrain(incident.fault_type)
            except Exception as e:
                logger.error("ML retrain trigger failed", exc_info=e)

        return {
            "status":       "ok",
            "incident_id":  incident.id,
            "postmortem_id": pm.id,
            "auto_resolved": incident.was_auto_resolved,
            "pm_requires_review": pm.requires_human_review,
        }

    @app.post("/fsm/runbook-succeeded")
    async def runbook_succeeded(region: str):
        """Called by the runbook executor when a runbook verifies healing."""
        try:
            phase4_client.notify_runbook_succeeded(region)
        except Exception as e:
            logger.error("Failed to notify Phase 4 runbook success", exc_info=e)
        return {"status": "ok", "region": region}

    @app.post("/fsm/runbook-failed")
    async def runbook_failed(region: str):
        """Called by the runbook executor when a runbook exhausts retries."""
        try:
            phase4_client.notify_runbook_failed(region)
        except Exception as e:
            logger.error("Failed to notify Phase 4 runbook failure", exc_info=e)

        incident = incident_manager.get_active(region)
        if incident:
            notifier.on_escalation(incident, "Runbook exhausted all retries")

        return {"status": "ok", "region": region}

    # ── Incident Endpoints ────────────────────────────────────────────────────

    @app.get("/incidents")
    async def list_incidents(limit: int = 20):
        return {"incidents": [_incident_to_dict(i) for i in incident_manager.recent(limit)]}

    @app.get("/incidents/active")
    async def list_active():
        return {"incidents": [_incident_to_dict(i) for i in incident_manager.all_active()]}

    @app.get("/incidents/{incident_id}")
    async def get_incident(incident_id: str):
        inc = incident_manager.get_by_id(incident_id)
        if not inc:
            raise HTTPException(status_code=404, detail="Incident not found")
        return _incident_to_dict(inc)

    @app.post("/incidents/{incident_id}/ack")
    async def ack_incident(incident_id: str, req: SREAckRequest):
        inc = incident_manager.get_by_id(incident_id)
        if not inc:
            raise HTTPException(status_code=404, detail="Incident not found")
        incident_manager.record_sre_ack(inc.region)
        return {"status": "acknowledged", "incident_id": incident_id, "sre": req.sre_name}

    @app.post("/incidents/{incident_id}/resolve")
    async def resolve_incident(incident_id: str, req: SREAckRequest):
        inc = incident_manager.get_by_id(incident_id)
        if not inc:
            raise HTTPException(status_code=404, detail="Incident not found")
        # SRE manually resolving — notify Phase 4 to clear FAILED state
        phase4_client.notify_manual_override(inc.region, "HEALTHY")
        return {"status": "resolved", "incident_id": incident_id}

    # ── Postmortem Endpoints ──────────────────────────────────────────────────

    @app.get("/postmortems")
    async def list_postmortems(limit: int = 20):
        return {"postmortems": postmortem_gen.list_recent(limit)}

    @app.get("/postmortems/{pm_id}")
    async def get_postmortem(pm_id: str):
        pm = postmortem_gen.get(pm_id)
        if not pm:
            raise HTTPException(status_code=404, detail="Postmortem not found")
        return pm

    @app.put("/postmortems/{pm_id}")
    async def update_postmortem(pm_id: str, req: PostmortemUpdateRequest):
        pm_data = postmortem_gen.get(pm_id)
        if not pm_data:
            raise HTTPException(status_code=404, detail="Postmortem not found")

        pm_data["contributing_factors"] = req.contributing_factors
        pm_data["action_items"]         = req.action_items
        pm_data["sre_author"]           = req.sre_author

        # Re-save
        import json
        from pathlib import Path
        path = postmortem_gen.store_dir / f"{pm_id}.json"
        with open(path, "w") as f:
            json.dump(pm_data, f, indent=2, default=str)

        return {"status": "updated", "pm_id": pm_id}

    @app.post("/postmortems/{pm_id}/publish")
    async def publish_postmortem(pm_id: str, req: PostmortemUpdateRequest):
        pm_data = postmortem_gen.get(pm_id)
        if not pm_data:
            raise HTTPException(status_code=404, detail="Postmortem not found")

        pm_data["status"]     = "published"
        pm_data["sre_author"] = req.sre_author
        if req.contributing_factors:
            pm_data["contributing_factors"] = req.contributing_factors
        if req.action_items:
            pm_data["action_items"] = req.action_items

        import json
        from pathlib import Path
        path = postmortem_gen.store_dir / f"{pm_id}.json"
        with open(path, "w") as f:
            json.dump(pm_data, f, indent=2, default=str)

        return {"status": "published", "pm_id": pm_id, "author": req.sre_author}

    # ── Runbook Endpoints ─────────────────────────────────────────────────────

    @app.get("/runbooks")
    async def list_runbooks():
        return {
            "runbooks": [
                {
                    "id":           rb.id,
                    "name":         rb.name,
                    "fault_type":   rb.fault_type,
                    "min_severity": rb.min_severity.value,
                    "steps":        len(rb.steps),
                    "ttl_s":        rb.ttl_s,
                }
                for rb in RUNBOOK_REGISTRY.values()
            ]
        }

    # ── Health & Metrics ──────────────────────────────────────────────────────

    @app.get("/health")
    async def health():
        active = incident_manager.all_active()
        return {
            "status":           "ok",
            "active_incidents": len(active),
            "regions_degraded": [i.region for i in active],
        }

    @app.get("/metrics")
    async def metrics():
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        from fastapi.responses import Response
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _incident_to_dict(inc) -> dict:
    return {
        "id":             inc.id,
        "region":         inc.region,
        "severity":       inc.severity.value,
        "status":         inc.status.value,
        "fault_type":     inc.fault_type,
        "duration_s":     round(inc.duration_s, 1),
        "auto_resolved":  inc.was_auto_resolved,
        "escalated":      inc.escalated_to_sre,
        "runbook_id":     inc.runbook_id,
        "runbook_result": inc.runbook_result,
        "budget_consumed_m": round(inc.error_budget_consumed_minutes, 2),
    }


async def _trigger_ml_retrain(fault_type: str) -> None:
    """Trigger the Phase 3 ML retraining service after a production incident."""
    import urllib.request
    payload = f'{{"trigger": "production_incident", "fault_type": "{fault_type}"}}'
    req = urllib.request.Request(
        "http://nimbusnet-ml-scoring:8001/models/retrain",
        data=payload.encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5):
        pass
    logger.info("ML retrain triggered", extra={"fault_type": fault_type})
