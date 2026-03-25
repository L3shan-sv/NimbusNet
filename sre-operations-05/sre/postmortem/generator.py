"""
generator.py — Automated postmortem generation.

Fires after EVERY incident — auto-resolved or escalated.
Populates the postmortem skeleton from:
  - Incident record (timing, fault type, ML scores)
  - FSM transition history (from Phase 4)
  - Runbook execution result (steps completed, duration)
  - Traffic weight timeline (from Phase 4 metrics)
  - XGBoost SHAP feature importances (root cause signal)

For P0/P1: generates the skeleton, marks for human review.
           SRE fills in contributing_factors and action_items.
For P2/P3: fully auto-generated. No human review required unless
           the SRE explicitly opens it.

The generated postmortem feeds back into four downstream systems:
  1. ML retraining     — production outcome label for flywheel
  2. Runbook updates   — step failure patterns suggest improvements
  3. SLO recalibration — actual TTR vs SLO budget
  4. Chaos experiments — new fault signatures added to chaos suite
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from ..types import (
    Incident, IncidentSeverity, IncidentStatus,
    Postmortem, RunbookResult, RunbookStatus,
)

logger = logging.getLogger(__name__)


class PostmortemGenerator:
    """
    Generates and stores structured postmortems.

    Storage: one JSON file per postmortem in the configured store directory.
    Postmortems are immutable once published (SRE signs off).
    """

    def __init__(
        self,
        store_dir: str | Path,
        ml_scoring_url: str = "http://nimbusnet-ml-scoring:8001",
    ):
        self.store_dir       = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.ml_scoring_url  = ml_scoring_url
        self._counter        = self._load_counter()

    # ─── Primary API ───────────────────────────────────────────────────────────

    def generate(
        self,
        incident:        Incident,
        runbook_result:  Optional[RunbookResult] = None,
        fsm_transitions: Optional[list[dict]] = None,
        weight_timeline: Optional[list[dict]] = None,
        feature_importances: Optional[dict[str, float]] = None,
    ) -> Postmortem:
        """
        Generate a postmortem for a closed incident.

        Called by the SRE server on every incident resolution — auto or escalated.
        All parameters except `incident` are optional enrichment data.
        """
        self._counter += 1
        pm_id = f"PM-{time.strftime('%Y%m%d')}-{self._counter:04d}"

        requires_human = incident.severity in (IncidentSeverity.P0, IncidentSeverity.P1)

        pm = Postmortem(
            id=pm_id,
            incident_id=incident.id,
            region=incident.region,
            severity=incident.severity,
            status="draft",
            requires_human_review=requires_human,
        )

        # ── Auto-populate all sections ────────────────────────────────────────
        pm.summary    = self._generate_summary(incident, runbook_result)
        pm.timeline   = self._generate_timeline(incident, runbook_result, fsm_transitions)
        pm.root_cause = self._generate_root_cause(incident, feature_importances)
        pm.impact     = self._generate_impact(incident)
        pm.detection  = self._generate_detection(incident)
        pm.resolution = self._generate_resolution(incident, runbook_result)

        # ── Downstream actions ────────────────────────────────────────────────
        pm.ml_retrain_triggered   = self._should_trigger_ml_retrain(incident, runbook_result)
        pm.runbook_updated        = self._should_update_runbook(runbook_result)
        pm.slo_recalibrated       = incident.error_budget_consumed_minutes > 5.0
        pm.chaos_experiment_added = self._should_add_chaos_experiment(incident)

        # ── Status ────────────────────────────────────────────────────────────
        if requires_human:
            pm.status = "under_review"
            pm.action_items = self._generate_draft_action_items(incident, runbook_result)
        else:
            pm.status = "published"  # P2/P3 auto-published

        self._save(pm)

        logger.info(
            "Postmortem generated",
            extra={
                "pm_id":              pm.id,
                "incident_id":        incident.id,
                "severity":           incident.severity.value,
                "requires_review":    pm.requires_human_review,
                "ml_retrain":         pm.ml_retrain_triggered,
                "chaos_added":        pm.chaos_experiment_added,
                "status":             pm.status,
            }
        )
        return pm

    # ─── Section Generators ────────────────────────────────────────────────────

    def _generate_summary(
        self,
        incident: Incident,
        result: Optional[RunbookResult],
    ) -> str:
        duration = f"{incident.duration_s:.0f}s" if incident.duration_s < 120 else f"{incident.duration_s/60:.1f}m"
        resolution = (
            "automatically resolved by runbook executor"
            if incident.was_auto_resolved
            else "escalated to on-call SRE for manual resolution"
        )
        budget = f"{incident.error_budget_consumed_minutes:.1f} minutes"

        return (
            f"On {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(incident.detected_at_ns / 1e9))}, "
            f"NimbusNet detected a {incident.fault_type.replace('_', ' ').lower()} event in "
            f"{incident.region} (severity: {incident.severity.value}). "
            f"The incident lasted {duration} and was {resolution}. "
            f"Estimated error budget consumed: {budget}."
        )

    def _generate_timeline(
        self,
        incident: Incident,
        result: Optional[RunbookResult],
        fsm_transitions: Optional[list[dict]],
    ) -> list[dict]:
        timeline = []

        def ts(ns: Optional[int]) -> str:
            if ns is None:
                return "unknown"
            return time.strftime('%H:%M:%S UTC', time.gmtime(ns / 1e9))

        timeline.append({
            "time":  ts(incident.detected_at_ns),
            "event": "ML detection",
            "detail": (
                f"IsolationForest score {incident.if_score:.2f}, "
                f"XGBoost severity {incident.xgb_severity}, "
                f"LSTM confidence {incident.lstm_confidence:.2f}"
            ),
        })

        timeline.append({
            "time":  ts(incident.degraded_at_ns),
            "event": "FSM: HEALTHY → DEGRADED",
            "detail": (
                f"Soft failover initiated. Traffic decay started. "
                f"Route 53 drain initiated."
            ),
        })

        if incident.healing_at_ns:
            timeline.append({
                "time":  ts(incident.healing_at_ns),
                "event": f"Runbook {incident.runbook_id} started",
                "detail": f"Autonomous remediation began.",
            })

        # Append FSM transitions from Phase 4 if provided
        if fsm_transitions:
            for t in fsm_transitions:
                timeline.append({
                    "time":   t.get("timestamp", ""),
                    "event":  f"FSM: {t.get('from')} → {t.get('to')}",
                    "detail": f"Event: {t.get('event')}. Duration in state: {t.get('duration_s', 0):.0f}s",
                })

        if incident.resolved_at_ns:
            timeline.append({
                "time":  ts(incident.resolved_at_ns),
                "event": "Incident resolved",
                "detail": (
                    f"FSM returned to HEALTHY. "
                    f"Total duration: {incident.duration_s:.0f}s. "
                    f"Auto-resolved: {incident.was_auto_resolved}."
                ),
            })

        if incident.escalated_to_sre and incident.escalated_at_ns:
            timeline.append({
                "time":  ts(incident.escalated_at_ns),
                "event": "Escalated to SRE",
                "detail": "PagerDuty alert fired. Runbook exhausted TTL.",
            })

        return sorted(timeline, key=lambda x: x.get("time", ""))

    def _generate_root_cause(
        self,
        incident: Incident,
        feature_importances: Optional[dict[str, float]],
    ) -> str:
        if feature_importances:
            top_features = sorted(
                feature_importances.items(), key=lambda x: x[1], reverse=True
            )[:3]
            feature_str = ", ".join(
                f"{name} ({imp:.1%})" for name, imp in top_features
            )
            return (
                f"XGBoost classifier identified the primary contributing signals as: {feature_str}. "
                f"Fault type classified as {incident.fault_type.replace('_', ' ').lower()}. "
                f"LSTM temporal sequence confidence: {incident.lstm_confidence:.0%}."
            )
        else:
            return (
                f"Fault type: {incident.fault_type}. "
                f"RTT at detection: {incident.rtt_ewma_us / 1000:.0f}ms. "
                f"Retransmit rate: {incident.retransmit_rate:.1%}. "
                f"IsolationForest anomaly probability: {incident.if_score:.0%}. "
                f"Root cause feature importances not available — request ML scoring service logs."
            )

    def _generate_impact(self, incident: Incident) -> str:
        ttr = incident.ttr_s
        ttr_str = f"{ttr:.0f}s" if ttr and ttr < 120 else (f"{ttr/60:.1f}m" if ttr else "ongoing")
        return (
            f"Region {incident.region} was degraded for {ttr_str}. "
            f"Traffic was shed from the region via adaptive decay (soft failover). "
            f"Estimated error budget consumed: {incident.error_budget_consumed_minutes:.1f} minutes "
            f"({incident.error_budget_consumed_minutes / 43.8 * 100:.1f}% of monthly 99.9% budget). "
            f"No full outage — other regions absorbed traffic throughout."
        )

    def _generate_detection(self, incident: Incident) -> str:
        return (
            f"Autonomous detection by NimbusNet ML pipeline. "
            f"IsolationForest anomaly probability: {incident.if_score:.0%}. "
            f"XGBoost severity: {incident.xgb_severity}. "
            f"Time to detect from first signal to FSM DEGRADED: {incident.ttd_s:.1f}s. "
            f"No human involvement in detection."
        )

    def _generate_resolution(
        self,
        incident: Incident,
        result: Optional[RunbookResult],
    ) -> str:
        if incident.was_auto_resolved and result:
            return (
                f"Runbook {incident.runbook_id} executed autonomously. "
                f"{result.steps_completed}/{result.steps_total} steps completed. "
                f"Runbook duration: {result.duration_s:.0f}s. "
                f"Final verification gate passed. "
                f"Phase 4 FSM transitioned to RECOVERED, then HEALTHY after 60s observation window."
            )
        elif incident.escalated_to_sre:
            return (
                f"Runbook {incident.runbook_id} exhausted TTL without resolution. "
                f"{incident.runbook_steps_completed}/{incident.runbook_steps_total} steps completed. "
                f"SRE was paged via PagerDuty. "
                f"Manual resolution details: [FILL IN — SRE author required]."
            )
        else:
            return "Resolution details pending — incident may still be open."

    def _generate_draft_action_items(
        self,
        incident: Incident,
        result: Optional[RunbookResult],
    ) -> list[dict]:
        items = []

        if result and result.status != RunbookStatus.SUCCESS:
            items.append({
                "action": f"Investigate why runbook {incident.runbook_id} failed",
                "owner":  "SRE",
                "due":    "1 week",
                "priority": "high",
            })

        if incident.fault_type == "REGION_PARTITION":
            items.append({
                "action": "Review TGW attachment health monitoring coverage",
                "owner":  "SRE + Networking",
                "due":    "2 weeks",
                "priority": "medium",
            })

        items.append({
            "action": f"Add {incident.fault_type} scenario to quarterly GameDay",
            "owner":  "SRE",
            "due":    "Next GameDay",
            "priority": "low",
        })

        return items

    # ─── Downstream Trigger Logic ──────────────────────────────────────────────

    def _should_trigger_ml_retrain(
        self,
        incident: Incident,
        result: Optional[RunbookResult],
    ) -> bool:
        # Retrain if: incident was P0/P1 OR runbook failed (suggests ML missed something)
        return (
            incident.severity in (IncidentSeverity.P0, IncidentSeverity.P1)
            or (result is not None and result.status != RunbookStatus.SUCCESS)
        )

    def _should_update_runbook(self, result: Optional[RunbookResult]) -> bool:
        if result is None:
            return False
        # Update if any steps failed (even if overall runbook succeeded via retry)
        return any(
            s.status == RunbookStatus.FAILED
            for s in result.step_results
        )

    def _should_add_chaos_experiment(self, incident: Incident) -> bool:
        # Add a chaos experiment if this fault type hasn't been seen in the last 30 days
        # (simplified — in production, check the chaos experiment registry)
        return incident.fault_type in ("CASCADING", "DUAL_REGION_FAILURE", "UNKNOWN")

    # ─── Persistence ───────────────────────────────────────────────────────────

    def _save(self, pm: Postmortem) -> None:
        path = self.store_dir / f"{pm.id}.json"
        data = {
            "id":           pm.id,
            "incident_id":  pm.incident_id,
            "region":       pm.region,
            "severity":     pm.severity.value,
            "status":       pm.status,
            "summary":      pm.summary,
            "timeline":     pm.timeline,
            "root_cause":   pm.root_cause,
            "impact":       pm.impact,
            "detection":    pm.detection,
            "resolution":   pm.resolution,
            "contributing_factors": pm.contributing_factors,
            "action_items": pm.action_items,
            "generated_at": pm.generated_at,
            "requires_human_review": pm.requires_human_review,
            "ml_retrain_triggered":   pm.ml_retrain_triggered,
            "runbook_updated":        pm.runbook_updated,
            "slo_recalibrated":       pm.slo_recalibrated,
            "chaos_experiment_added": pm.chaos_experiment_added,
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def get(self, pm_id: str) -> Optional[dict]:
        path = self.store_dir / f"{pm_id}.json"
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)

    def list_recent(self, limit: int = 20) -> list[dict]:
        paths = sorted(
            self.store_dir.glob("PM-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        result = []
        for p in paths:
            try:
                with open(p) as f:
                    result.append(json.load(f))
            except Exception:
                pass
        return result

    def _load_counter(self) -> int:
        existing = list(self.store_dir.glob("PM-*.json"))
        return len(existing)
