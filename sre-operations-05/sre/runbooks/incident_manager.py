"""
incident_manager.py — Incident lifecycle management.

Creates an Incident when the FSM transitions HEALTHY → DEGRADED.
Updates it as the runbook executes.
Closes it when the FSM returns to HEALTHY.
Hands it off to the postmortem generator on close.

The incident manager is the connective tissue between:
  - Phase 4 FSM (state transitions → incident lifecycle events)
  - Phase 3 ML scoring (scored metrics → incident context)
  - Phase 5 runbook executor (execution results → incident updates)
  - Phase 5 postmortem generator (closed incidents → postmortems)
  - Phase 5 notification system (state changes → SRE notifications)
"""

from __future__ import annotations

import json
import logging
import time
import threading
from pathlib import Path
from typing import Optional, Callable

from .types import (
    Incident, IncidentSeverity, IncidentStatus,
    RunbookResult, RunbookStatus,
)

logger = logging.getLogger(__name__)


def _new_incident_id(region: str) -> str:
    """Generate a human-readable incident ID."""
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    short_region = region.replace("-", "")[:6]
    return f"INC-{ts}-{short_region}"


def _severity_from_xgb(xgb_severity: str, fault_type: str) -> IncidentSeverity:
    """Map XGBoost severity + fault type to incident severity."""
    if xgb_severity == "CRITICAL" or fault_type in ("REGION_PARTITION", "CASCADING"):
        return IncidentSeverity.P0
    if xgb_severity == "DEGRADED":
        return IncidentSeverity.P1
    if xgb_severity == "WARNING":
        return IncidentSeverity.P2
    return IncidentSeverity.P3


