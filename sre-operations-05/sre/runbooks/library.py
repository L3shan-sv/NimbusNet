"""
library.py — NimbusNet Runbook Library.

The three runbooks that cover every fault type in the system:

  RB-001  Region Failover
          Triggered by: REGION_PARTITION, NETWORK_LATENCY (high severity), CASCADING
          Action: Verify data plane failover, drain DNS, confirm healthy region is serving

  RB-002  ML Model Degradation
          Triggered by: ML scoring service unavailable or producing stale/wrong signals
          Action: Restart ML serving, trigger flywheel retrain, validate model health

  RB-003  Cold Standby Provisioning
          Triggered by: two active regions simultaneously degraded (dual-region failure)
          Action: Terraform apply cold standby, verify provision, route traffic to it

Each runbook is a pure data structure. The RunbookExecutor (executor.py) drives execution.
"""

from __future__ import annotations

from ..types import Runbook, RunbookStep, IncidentSeverity


# ─── RB-001: Region Failover ──────────────────────────────────────────────────

RB_001_REGION_FAILOVER = Runbook(
    id="RB-001",
    name="Region Failover",
    fault_type="REGION_PARTITION",
    min_severity=IncidentSeverity.P1,
    description=(
        "Autonomous region failover procedure. Verifies the data plane has rerouted traffic, "
        "confirms the healthy region is absorbing load, ensures Route 53 has drained the failed "
        "region, and validates the cold standby is not needed."
    ),
    ttl_s=600,
    max_retries=2,
    preconditions=[
        "At least one other region must be in HEALTHY state",
        "Phase 4 control plane must be reachable",
        "Route 53 hosted zone must be accessible",
    ],
    verification_query="rtt_ewma_us < 150000 AND retransmit_rate < 0.05",
    verification_threshold=0.0,
    steps=[
        RunbookStep(
            number=1,
            title="Verify data plane failover",
            description=(
                "Confirm the VPC route table has already been updated by Phase 4. "
                "The control plane updates the route table immediately on DEGRADED entry — "
                "this step verifies it took effect."
            ),
            command="aws ec2 describe-route-tables --filters Name=route.state,Values=active",
            verify="grep -q 'tgw-attach' <<< $OUTPUT && echo 'PASS'",
            timeout_s=30,
            retries=2,
            rollback="Alert SRE — data plane failover did not complete. Manual intervention required.",
        ),
        RunbookStep(
            number=2,
            title="Confirm healthy region is absorbing traffic",
            description=(
                "Check that the surviving regions are serving requests successfully. "
                "RTT should be elevated slightly (more traffic) but retransmit rate should be nominal."
            ),
            command="curl -s http://nimbusnet-ml-scoring:8001/models/stats",
            verify="healthy_region_rtt < 200ms AND retransmit_rate < 0.03",
            timeout_s=60,
            retries=3,
            rollback=None,
        ),
        RunbookStep(
            number=3,
            title="Verify Route 53 health check is failing for failed region",
            description=(
                "The Phase 2 Go agent's /healthz is returning 503 on the failed region. "
                "Route 53 should have marked the region unhealthy within 20 seconds. "
                "This step confirms DNS plane convergence."
            ),
            command="aws route53 get-health-check-status --health-check-id {health_check_id}",
            verify="StatusReport shows 'Failure'",
            timeout_s=45,
            retries=3,
        ),
        RunbookStep(
            number=4,
            title="Check cold standby threshold",
            description=(
                "If only one active region remains healthy, evaluate whether to provision "
                "the cold standby (RB-003 would be triggered separately). "
                "Two healthy regions is the minimum safe configuration."
            ),
            command="curl -s http://nimbusnet-controlplane:9091/status | jq '.regions[] | select(.fsm_state == \"HEALTHY\")'",
            verify="healthy_count >= 2",
            timeout_s=30,
            retries=1,
            rollback="Trigger RB-003 (Cold Standby Provisioning) if healthy_count == 1",
        ),
        RunbookStep(
            number=5,
            title="Monitor failed region for recovery signal",
            description=(
                "Wait for the failed region's ML anomaly score to drop below threshold. "
                "The bandit will detect RTT improvement automatically. "
                "Do not manually restore until this step passes."
            ),
            command="watch -n10 'curl -s http://nimbusnet-ml-scoring:8001/score/batch'",
            verify="if_score < 0.30 for 3 consecutive windows (150ms)",
            timeout_s=300,
            retries=1,
        ),
        RunbookStep(
            number=6,
            title="Verify healing and notify FSM",
            description=(
                "Healing verified — signal Phase 4 control plane that the runbook succeeded. "
                "The FSM will transition HEALING → RECOVERED and begin traffic ramp-back."
            ),
            command="POST /api/fsm/runbook-succeeded region={region}",
            verify="FSM state == RECOVERED",
            timeout_s=30,
            retries=2,
        ),
    ],
)


