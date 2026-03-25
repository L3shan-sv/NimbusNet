# NimbusNet — Phase 5: SRE Operations Layer

> **The immune system.** The SRE is never the first responder. The system is. This layer is the formal boundary between autonomous operation and human escalation. Everything before FAILED state — detection, healing, postmortem generation — is fully automated. The SRE is in the loop passively via Slack FYI, and actively only when automation has exhausted itself.

---

## The Core Design Principle

```
Autonomous boundary:
────────────────────────────────────────────────────────────
  Detection (Phase 2+3) → Incident opened → Runbook executes
  → Healed → Postmortem auto-generated → Slack FYI to SRE
────────────────────────────────────────────────────────────
Human boundary: crossed ONLY when TTL expires or P0/P1
  Runbook TTL exhausted → FSM: FAILED → PagerDuty fires
  P0/P1 auto-resolved  → Slack FYI (informed, not burdened)
  P0/P1 escalated      → Slack #alerts + PagerDuty
```

This is the distinction that makes NimbusNet genuinely FAANG-grade. The SRE's cognitive load is proportional to actual incidents that automation could not handle — not to every noisy alert.

---

## What This Phase Delivers

| Component | Description |
|-----------|-------------|
| `types.py` | Shared data contracts: `Incident`, `Runbook`, `RunbookStep`, `RunbookResult`, `Postmortem`, `EscalationPolicy` |
| `runbooks/incident_manager.py` | Incident lifecycle: open → track → resolve. Crash-recoverable (persists to JSON). One active incident per region. |
| `runbooks/library.py` | Three production runbooks: RB-001 (Region Failover), RB-002 (ML Model Degradation), RB-003 (Cold Standby Provisioning) |
| `runbooks/executor.py` | Autonomous runbook executor. Step-by-step with retries, timeouts, rollback. Final verification gate. Postmortem hook fires on every outcome. |
| `postmortem/generator.py` | Auto-populates postmortem skeleton from incident data, FSM transitions, SHAP importances. P0/P1 marked for human review. P2/P3 auto-published. Feeds back into ML flywheel, chaos suite, SLO recalibration. |
| `notifications/notifier.py` | Full notification matrix. Slack FYI for auto-resolved. PagerDuty only for P0/P1 or TTL breach. Formatted Slack blocks. PagerDuty Events API v2. |
| `server/server.py` | FastAPI coordination hub. Receives Phase 4 FSM events, drives runbooks, exposes incident/postmortem read API. |
| `server/phase4_client.py` | HTTP client for Phase 4 control plane FSM notifications. |
| `main.py` | Entrypoint. Wires all subsystems from config. |
| `configs/sre_config.yaml` | Annotated config reference. |

---

## The Notification Matrix

| Event | Channel | Action Required? |
|-------|---------|-----------------|
| Incident opened (any severity) | Slack `#nimbusnet-sre` | No — monitoring only |
| Auto-resolved (any severity) | Slack `#nimbusnet-sre` FYI | No — informed only |
| P2/P3 escalated | Slack `#nimbusnet-alerts` | Review when available |
| P1 escalated | Slack `#nimbusnet-alerts` + PagerDuty | Acknowledge within 15m |
| P0 escalated | Slack `#nimbusnet-alerts` + PagerDuty | Acknowledge within 5m |
| Runbook TTL breach | PagerDuty (mandatory) | Immediate |
| Cold standby activated | PagerDuty (mandatory) | Immediate |
| Postmortem needs review (P0/P1) | Slack `#nimbusnet-alerts` | Fill in + publish |

**The rule:** PagerDuty fires only when human judgement is genuinely needed. A SRE who gets paged at 3am should never see "auto-resolved 2 seconds later."

---

## The Three Runbooks

### RB-001 — Region Failover
**Triggers:** REGION_PARTITION, NETWORK_LATENCY, PACKET_LOSS, CASCADING, SYN_FLOOD, UNKNOWN  
**Steps:** Verify data plane failover → confirm healthy regions absorbing load → verify R53 health check failing → check cold standby threshold → monitor for recovery signal → signal FSM to RECOVERED  
**TTL:** 10 minutes