class IncidentManager:
    """
    Creates and manages incident records throughout the fault lifecycle.

    Thread-safe. One active incident per region at a time.
    """

    def __init__(
        self,
        store_dir: str | Path,
        on_incident_opened:  Optional[Callable[[Incident], None]] = None,
        on_incident_resolved: Optional[Callable[[Incident], None]] = None,
        on_incident_escalated: Optional[Callable[[Incident], None]] = None,
    ):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)

        # Callbacks wired by the SRE server
        self.on_incident_opened   = on_incident_opened
        self.on_incident_resolved = on_incident_resolved
        self.on_incident_escalated = on_incident_escalated

        self._lock = threading.RLock()
        # One active incident per region
        self._active: dict[str, Incident] = {}
        # All incidents (in-memory for current session; persisted to disk)
        self._all: dict[str, Incident] = {}

        self._load_active()

    # ─── Lifecycle ─────────────────────────────────────────────────────────────

    def open(
        self,
        region: str,
        detected_at_ns: int,
        fault_type: str,
        xgb_severity: str,
        if_score: float,
        lstm_confidence: float,
        rtt_ewma_us: int,
        retransmit_rate: float,
    ) -> Incident:
        """
        Open a new incident for a region.
        Called when the Phase 4 FSM transitions HEALTHY → DEGRADED.
        """
        with self._lock:
            # If there's already an active incident for this region, update it
            if region in self._active:
                existing = self._active[region]
                if existing.status in (IncidentStatus.OPEN,):
                    logger.info(
                        "IncidentManager: region already has active incident",
                        extra={"region": region, "incident_id": existing.id}
                    )
                    return existing

            severity = _severity_from_xgb(xgb_severity, fault_type)

            incident = Incident(
                id=_new_incident_id(region),
                region=region,
                severity=severity,
                status=IncidentStatus.OPEN,
                detected_at_ns=detected_at_ns,
                degraded_at_ns=time.time_ns(),
                fault_type=fault_type,
                if_score=if_score,
                xgb_severity=xgb_severity,
                lstm_confidence=lstm_confidence,
                rtt_ewma_us=rtt_ewma_us,
                retransmit_rate=retransmit_rate,
            )

            self._active[region] = incident
            self._all[incident.id] = incident
            self._persist(incident)

            logger.warning(
                "Incident opened",
                extra={
                    "incident_id": incident.id,
                    "region": region,
                    "severity": severity.value,
                    "fault_type": fault_type,
                }
            )

            if self.on_incident_opened:
                try:
                    self.on_incident_opened(incident)
                except Exception as e:
                    logger.error("on_incident_opened callback failed", exc_info=e)

            return incident

    def record_runbook_started(self, region: str, runbook_id: str, steps_total: int) -> None:
        """Called when the runbook executor begins a runbook."""
        with self._lock:
            inc = self._active.get(region)
            if inc:
                inc.runbook_id = runbook_id
                inc.runbook_steps_total = steps_total
                self._persist(inc)

    def record_runbook_progress(self, region: str, steps_completed: int) -> None:
        """Called as each runbook step completes."""
        with self._lock:
            inc = self._active.get(region)
            if inc:
                inc.runbook_steps_completed = steps_completed
                self._persist(inc)

    def record_runbook_result(self, region: str, result: RunbookResult) -> None:
        """Called when a runbook finishes (success or failure)."""
        with self._lock:
            inc = self._active.get(region)
            if not inc:
                return

            if result.status == RunbookStatus.SUCCESS:
                inc.runbook_result = "SUCCESS"
                inc.status = IncidentStatus.MITIGATED
            elif result.status == RunbookStatus.TIMEOUT:
                inc.runbook_result = "TIMEOUT"
            else:
                inc.runbook_result = "FAILURE"

            inc.runbook_steps_completed = result.steps_completed
            self._persist(inc)

    def record_escalation(self, region: str) -> None:
        """Called when the incident is escalated to an SRE."""
        with self._lock:
            inc = self._active.get(region)
            if inc:
                inc.escalated_to_sre  = True
                inc.escalated_at_ns   = time.time_ns()
                self._persist(inc)

                if self.on_incident_escalated:
                    try:
                        self.on_incident_escalated(inc)
                    except Exception as e:
                        logger.error("on_incident_escalated callback failed", exc_info=e)

    def record_sre_ack(self, region: str) -> None:
        """Called when SRE acknowledges the PagerDuty alert."""
        with self._lock:
            inc = self._active.get(region)
            if inc:
                inc.sre_acknowledged = True
                inc.sre_ack_at_ns    = time.time_ns()
                self._persist(inc)

    def resolve(self, region: str) -> Optional[Incident]:
        """
        Close an incident when the FSM returns to HEALTHY.
        Returns the closed incident for postmortem generation.
        """
        with self._lock:
            inc = self._active.pop(region, None)
            if not inc:
                logger.debug("IncidentManager: no active incident to resolve", extra={"region": region})
                return None

            inc.resolved_at_ns = time.time_ns()
            inc.status         = IncidentStatus.RESOLVED

            # Calculate error budget impact
            if inc.ttr_s:
                # 99.9% SLO = 43.8 min/month budget
                # Each second of downtime = 1/2628000 of monthly budget
                inc.error_budget_consumed_minutes = inc.ttr_s / 60.0

            self._persist(inc)

            logger.info(
                "Incident resolved",
                extra={
                    "incident_id":     inc.id,
                    "region":          region,
                    "duration_s":      round(inc.duration_s, 1),
                    "auto_resolved":   inc.was_auto_resolved,
                    "budget_consumed": round(inc.error_budget_consumed_minutes, 2),
                }
            )

            if self.on_incident_resolved:
                try:
                    self.on_incident_resolved(inc)
                except Exception as e:
                    logger.error("on_incident_resolved callback failed", exc_info=e)

            return inc

    # ─── Queries ───────────────────────────────────────────────────────────────

    def get_active(self, region: str) -> Optional[Incident]:
        with self._lock:
            return self._active.get(region)

    def get_by_id(self, incident_id: str) -> Optional[Incident]:
        with self._lock:
            return self._all.get(incident_id)

    def all_active(self) -> list[Incident]:
        with self._lock:
            return list(self._active.values())

    def recent(self, limit: int = 20) -> list[Incident]:
        with self._lock:
            incidents = sorted(
                self._all.values(),
                key=lambda i: i.detected_at_ns,
                reverse=True,
            )
            return incidents[:limit]

    # ─── Persistence ───────────────────────────────────────────────────────────

    def _persist(self, incident: Incident) -> None:
        path = self.store_dir / f"{incident.id}.json"
        try:
            data = {
                "id":              incident.id,
                "region":          incident.region,
                "severity":        incident.severity.value,
                "status":          incident.status.value,
                "detected_at_ns":  incident.detected_at_ns,
                "degraded_at_ns":  incident.degraded_at_ns,
                "healing_at_ns":   incident.healing_at_ns,
                "resolved_at_ns":  incident.resolved_at_ns,
                "closed_at_ns":    incident.closed_at_ns,
                "fault_type":      incident.fault_type,
                "if_score":        incident.if_score,
                "xgb_severity":    incident.xgb_severity,
                "lstm_confidence": incident.lstm_confidence,
                "rtt_ewma_us":     incident.rtt_ewma_us,
                "retransmit_rate": incident.retransmit_rate,
                "runbook_id":      incident.runbook_id,
                "runbook_result":  incident.runbook_result,
                "runbook_steps_completed": incident.runbook_steps_completed,
                "runbook_steps_total":     incident.runbook_steps_total,
                "escalated_to_sre": incident.escalated_to_sre,
                "escalated_at_ns":  incident.escalated_at_ns,
                "sre_acknowledged": incident.sre_acknowledged,
                "error_budget_consumed_minutes": incident.error_budget_consumed_minutes,
            }
            with open(path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            logger.error("IncidentManager: persist failed", exc_info=e)

    def _load_active(self) -> None:
        """Reload active incidents from disk on startup (crash recovery)."""
        for path in self.store_dir.glob("INC-*.json"):
            try:
                with open(path) as f:
                    data = json.load(f)
                if data.get("status") in ("open", "mitigated"):
                    # Reconstruct a minimal Incident object
                    inc = Incident(
                        id=data["id"],
                        region=data["region"],
                        severity=IncidentSeverity(data["severity"]),
                        status=IncidentStatus(data["status"]),
                        detected_at_ns=data["detected_at_ns"],
                        degraded_at_ns=data["degraded_at_ns"],
                        fault_type=data.get("fault_type", "UNKNOWN"),
                        if_score=data.get("if_score", 0.0),
                        xgb_severity=data.get("xgb_severity", "NOMINAL"),
                        rtt_ewma_us=data.get("rtt_ewma_us", 0),
                    )
                    self._active[inc.region] = inc
                    self._all[inc.id] = inc
                    logger.info(
                        "IncidentManager: reloaded active incident from disk",
                        extra={"incident_id": inc.id, "region": inc.region}
                    )
            except Exception as e:
                logger.warning(f"Failed to load incident from {path}", exc_info=e)