# ─── RB-002: ML Model Degradation ────────────────────────────────────────────

RB_002_ML_DEGRADATION = Runbook(
    id="RB-002",
    name="ML Model Degradation",
    fault_type="ML_MODEL_DEGRADED",
    min_severity=IncidentSeverity.P2,
    description=(
        "Remediation for ML scoring service failures or model staleness. "
        "Restarts the scoring service, triggers flywheel retrain if data quality is the issue, "
        "and validates model health before restoring autonomous operation."
    ),
    ttl_s=300,
    max_retries=2,
    preconditions=[
        "ML scoring service endpoint must be reachable (even if returning errors)",
        "Flywheel data store must be accessible",
    ],
    verification_query="models/health returns status == 'ok'",
    steps=[
        RunbookStep(
            number=1,
            title="Assess ML service health",
            description="Check which models are failing and why.",
            command="curl -s http://nimbusnet-ml-scoring:8001/models/health | jq .",
            verify="Identify failing models",
            timeout_s=15,
            retries=2,
        ),
        RunbookStep(
            number=2,
            title="Restart ML scoring service",
            description=(
                "Restart the FastAPI serving process. This reloads model weights from disk "
                "and re-initialises the per-flow LSTM windows."
            ),
            command="systemctl restart nimbusnet-ml-scoring || docker restart nimbusnet-ml",
            verify="curl -f http://nimbusnet-ml-scoring:8001/models/health",
            timeout_s=60,
            retries=2,
            rollback="Escalate to SRE — ML service will not start.",
        ),
        RunbookStep(
            number=3,
            title="Validate model predictions",
            description=(
                "Send a known-anomalous synthetic metric and verify the scoring pipeline "
                "returns a reasonable result. This is the model smoke test."
            ),
            command=(
                "curl -X POST http://nimbusnet-ml-scoring:8001/score "
                "-d '{\"rtt_ewma_us\": 850000, \"retransmit_rate\": 0.35, "
                "\"max_anomaly_score\": 88, \"region\": \"test\", "
                "\"window_start_ns\": 0, \"window_end_ns\": 50000000}'"
            ),
            verify="isolation_forest_score > 0.60 AND xgboost_severity in ['DEGRADED', 'CRITICAL']",
            timeout_s=10,
            retries=3,
        ),
        RunbookStep(
            number=4,
            title="Check if retrain is needed",
            description=(
                "If model health shows staleness (version hash unchanged for > 48h "
                "and > 500 new samples in flywheel), trigger a retrain."
            ),
            command="curl -s http://nimbusnet-ml-scoring:8001/models/stats",
            verify="retrain_recommended == false OR trigger_retrain()",
            timeout_s=30,
            retries=1,
        ),
        RunbookStep(
            number=5,
            title="Trigger flywheel retrain if needed",
            description=(
                "Force a retrain if staleness was detected. "
                "The retrain runs in the background — this step queues it and continues."
            ),
            command=(
                "curl -X POST http://nimbusnet-ml-scoring:8001/models/retrain "
                "-d '{\"trigger\": \"runbook_rb002\", \"token\": \"$ML_ADMIN_TOKEN\"}'"
            ),
            verify="retrain_queued == true",
            timeout_s=10,
            retries=2,
        ),
    ],
)


# ─── RB-003: Cold Standby Provisioning ───────────────────────────────────────