### RB-002 — ML Model Degradation
**Triggers:** ML_MODEL_DEGRADED (scoring service unavailable or producing stale signals)  
**Steps:** Assess model health → restart ML scoring service → validate predictions with synthetic metric → check if retrain needed → trigger flywheel retrain  
**TTL:** 5 minutes

### RB-003 — Cold Standby Provisioning
**Triggers:** DUAL_REGION_FAILURE (both active regions degraded)  
**Steps:** Acquire DynamoDB lock → verify not already active → terraform apply → wait for health check → register in Route 53 → add bandit arm → page SRE (mandatory, even on success)  
**TTL:** 15 minutes  
**Note:** Cold standby activation always pages the SRE regardless of automation success. Human oversight is mandatory for this scenario.

---

## Postmortem Lifecycle

```
Every incident (auto-resolved OR escalated)
        │
        ▼
PostmortemGenerator.generate()
        │
        ├── Auto-populated: summary, timeline, root cause (SHAP),
        │   impact (budget consumed), detection (ML scores), resolution
        │
        ├── P0/P1: status = "under_review"
        │         → Slack notification to SRE
        │         → SRE fills: contributing_factors, action_items
        │         → SRE publishes via PUT /postmortems/{id}
        │
        └── P2/P3: status = "published" immediately
                  → No human review required
                  → Slack FYI only
        │
        ▼
Downstream feedback (automatic):
  ml_retrain_triggered   → POST /models/retrain to Phase 3
  runbook_updated        → flag for runbook library review
  slo_recalibrated       → flag for SLO threshold review
  chaos_experiment_added → flag for Phase 6 chaos suite
```

---

## Quick Start

```bash
pip install -r requirements.txt

# Configure environment variables
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
export PAGERDUTY_ROUTING_KEY="your-routing-key"

# Run (dry_run: true in config suppresses actual Slack/PagerDuty delivery)
CONFIG_PATH=configs/sre_config.yaml python main.py

# Check health
curl http://localhost:8002/health

# List active incidents
curl http://localhost:8002/incidents/active

# List runbooks
curl http://localhost:8002/runbooks

# Simulate a DEGRADED event (test mode)
curl -X POST http://localhost:8002/fsm/degraded \
  -H 'Content-Type: application/json' \
  -d '{
    "region": "us-east-1",
    "detected_at_ns": 1700000000000000000,
    "fault_type": "REGION_PARTITION",
    "xgb_severity": "CRITICAL",
    "if_score": 0.87,
    "rtt_ewma_us": 850000,
    "retransmit_rate": 0.38
  }'
```

---

## File Tree

```
phase-05-sre-operations/
├── README.md                              ← you are here
├── DOCUMENTATION.md                       ← deep technical reference
├── main.py                                ← entrypoint
├── requirements.txt
├── sre/
│   ├── types.py                           ← shared data contracts
│   ├── runbooks/
│   │   ├── incident_manager.py            ← incident lifecycle
│   │   ├── library.py                     ← RB-001, RB-002, RB-003
│   │   └── executor.py                    ← autonomous runbook executor
│   ├── postmortem/
│   │   └── generator.py                   ← auto-postmortem generation
│   ├── notifications/
│   │   └── notifier.py                    ← Slack + PagerDuty routing
│   └── server/
│       ├── server.py                      ← FastAPI coordination hub
│       └── phase4_client.py               ← Phase 4 HTTP client
└── configs/
    └── sre_config.yaml                    ← annotated config reference
```

---

## What's Next — Phase 6

Phase 6 is the Chaos CI Gate — `tc qdisc` fault injection, chaos runner integrated into the CI pipeline, error budget enforcement, and the data flywheel connection that generates labeled training data as a side effect of every chaos session. One operation, two outputs.
