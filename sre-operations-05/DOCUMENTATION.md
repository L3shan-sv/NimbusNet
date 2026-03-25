# NimbusNet Phase 5 — Technical Documentation
## SRE Operations Layer: Deep Reference

**Version:** 0.5.0
**Status:** Implementation Complete
**Dependencies:** Phase 3 (ML scoring), Phase 4 (control plane)

---

## Table of Contents

1. [Architecture & Data Flow](#1-architecture--data-flow)
2. [Incident Lifecycle](#2-incident-lifecycle)
3. [Runbook Design Principles](#3-runbook-design-principles)
4. [Runbook Executor — Deep Dive](#4-runbook-executor--deep-dive)
5. [Postmortem Generation — Deep Dive](#5-postmortem-generation--deep-dive)
6. [Notification Matrix — Design Rationale](#6-notification-matrix--design-rationale)
7. [Escalation Policy](#7-escalation-policy)
8. [SLO Budget Tracking](#8-slo-budget-tracking)
9. [Cross-Phase Feedback Loops](#9-cross-phase-feedback-loops)
10. [Operational Runbook: Phase 5](#10-operational-runbook-phase-5)
11. [Failure Modes & Mitigations](#11-failure-modes--mitigations)
12. [Integration Contracts](#12-integration-contracts)

---

## 1. Architecture & Data Flow

```
Phase 4 FSM event (DEGRADED / FAILED / HEALTHY)
        │
        ▼  POST /fsm/degraded
┌─────────────────────────────────────────────────────────┐
│                   SRE Operations Server                 │
│                                                         │
│  IncidentManager.open()                                 │
│         │                                               │
│         ├──► Notifier.on_incident_opened()  → Slack     │
│         └──► RunbookExecutor.execute_for_incident()     │
│                       │                                 │
│              (background thread)                        │
│                       │                                 │
│              Step 1 → Step 2 → ... → Final Verify       │
│                       │                                 │
│              Phase4Client.notify_runbook_succeeded()    │
│                       │                                 │
│              on_complete() ──► PostmortemGenerator      │
│                                       │                 │
│                               Notifier.on_auto_resolved │
│                               → Slack FYI               │
│                                                         │
└─────────────────────────────────────────────────────────┘

If runbook fails:
  Phase4Client.notify_runbook_failed()
  → Phase 4 FSM: HEALING → FAILED
  → POST /fsm/failed received
  → Notifier.on_escalation() → Slack #alerts + PagerDuty
```

---

## 2. Incident Lifecycle

### State Transitions

```
[no incident]
     │  FSM: HEALTHY → DEGRADED
     │  POST /fsm/degraded received
     ▼
  OPEN
     │  RunbookExecutor starts
     │  Notifier: Slack FYI
     │
     ├── Runbook succeeds → MITIGATED
     │         │
     │         │  FSM: HEALING → RECOVERED → HEALTHY
     │         │  POST /fsm/healthy received
     │         ▼
     │      RESOLVED (auto)
     │         │  PostmortemGenerator.generate()
     │         │  Notifier: Slack FYI (auto-resolved)
     │         ▼
     │       CLOSED
     │
     └── Runbook fails → still OPEN, escalated_to_sre = True
               │
               │  FSM: HEALING → FAILED
               │  POST /fsm/failed received
               │  PagerDuty fires
               │
               │  SRE acknowledges, manually resolves
               │  POST /incidents/{id}/resolve
               │  Phase4Client.notify_manual_override(HEALTHY)
               ▼
           RESOLVED (manual)
               │  PostmortemGenerator.generate()
               │  Notifier: Slack FYI (resolved)
               │  pm.requires_human_review = True
               ▼
             CLOSED
```

### Crash Recovery

Incidents are persisted to JSON on every state change. On server restart, `IncidentManager._load_active()` scans the store directory for incidents with `status == "open"` or `"mitigated"` and reloads them into `_active`. The runbook executor does NOT auto-restart after a crash — this prevents duplicate runbook execution. An SRE must manually trigger re-execution or resolve the incident.

---

## 3. Runbook Design Principles

Each runbook embodies five properties that make it genuinely production-grade:

**1. Preconditions before execution**
Before the first step runs, preconditions are checked. "At least one other region must be HEALTHY" is checked before RB-001. If preconditions fail, the runbook is skipped and the incident immediately escalates. This prevents runbooks from making things worse when the environment is already degraded.

**2. Every step has a verification gate**
After each command, a verification expression confirms the step had the intended effect. A step that executed but didn't achieve its goal fails its verification and retries. This is the difference between "I ran the command" and "the command worked."

**3. Every step has a rollback**
When a step fails all retries, its rollback command runs. RB-003's Terraform rollback is `terraform destroy` — drastic but correct. The rollback for RB-001 Step 1 escalates to the SRE immediately because if the data plane failover didn't work, no further automation can fix it.

**4. TTL per runbook**
RB-001 (region failover): 10 minutes. If healing can't be verified in 10 minutes, something is genuinely broken and needs a human. RB-003 (cold standby): 15 minutes because Terraform provision takes 8+ minutes. The TTL matches the expected execution time.

**5. Postmortem hook unconditional**
The `on_complete` callback fires regardless of runbook outcome — success, failure, or timeout. A successful auto-resolution still generates a postmortem. This is how the system learns from even routine incidents.

---

## 4. Runbook Executor — Deep Dive

### Threading Model

Each runbook execution runs in a `threading.Thread(daemon=True)`. The main FastAPI event loop remains unblocked. This means:
- Multiple regions can execute runbooks simultaneously
- The HTTP server continues responding during runbook execution
- If the server shuts down mid-runbook, the daemon thread is killed (no orphaned processes)

`_active_runs` tracks live threads per region. Before starting a new thread, the executor checks if a live thread already exists for that region — preventing duplicate runbook execution.

### Retry and Backoff

Step retries use exponential backoff: `time.sleep(2 ** attempt)`. For `retries=3`:
- Attempt 1: immediate
- Attempt 2: 1s delay
- Attempt 3: 2s delay
- Attempt 4: 4s delay (final)

Total time for a step that fails all retries: up to `timeout_s + 7s` (backoff time). All retry delays are bounded by the runbook TTL deadline — a step that's eating into the TTL gets a shorter retry window.

### Dry Run Mode

`dry_run=True` logs every command without executing it. All steps return SUCCESS. Verification gates return True. This is the mode used in CI testing (Phase 6 chaos gate). The postmortem hook still fires — dry run produces real postmortems for inspection.

---

## 5. Postmortem Generation — Deep Dive

### Root Cause Analysis via SHAP

The most valuable auto-generated section is `root_cause`. When Phase 3's XGBoost model scores the incident metric, it produces SHAP feature importances:

```json
{
  "rtt_spike": 0.34,
  "retransmit_rate": 0.28,
  "rtt_jitter_ratio": 0.19,
  "packet_loss_signal": 0.11,
  "high_jitter": 0.08
}
```

The postmortem generator translates this into a human-readable root cause:
> "XGBoost classifier identified the primary contributing signals as: rtt_spike (34%), retransmit_rate (28%), rtt_jitter_ratio (19%). Fault type classified as region partition. LSTM temporal sequence confidence: 87%."

This is more actionable than "something broke in us-east-1." It tells the SRE exactly which signals drove the decision and how confident the system was.

### Timeline Auto-Population

The timeline is built from:
1. `incident.detected_at_ns` — when ML first fired
2. `incident.degraded_at_ns` — when FSM transitioned
3. `incident.healing_at_ns` — when runbook started
4. FSM transitions from Phase 4 (if provided via API)
5. `incident.resolved_at_ns` — when FSM returned to HEALTHY
6. `incident.escalated_at_ns` — if escalated

A P0 postmortem timeline might look like:
```
02:14:33 UTC — ML detection (IF: 87%, XGB: CRITICAL)
02:14:34 UTC — FSM: HEALTHY → DEGRADED (soft failover started)
02:14:35 UTC — Runbook RB-001 started
02:14:45 UTC — FSM: DEGRADED → HEALING (runbook notified FSM)
02:19:12 UTC — Runbook RB-001 failed (step 5 timeout)
02:19:12 UTC — FSM: HEALING → FAILED
02:19:12 UTC — Escalated to SRE (PagerDuty fired)
02:24:01 UTC — SRE acknowledged
02:31:45 UTC — Incident resolved (manual)
```

### P2/P3 Auto-Publish

P2 and P3 postmortems are auto-published without human review. This is a deliberate policy decision: the cognitive overhead of reviewing every minor incident postmortem erodes the value of the postmortem process. SREs should spend their review time on P0/P1 postmortems where the learnings are significant.

P2/P3 postmortems are still searchable and linkable — they're not discarded. An SRE can open any postmortem via `GET /postmortems/{id}` and add notes at any time.

---

## 6. Notification Matrix — Design Rationale

### Why Slack FYI for Auto-Resolved?

If an SRE is paged every time NimbusNet auto-resolves an incident, two things happen:
1. Alert fatigue — SREs start ignoring pages
2. The value of PagerDuty is diluted — when it fires, it no longer feels urgent

The Slack FYI approach respects SRE attention. They see what happened, confirm the system handled it, and move on. The channel name (`#nimbusnet-sre`) signals low urgency. The message format uses `:white_check_mark:` and explicitly says "FYI only — no action required."

### Why PagerDuty Only for FAILED/P0/P1?

NimbusNet is designed to handle P2/P3 incidents autonomously. Paging for P2/P3 would mean paging for incidents that are, by definition, low customer impact. The SRE on-call should only be interrupted for incidents where their judgement is genuinely needed.

The FAILED state is the explicit marker: "automation has exhausted itself." This is when human judgement has the most value. Paging at FAILED rather than at DEGRADED gives automation its full TTL to resolve the issue first.

### Cold Standby Exception

RB-003 always pages the SRE even if it succeeds. Cold standby is a capacity-constrained state — the infrastructure is not in normal topology. The SRE must plan for:
- Returning to normal topology after the failed region recovers
- Capacity limits of the cold standby environment
- Cost implications of running cold standby for extended periods

These decisions require human judgement regardless of automation success.

---

## 7. Escalation Policy

### Tier Structure

```
Tier 0 (immediate): P0 FSM FAILED → PagerDuty primary on-call
Tier 1 (5 minutes): P0 unacknowledged → PagerDuty escalation to secondary
Tier 2 (15 minutes): P0 still unresolved → Slack channel + email manager

Tier 0 (immediate): P1 FSM FAILED → PagerDuty primary on-call
Tier 1 (15 minutes): P1 unacknowledged → PagerDuty escalation

P2/P3: Slack #nimbusnet-alerts only, no PagerDuty
```

### Acknowledgment Requirement

P0 and P1 incidents require PagerDuty acknowledgment. The `incidents/{id}/ack` endpoint records SRE acknowledgment and can be called from PagerDuty webhooks. Unacknowledged P0s escalate automatically through PagerDuty's escalation policy.

---

## 8. SLO Budget Tracking

### Budget Calculation

```
Monthly budget (99.9% SLO): 43.8 minutes
Per incident budget consumed: incident.ttr_s / 60.0 minutes

Example: 5-minute P1 incident = 5/43.8 = 11.4% of monthly budget
```

This is included in every postmortem under `impact`. It makes the real cost of each incident visible without requiring manual calculation.

### Budget Exhaustion

When `slo_recalibrated = True` in the postmortem (triggered when `error_budget_consumed_minutes > 5.0`), it's a flag for the SRE to review whether SLO targets or alert thresholds need adjustment. This is not automated — it's a prompt for human judgement about whether the SLO is right for this system at this scale.

---

## 9. Cross-Phase Feedback Loops

Phase 5 closes four feedback loops:

**Loop 1: ML Flywheel**
When `pm.ml_retrain_triggered = True`, the server POSTs to Phase 3 `/models/retrain`. The incident's ML scores are already in the flywheel (Phase 3's `ingest_production_outcome` is called with the actual fault type confirmed in the postmortem). This improves model accuracy for future incidents of the same type.

**Loop 2: Runbook Updates**
When `pm.runbook_updated = True`, it signals that one or more runbook steps failed during execution. This doesn't automatically update the runbook code — that requires SRE review. But it adds the step failure to the postmortem's action items, prompting a runbook improvement sprint.

**Loop 3: SLO Recalibration**
When a single incident consumes > 5 minutes of budget, the postmortem flags `slo_recalibrated = True`. Combined with the trending data from Phase 1's Prometheus multi-window burn rate rules, this gives the SRE the data to decide whether the SLO is appropriate.

**Loop 4: Chaos Experiments**
When `pm.chaos_experiment_added = True` (for CASCADING, DUAL_REGION_FAILURE, UNKNOWN fault types), Phase 6's chaos suite should add a new experiment targeting this fault signature. This ensures GameDay sessions cover the full distribution of real production failure modes, not just common synthetic ones.

---

## 10. Operational Runbook: Phase 5

### RB-P5-001: SRE Operations Service Unavailable

**Symptom:** Phase 4 FSM events are not being received. Incidents are not being opened.

**Impact:** Runbooks won't execute. SRE won't be notified. Phase 4 continues operating (FSM still transitions, route tables still update). The SRE layer is non-critical for the healing path — it's only critical for notification and escalation.

**Resolution:**
```bash
# Restart service
systemctl restart nimbusnet-sre || docker restart nimbusnet-sre

# Verify health
curl http://localhost:8002/health

# Check for active incidents that missed notifications
curl http://localhost:8002/incidents/active

# Manually trigger notifications for missed incidents
curl -X POST http://localhost:8002/fsm/degraded \
  -d '{"region": "us-east-1", "fault_type": "UNKNOWN", ...}'
```

### RB-P5-002: Postmortem Store Full / Corrupted

**Symptom:** `GET /postmortems` returns errors. Postmortem generation fails.

**Resolution:**
```bash
# Check disk space
df -h /opt/nimbusnet/sre-data

# List postmortem files
ls -la /opt/nimbusnet/sre-data/postmortems/

# Archive old postmortems (> 90 days)
find /opt/nimbusnet/sre-data/postmortems -name "PM-*.json" \
  -mtime +90 -exec gzip {} \;
```

---

## 11. Failure Modes & Mitigations

| Failure | Impact | Mitigation |
|---------|--------|-----------|
| Slack webhook fails | SRE not notified | Retried once; failure logged to CloudWatch. PagerDuty still fires independently. |
| PagerDuty API down | SRE not paged | Retry with exponential backoff. After 3 failures: log CRITICAL in CloudWatch — treated as SEV-0. |
| Runbook executor crash | Runbook doesn't complete | Daemon thread killed. Incident stays OPEN. FSM TTL eventually escalates to FAILED → PagerDuty fires. |
| Phase 4 client timeout | FSM not notified of runbook result | FSM TTL escalates independently (HEALING → FAILED after 10m). No stuck states. |
| Postmortem generation fails | No postmortem | Incident is already resolved. Log error. SRE can manually trigger: `POST /incidents/{id}/generate-postmortem`. |
| Incident store corruption | Can't reload active incidents | JSONL is per-file — single corrupted file doesn't affect others. Corrupt files are skipped. |

---

## 12. Integration Contracts

### Phase 4 → Phase 5 (inbound events)

```
POST /fsm/degraded     Body: FSMDegradedEvent
POST /fsm/failed       Body: FSMFailedEvent
POST /fsm/healthy      Body: FSMHealthyEvent
```

Phase 4 calls these on every FSM transition. Phase 5 is idempotent on duplicate events.

### Phase 5 → Phase 4 (outbound notifications)

```
POST {phase4_url}/api/fsm/runbook-started   {"region": "us-east-1"}
POST {phase4_url}/api/fsm/runbook-succeeded {"region": "us-east-1"}
POST {phase4_url}/api/fsm/runbook-failed    {"region": "us-east-1"}
POST {phase4_url}/api/fsm/override          {"region": "us-east-1", "target_state": "HEALTHY"}
```

### Phase 5 → Phase 3 (ML retrain trigger)

```
POST {ml_url}/models/retrain  {"trigger": "production_incident", "fault_type": "REGION_PARTITION"}
```

Called automatically when postmortem's `ml_retrain_triggered == True`.

### Phase 6 → Phase 5 (chaos session labeling)

```
POST /flywheel/ingest  (proxied to Phase 3)
  Called by Phase 6 chaos framework after each tc qdisc session.
  Labels: fault_type, severity, session_id, confidence=0.95
```