RB_003_COLD_STANDBY = Runbook(
    id="RB-003",
    name="Cold Standby Provisioning",
    fault_type="DUAL_REGION_FAILURE",
    min_severity=IncidentSeverity.P0,
    description=(
        "Emergency cold standby provisioning for dual-region failure scenarios. "
        "Triggers Terraform to provision the pre-configured cold standby environment, "
        "waits for it to become healthy, routes traffic to it, and notifies SRE. "
        "This runbook is the last line of defence before total service unavailability."
    ),
    ttl_s=900,  # 15 minutes — Terraform provision takes longer
    max_retries=1,  # No retry on cold standby — too slow
    preconditions=[
        "Terraform state must be accessible (S3 backend)",
        "Cold standby region must NOT already be provisioned (idempotency check)",
        "DynamoDB lock must be acquirable",
        "AWS credentials must have cold standby IAM permissions",
    ],
    verification_query="cold_standby_region health check passes",
    steps=[
        RunbookStep(
            number=1,
            title="Acquire DynamoDB cold standby lock",
            description=(
                "Prevent duplicate provisioning if both active regions fire RB-003 simultaneously. "
                "The DynamoDB conditional write ensures only one Terraform run executes."
            ),
            command=(
                "aws dynamodb put-item --table-name nimbusnet-control-plane-locks "
                "--item '{\"lock_key\":{\"S\":\"cold-standby-provision\"}}' "
                "--condition-expression 'attribute_not_exists(lock_key)'"
            ),
            verify="Exit code 0 = lock acquired; ConditionalCheckFailed = another agent running",
            timeout_s=10,
            retries=1,
            rollback="Log 'Another agent is provisioning cold standby — backing off'",
        ),
        RunbookStep(
            number=2,
            title="Verify cold standby is not already active",
            description="Check Terraform state to confirm cold standby is not already running.",
            command="terraform -chdir=/opt/nimbusnet/terraform/cold-standby output -json | jq .status",
            verify="status != 'active'",
            timeout_s=30,
            retries=2,
        ),
        RunbookStep(
            number=3,
            title="Terraform apply cold standby environment",
            description=(
                "Provision the pre-configured cold standby environment. "
                "This creates VPC, ALB, ECS/EC2 instances, and registers with the TGW. "
                "The Terraform plan is pre-validated — this is apply-only."
            ),
            command=(
                "terraform -chdir=/opt/nimbusnet/terraform/cold-standby apply "
                "-auto-approve -var='environment=cold-standby' "
                "-var='incident_id={incident_id}'"
            ),
            verify="terraform output alb_dns_name",
            timeout_s=480,  # 8 minutes for full provision
            retries=0,
            rollback=(
                "terraform -chdir=/opt/nimbusnet/terraform/cold-standby destroy "
                "-auto-approve — ALERT SRE IMMEDIATELY if this fires"
            ),
        ),
        RunbookStep(
            number=4,
            title="Wait for cold standby health check to pass",
            description=(
                "Poll the cold standby's /healthz until it returns 200. "
                "Timeout of 5 minutes for application initialisation."
            ),
            command="until curl -sf http://{cold_standby_alb}/healthz; do sleep 10; done",
            verify="HTTP 200 from /healthz",
            timeout_s=300,
            retries=1,
        ),
        RunbookStep(
            number=5,
            title="Register cold standby in Route 53",
            description=(
                "Add the cold standby region to the latency routing policy. "
                "Route 53 will begin routing traffic to it immediately."
            ),
            command=(
                "aws route53 change-resource-record-sets "
                "--hosted-zone-id {hosted_zone_id} "
                "--change-batch file:///opt/nimbusnet/r53/cold-standby-record.json"
            ),
            verify="Route 53 change status == INSYNC",
            timeout_s=60,
            retries=2,
        ),
        RunbookStep(
            number=6,
            title="Update bandit with cold standby region",
            description=(
                "Register the cold standby region as a new arm in the multi-armed bandit. "
                "The bandit will begin routing traffic proportionally."
            ),
            command=(
                "curl -X POST http://nimbusnet-ml-scoring:8001/bandit/add-region "
                "-d '{\"region\": \"{cold_standby_region}\"}'"
            ),
            verify="bandit_arm_added == true",
            timeout_s=10,
            retries=3,
        ),
        RunbookStep(
            number=7,
            title="Page SRE — cold standby active",
            description=(
                "Cold standby is now serving traffic. SRE MUST be paged regardless of "
                "automation success. Cold standby capacity is limited and the SRE must "
                "plan for returning to normal topology."
            ),
            command="POST /api/escalate force=true reason='Cold standby activated'",
            verify="PagerDuty alert fired",
            timeout_s=30,
            retries=3,
            # No rollback — the page MUST fire
        ),
    ],
)


# ─── Registry ─────────────────────────────────────────────────────────────────

RUNBOOK_REGISTRY: dict[str, Runbook] = {
    rb.id: rb
    for rb in [RB_001_REGION_FAILOVER, RB_002_ML_DEGRADATION, RB_003_COLD_STANDBY]
}

# Fault type → runbook mapping
FAULT_TYPE_TO_RUNBOOK: dict[str, str] = {
    "REGION_PARTITION":  "RB-001",
    "NETWORK_LATENCY":   "RB-001",
    "PACKET_LOSS":       "RB-001",
    "CASCADING":         "RB-001",
    "CONGESTION":        "RB-001",
    "SYN_FLOOD":         "RB-001",
    "ML_MODEL_DEGRADED": "RB-002",
    "DUAL_REGION_FAILURE": "RB-003",
    "UNKNOWN":           "RB-001",   # default to region failover
}


def select_runbook(fault_type: str) -> Runbook:
    """Select the appropriate runbook for a given fault type."""
    runbook_id = FAULT_TYPE_TO_RUNBOOK.get(fault_type, "RB-001")
    return RUNBOOK_REGISTRY[runbook_id]
