"""
types.py — Shared data contracts for the NimbusNet SRE Operations Layer.

The SRE layer sits at the boundary between the autonomous system and
human operators. These types model that boundary precisely:

    Incident      — the lifecycle of a single fault event
    Runbook       — a structured remediation procedure
    RunbookResult — what happened when a runbook executed
    Postmortem    — the structured learning artefact generated after every incident
    EscalationPolicy — who gets paged, when, and through which channel
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Optional
import time


# ─── Incident ──────────────────────────────────────────────────────────────────

class IncidentSeverity(str, Enum):
    P0 = "P0"  # Total regional outage, revenue impact
    P1 = "P1"  # Significant degradation, customer-facing
    P2 = "P2"  # Partial degradation, limited customer impact
    P3 = "P3"  # Minor issue, no customer impact

class IncidentStatus(str, Enum):
    OPEN        = "open"
    MITIGATED   = "mitigated"   # automated healing resolved it
    RESOLVED    = "resolved"    # SRE confirmed resolution
    CLOSED      = "closed"      # postmortem complete


@dataclass
class Incident:
    """
    The full lifecycle record of a single fault event.
    Created when the FSM transitions HEALTHY → DEGRADED.
    Closed when postmortem is generated.
    """
    id:              str          # e.g. "INC-2024-001"
    region:          str
    severity:        IncidentSeverity
    status:          IncidentStatus

    # Timing
    detected_at_ns:  int          # when ML first fired
    degraded_at_ns:  int          # when FSM entered DEGRADED
    healing_at_ns:   Optional[int] = None   # when runbook started
    resolved_at_ns:  Optional[int] = None   # when FSM returned to HEALTHY
    closed_at_ns:    Optional[int] = None   # when postmortem filed

    # ML context (from Phase 3 scored metric at detection time)
    fault_type:      str = "UNKNOWN"
    if_score:        float = 0.0
    xgb_severity:    str = "NOMINAL"
    lstm_confidence: float = 0.0
    rtt_ewma_us:     int = 0
    retransmit_rate: float = 0.0

    # Runbook execution
    runbook_id:      Optional[str] = None
    runbook_result:  Optional[str] = None   # "SUCCESS" | "FAILURE" | "TIMEOUT"
    runbook_steps_completed: int = 0
    runbook_steps_total: int = 0

    # Escalation
    escalated_to_sre: bool = False
    escalated_at_ns:  Optional[int] = None
    sre_acknowledged: bool = False
    sre_ack_at_ns:    Optional[int] = None

    # SLO impact
    error_budget_consumed_minutes: float = 0.0

    @property
    def duration_s(self) -> float:
        end = self.resolved_at_ns or time.time_ns()
        return (end - self.detected_at_ns) / 1e9

    @property
    def ttd_s(self) -> float:
        """Time to detect (detection → FSM degraded)."""
        return (self.degraded_at_ns - self.detected_at_ns) / 1e9

    @property
    def ttr_s(self) -> Optional[float]:
        """Time to resolve."""
        if self.resolved_at_ns is None:
            return None
        return (self.resolved_at_ns - self.degraded_at_ns) / 1e9

    @property
    def was_auto_resolved(self) -> bool:
        return self.runbook_result == "SUCCESS" and not self.escalated_to_sre


# ─── Runbook ──────────────────────────────────────────────────────────────────

class RunbookStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    SUCCESS   = "success"
    FAILED    = "failed"
    TIMEOUT   = "timeout"
    SKIPPED   = "skipped"  # precondition not met


@dataclass
class RunbookStep:
    """One step in a runbook procedure."""
    number:       int
    title:        str
    description:  str
    command:      Optional[str] = None   # shell command or AWS API call
    verify:       Optional[str] = None   # verification command/query
    timeout_s:    int = 60
    retries:      int = 3
    rollback:     Optional[str] = None   # what to do if this step fails

    # Runtime state
    status:       RunbookStatus = RunbookStatus.PENDING
    started_at:   Optional[float] = None
    completed_at: Optional[float] = None
    output:       str = ""
    error:        str = ""


@dataclass
class Runbook:
    """
    A structured remediation procedure for a specific fault type.

    Every runbook has:
      - Metadata (ID, fault type, severity)
      - Preconditions (checked before execution)
      - Numbered steps (with verification and rollback)
      - A verification gate (confirms healing before marking SUCCESS)
      - A postmortem hook (fires regardless of outcome)
    """
    id:             str           # e.g. "RB-001"
    name:           str
    fault_type:     str           # FaultType name this runbook handles
    min_severity:   IncidentSeverity
    description:    str

    # Execution
    steps:          list[RunbookStep] = field(default_factory=list)
    ttl_s:          int = 600     # 10 minutes — matches FSM HEALING timeout
    max_retries:    int = 2       # how many times to retry the full runbook

    # Preconditions — checked before execution begins
    preconditions:  list[str] = field(default_factory=list)

    # Verification — must pass for SUCCESS
    verification_query:  Optional[str] = None
    verification_threshold: float = 0.0


@dataclass
class RunbookResult:
    """The outcome of a single runbook execution."""
    runbook_id:   str
    incident_id:  str
    region:       str
    status:       RunbookStatus
    started_at:   float
    completed_at: float
    steps_completed: int
    steps_total:  int
    step_results: list[RunbookStep] = field(default_factory=list)
    error:        str = ""
    verified:     bool = False  # did the verification gate pass?

    @property
    def duration_s(self) -> float:
        return self.completed_at - self.started_at


# ─── Postmortem ───────────────────────────────────────────────────────────────

@dataclass
class PostmortemSection:
    title:   str
    content: str


@dataclass
class Postmortem:
    """
    Auto-generated postmortem for every incident.

    The skeleton is populated automatically from:
      - FSM transition history (from Phase 4)
      - ML scored metric at detection time (from Phase 3)
      - Runbook execution result
      - Traffic weight timeline

    The "narrative" and "action items" sections require human input
    for P0/P1 incidents. P2/P3 postmortems are fully auto-generated.
    """
    id:           str            # e.g. "PM-2024-001"
    incident_id:  str
    region:       str
    severity:     IncidentSeverity
    status:       str            # "draft" | "under_review" | "published"

    # Auto-populated sections
    summary:          str = ""   # 2-3 sentence executive summary
    timeline:         list[dict] = field(default_factory=list)  # FSM transitions
    root_cause:       str = ""   # XGBoost SHAP feature importances
    impact:           str = ""   # duration, regions affected, error budget consumed
    detection:        str = ""   # how it was detected (ML scores)
    resolution:       str = ""   # what the runbook did

    # Human-required sections (P0/P1 only)
    contributing_factors: str = ""
    action_items:     list[dict] = field(default_factory=list)

    # Metadata
    generated_at:     float = field(default_factory=time.time)
    requires_human_review: bool = False
    sre_author:       Optional[str] = None

    # Downstream effects
    ml_retrain_triggered: bool = False
    runbook_updated:      bool = False
    slo_recalibrated:     bool = False
    chaos_experiment_added: bool = False


# ─── Escalation ───────────────────────────────────────────────────────────────

class NotificationChannel(str, Enum):
    SLACK     = "slack"
    PAGERDUTY = "pagerduty"
    EMAIL     = "email"
    WEBHOOK   = "webhook"


@dataclass
class EscalationTier:
    """One tier in an escalation policy."""
    tier:        int
    delay_s:     int    # how long to wait before escalating to this tier
    channels:    list[NotificationChannel]
    message:     str    # notification message template
    require_ack: bool   # does this tier require SRE acknowledgment?


@dataclass
class EscalationPolicy:
    """
    Defines how incidents are escalated.

    NimbusNet uses a passive-to-active escalation model:
      - Auto-resolved incidents:  Slack FYI only (SRE informed, not burdened)
      - P2/P3 escalations:        Slack channel alert
      - P0/P1 escalations:        PagerDuty + Slack
      - TTL breach:               PagerDuty (mandatory)
    """
    policy_id:   str
    name:        str
    tiers:       list[EscalationTier]
    auto_close_resolved: bool = True  # close without SRE if auto-resolved
