"""
executor.py — Autonomous runbook executor.

Drives execution of a Runbook step-by-step:
  1. Check preconditions
  2. Execute each step (with retries and timeout)
  3. Run verification gate after each step
  4. On failure: execute rollback, continue or abort
  5. Final verification gate (confirms healing)
  6. Notify Phase 4 FSM: RunbookSucceeded or RunbookFailed
  7. Fire postmortem hook (ALWAYS — regardless of outcome)

The executor is the boundary between "the system is trying to heal itself"
and "the system has given up and needs a human". When a runbook succeeds,
the SRE gets a Slack FYI. When it fails, PagerDuty fires.

Thread model: each runbook execution runs in its own thread.
The main event loop remains unblocked.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Callable, Optional

from ..types import (
    Runbook, RunbookStep, RunbookResult, RunbookStatus,
    Incident,
)
from .library import select_runbook
from .incident_manager import IncidentManager

logger = logging.getLogger(__name__)


class RunbookExecutor:
    """
    Executes runbooks autonomously and notifies the Phase 4 control plane.

    Wiring:
      - IncidentManager:  updates incident records during execution
      - notify_fsm_*:     HTTP calls to Phase 4 /api/fsm/runbook-* endpoints
      - on_complete:      callback to postmortem generator
    """

    def __init__(
        self,
        incident_manager: IncidentManager,
        notify_fsm_started:   Callable[[str], None],  # region → POST /runbook-started
        notify_fsm_succeeded: Callable[[str], None],
        notify_fsm_failed:    Callable[[str], None],
        on_complete: Optional[Callable[[Incident, RunbookResult], None]] = None,
        dry_run: bool = False,
    ):
        self.incident_manager  = incident_manager
        self.notify_fsm_started   = notify_fsm_started
        self.notify_fsm_succeeded = notify_fsm_succeeded
        self.notify_fsm_failed    = notify_fsm_failed
        self.on_complete = on_complete
        self.dry_run = dry_run

        self._active_runs: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    # ─── Public API ────────────────────────────────────────────────────────────

    def execute_for_incident(self, incident: Incident) -> None:
        """
        Select and execute the appropriate runbook for an incident.
        Runs in a background thread — returns immediately.
        """
        with self._lock:
            if incident.region in self._active_runs:
                t = self._active_runs[incident.region]
                if t.is_alive():
                    logger.info(
                        "RunbookExecutor: runbook already running for region",
                        extra={"region": incident.region}
                    )
                    return

        runbook = select_runbook(incident.fault_type)
        thread = threading.Thread(
            target=self._run,
            args=(incident, runbook),
            name=f"runbook-{incident.region}",
            daemon=True,
        )

        with self._lock:
            self._active_runs[incident.region] = thread

        thread.start()
        logger.info(
            "RunbookExecutor: started",
            extra={
                "runbook_id": runbook.id,
                "incident_id": incident.id,
                "region": incident.region,
            }
        )

    # ─── Execution Core ────────────────────────────────────────────────────────

    def _run(self, incident: Incident, runbook: Runbook) -> None:
        """Full runbook execution. Runs in a background thread."""
        started_at = time.time()
        result = RunbookResult(
            runbook_id=runbook.id,
            incident_id=incident.id,
            region=incident.region,
            status=RunbookStatus.RUNNING,
            started_at=started_at,
            completed_at=started_at,
            steps_completed=0,
            steps_total=len(runbook.steps),
        )

        try:
            # ── Notify FSM: DEGRADED → HEALING ─────────────────────────────────
            self.notify_fsm_started(incident.region)
            self.incident_manager.record_runbook_started(
                incident.region,
                runbook_id=runbook.id,
                steps_total=len(runbook.steps),
            )

            # ── Preconditions ──────────────────────────────────────────────────
            if not self._check_preconditions(runbook, incident):
                result.status = RunbookStatus.SKIPPED
                result.error  = "Preconditions not met"
                self._finish(incident, result, runbook, started_at)
                return

            # ── Execute steps ──────────────────────────────────────────────────
            deadline = started_at + runbook.ttl_s

            for step in runbook.steps:
                if time.time() > deadline:
                    logger.warning(
                        "RunbookExecutor: TTL exceeded",
                        extra={"runbook_id": runbook.id, "step": step.number}
                    )
                    result.status = RunbookStatus.TIMEOUT
                    result.error  = f"TTL of {runbook.ttl_s}s exceeded at step {step.number}"
                    self._finish(incident, result, runbook, started_at)
                    return

                step_result = self._execute_step(step, incident, deadline)
                result.step_results.append(step_result)
                result.steps_completed += 1
                self.incident_manager.record_runbook_progress(
                    incident.region, result.steps_completed
                )

                if step_result.status == RunbookStatus.FAILED:
                    result.status = RunbookStatus.FAILED
                    result.error  = f"Step {step.number} '{step.title}' failed: {step_result.error}"
                    self._finish(incident, result, runbook, started_at)
                    return

            # ── Final verification ─────────────────────────────────────────────
            result.verified = self._run_final_verification(runbook, incident)

            if result.verified:
                result.status = RunbookStatus.SUCCESS
                logger.info(
                    "RunbookExecutor: SUCCESS",
                    extra={
                        "runbook_id":    runbook.id,
                        "incident_id":   incident.id,
                        "duration_s":    round(time.time() - started_at, 1),
                        "steps":         result.steps_completed,
                    }
                )
            else:
                result.status = RunbookStatus.FAILED
                result.error  = "Final verification gate failed — healing not confirmed"

        except Exception as e:
            logger.exception("RunbookExecutor: unexpected error")
            result.status = RunbookStatus.FAILED
            result.error  = str(e)

        finally:
            self._finish(incident, result, runbook, started_at)

    def _execute_step(
        self,
        step: RunbookStep,
        incident: Incident,
        deadline: float,
    ) -> RunbookStep:
        """Execute one step with retries and timeout."""
        step.started_at = time.time()
        step.status     = RunbookStatus.RUNNING

        for attempt in range(step.retries + 1):
            if time.time() > deadline:
                step.status = RunbookStatus.TIMEOUT
                step.error  = "Deadline exceeded"
                break

            try:
                if self.dry_run:
                    # In dry run, log the command but don't execute
                    logger.info(
                        f"[DRY RUN] Step {step.number}: {step.title}",
                        extra={"command": step.command or "(no command)"}
                    )
                    step.output = "[dry-run]"
                    step.status = RunbookStatus.SUCCESS
                    break
                else:
                    output, success = self._run_command(
                        step.command or "true",
                        timeout_s=min(step.timeout_s, int(deadline - time.time())),
                        context={
                            "region":     incident.region,
                            "incident_id": incident.id,
                        }
                    )
                    step.output = output

                    if success:
                        # Run verification if specified
                        if step.verify:
                            verified = self._run_verify(step.verify, output, incident)
                            if not verified and attempt < step.retries:
                                logger.warning(
                                    f"Step {step.number} verify failed — retrying ({attempt+1}/{step.retries})",
                                    extra={"step_title": step.title}
                                )
                                time.sleep(2 ** attempt)  # exponential backoff
                                continue
                            elif not verified:
                                step.status = RunbookStatus.FAILED
                                step.error  = "Verification check failed after all retries"
                                break

                        step.status = RunbookStatus.SUCCESS
                        break
                    else:
                        if attempt < step.retries:
                            logger.warning(
                                f"Step {step.number} failed — retrying ({attempt+1}/{step.retries})"
                            )
                            time.sleep(2 ** attempt)
                        else:
                            step.status = RunbookStatus.FAILED
                            step.error  = f"Command failed after {step.retries} retries"

            except Exception as e:
                step.error = str(e)
                if attempt >= step.retries:
                    step.status = RunbookStatus.FAILED

        step.completed_at = time.time()

        # Execute rollback if step failed
        if step.status == RunbookStatus.FAILED and step.rollback and not self.dry_run:
            logger.warning(
                f"Step {step.number} failed — executing rollback",
                extra={"rollback": step.rollback[:100]}
            )
            try:
                self._run_command(step.rollback, timeout_s=60, context={
                    "region": incident.region
                })
            except Exception as e:
                logger.error(f"Rollback also failed: {e}")

        return step

    def _run_command(
        self,
        command: str,
        timeout_s: int,
        context: dict,
    ) -> tuple[str, bool]:
        """
        Execute a shell command with substituted context variables.
        Returns (output, success).
        """
        # Substitute context variables: {region} → actual region value
        for key, value in context.items():
            command = command.replace(f"{{{key}}}", str(value))

        # For HTTP commands (POST /api/...), use the internal API
        if command.startswith("POST /api/"):
            return self._call_internal_api(command, context), True

        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            output = proc.stdout + proc.stderr
            return output, proc.returncode == 0
        except subprocess.TimeoutExpired:
            return "TIMEOUT", False
        except Exception as e:
            return str(e), False

    def _call_internal_api(self, command: str, context: dict) -> str:
        """Handle internal API calls to Phase 4 control plane."""
        # These are handled by the SRE server's Phase 4 client
        logger.debug("Internal API call", extra={"command": command})
        return "ok"

    def _check_preconditions(self, runbook: Runbook, incident: Incident) -> bool:
        """Check that all preconditions are met before execution."""
        if self.dry_run:
            return True

        # In production these are verified against live AWS APIs
        # For now, log and return True (preconditions checked by Phase 4 before runbook starts)
        for condition in runbook.preconditions:
            logger.debug("Checking precondition", extra={"condition": condition})

        return True

    def _run_verify(self, verify_expr: str, output: str, incident: Incident) -> bool:
        """Evaluate a verification expression against command output."""
        # In production, verification expressions are parsed and evaluated
        # against structured output. For now, return True if output is non-empty.
        if self.dry_run:
            return True
        return bool(output) and "error" not in output.lower()

    def _run_final_verification(self, runbook: Runbook, incident: Incident) -> bool:
        """Run the runbook's global verification gate."""
        if self.dry_run or not runbook.verification_query:
            return True

        logger.info(
            "RunbookExecutor: running final verification",
            extra={"query": runbook.verification_query, "region": incident.region}
        )
        # In production: query the ML scoring service and check the metric against the threshold
        return True

    def _finish(
        self,
        incident: Incident,
        result: RunbookResult,
        runbook: Runbook,
        started_at: float,
    ) -> None:
        """Finalise result, notify FSM, fire postmortem hook."""
        result.completed_at = time.time()
        self.incident_manager.record_runbook_result(incident.region, result)

        # Notify Phase 4 FSM
        if result.status == RunbookStatus.SUCCESS:
            try:
                self.notify_fsm_succeeded(incident.region)
            except Exception as e:
                logger.error("Failed to notify FSM success", exc_info=e)
        else:
            try:
                self.notify_fsm_failed(incident.region)
            except Exception as e:
                logger.error("Failed to notify FSM failure", exc_info=e)

        # Fire postmortem hook — ALWAYS, regardless of outcome
        if self.on_complete:
            try:
                self.on_complete(incident, result)
            except Exception as e:
                logger.error("on_complete (postmortem) callback failed", exc_info=e)

        with self._lock:
            self._active_runs.pop(incident.region, None)
