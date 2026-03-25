"""
notifier.py — SRE notification system.

Implements the NimbusNet notification matrix:

  Event                          Channel         Action Required?
  ─────────────────────────────  ──────────────  ────────────────
  Auto-resolved (any severity)   Slack FYI       No — informed only
  P2/P3 escalated                Slack #alerts   Review when available
  P1 escalated                   Slack + PD      Acknowledge within 15m
  P0 escalated                   Slack + PD      Acknowledge within 5m
  TTL breach (any severity)      PagerDuty       Immediate response

The critical rule: Slack FYI for auto-resolved, PagerDuty only for P0/P1 or TTL breach.
The SRE is never paged for something the system resolved on its own.

This is exactly how Google and Meta run SRE on-call.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional
import urllib.request
import urllib.error

from ..types import Incident, IncidentSeverity, Postmortem

logger = logging.getLogger(__name__)


@dataclass
class NotificationConfig:
    slack_webhook_url:      str = ""
    slack_alerts_channel:   str = "#nimbusnet-alerts"
    slack_fyi_channel:      str = "#nimbusnet-sre"
    pagerduty_routing_key:  str = ""
    pagerduty_api_url:      str = "https://events.pagerduty.com/v2/enqueue"
    dry_run:                bool = False


class Notifier:
    """
    Routes incident notifications to the correct channel.

    The notification matrix is enforced here — not by the caller.
    The caller says "incident opened" or "incident resolved";
    the Notifier decides which channel to use.
    """

    def __init__(self, cfg: NotificationConfig):
        self.cfg = cfg

    # ─── Incident Lifecycle Notifications ─────────────────────────────────────

    def on_incident_opened(self, incident: Incident) -> None:
        """
        Notify when an incident opens.
        P0/P1 → Slack #alerts (high-visibility)
        P2/P3 → Slack #sre (low-visibility)
        No PagerDuty yet — autonomous resolution may handle it.
        """
        channel = (
            self.cfg.slack_alerts_channel
            if incident.severity in (IncidentSeverity.P0, IncidentSeverity.P1)
            else self.cfg.slack_fyi_channel
        )

        msg = self._format_incident_opened(incident)
        self._slack(channel, msg)

        logger.info(
            "Notifier: incident opened notification sent",
            extra={"incident_id": incident.id, "channel": channel}
        )

    def on_incident_auto_resolved(self, incident: Incident, pm: Postmortem) -> None:
        """
        Slack FYI when autonomous resolution succeeds.
        NEVER pages PagerDuty — the system handled it without human help.
        """
        msg = self._format_auto_resolved(incident, pm)
        self._slack(self.cfg.slack_fyi_channel, msg)

        logger.info(
            "Notifier: auto-resolved FYI sent",
            extra={"incident_id": incident.id, "duration_s": round(incident.duration_s)}
        )

    def on_escalation(self, incident: Incident, reason: str) -> None:
        """
        Fire PagerDuty for P0/P1 or TTL breach.
        Also notify Slack #alerts.
        """
        # Slack alert
        msg = self._format_escalation(incident, reason)
        self._slack(self.cfg.slack_alerts_channel, msg)

        # PagerDuty — only for P0/P1 or explicit TTL breach
        should_page = (
            incident.severity in (IncidentSeverity.P0, IncidentSeverity.P1)
            or "TTL" in reason
            or "cold standby" in reason.lower()
        )

        if should_page:
            self._pagerduty(incident, reason)
            logger.warning(
                "Notifier: PagerDuty alert fired",
                extra={"incident_id": incident.id, "severity": incident.severity.value}
            )
        else:
            logger.info(
                "Notifier: Slack alert sent (no PagerDuty for this severity)",
                extra={"incident_id": incident.id}
            )

    def on_postmortem_ready(self, pm: Postmortem) -> None:
        """Notify when a postmortem requires human review (P0/P1 only)."""
        if not pm.requires_human_review:
            return

        msg = self._format_postmortem_review(pm)
        self._slack(self.cfg.slack_alerts_channel, msg)

    def on_runbook_step_update(self, incident_id: str, region: str, step: int, total: int) -> None:
        """Optional: post runbook progress to Slack (throttled to every 3 steps)."""
        if step % 3 != 0:
            return
        msg = {
            "text": (
                f":wrench: Runbook progress for `{region}` ({incident_id}): "
                f"step {step}/{total}"
            )
        }
        self._slack(self.cfg.slack_fyi_channel, json.dumps(msg))

    # ─── Message Formatters ────────────────────────────────────────────────────

    def _format_incident_opened(self, incident: Incident) -> str:
        severity_emoji = {"P0": ":red_circle:", "P1": ":orange_circle:", "P2": ":yellow_circle:", "P3": ":white_circle:"}
        emoji = severity_emoji.get(incident.severity.value, ":white_circle:")

        return json.dumps({
            "text": f"{emoji} *Incident {incident.id}* — {incident.severity.value} | {incident.region}",
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f"{emoji} {incident.severity.value} — {incident.region}"}
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Incident:*\n{incident.id}"},
                        {"type": "mrkdwn", "text": f"*Fault Type:*\n{incident.fault_type}"},
                        {"type": "mrkdwn", "text": f"*RTT:*\n{incident.rtt_ewma_us // 1000}ms"},
                        {"type": "mrkdwn", "text": f"*Retransmit Rate:*\n{incident.retransmit_rate:.1%}"},
                        {"type": "mrkdwn", "text": f"*IF Score:*\n{incident.if_score:.0%}"},
                        {"type": "mrkdwn", "text": f"*Status:*\nRunbook starting..."},
                    ]
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": ":robot_face: NimbusNet is attempting autonomous resolution. You will be paged only if needed."}]
                }
            ]
        })

    def _format_auto_resolved(self, incident: Incident, pm: Postmortem) -> str:
        duration = f"{incident.duration_s:.0f}s"
        return json.dumps({
            "text": f":white_check_mark: Auto-resolved: {incident.id} | {incident.region} | {duration}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":white_check_mark: *Auto-resolved* — {incident.id}\n"
                            f"Region `{incident.region}` | Duration: *{duration}* | "
                            f"Budget consumed: *{incident.error_budget_consumed_minutes:.1f}m*\n"
                            f"Runbook: `{incident.runbook_id}` "
                            f"({incident.runbook_steps_completed}/{incident.runbook_steps_total} steps)\n"
                            f"Postmortem: `{pm.id}` (auto-published, no review needed)"
                        )
                    }
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": ":information_source: *FYI only* — no action required."}]
                }
            ]
        })

    def _format_escalation(self, incident: Incident, reason: str) -> str:
        return json.dumps({
            "text": f":rotating_light: ESCALATION: {incident.id} | {incident.severity.value} | {incident.region}",
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f":rotating_light: SRE Escalation — {incident.severity.value}"}
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*Incident:* {incident.id}\n"
                            f"*Region:* `{incident.region}`\n"
                            f"*Reason:* {reason}\n"
                            f"*Fault type:* {incident.fault_type}\n"
                            f"*Duration so far:* {incident.duration_s:.0f}s\n"
                            f"*Runbook:* `{incident.runbook_id}` — "
                            f"{incident.runbook_steps_completed}/{incident.runbook_steps_total} steps completed"
                        )
                    }
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": ":bell: *PagerDuty alert has been fired.*"}
                }
            ]
        })

    def _format_postmortem_review(self, pm: Postmortem) -> str:
        return json.dumps({
            "text": f":memo: Postmortem requires review: {pm.id} | {pm.severity.value}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":memo: *Postmortem {pm.id}* requires SRE review\n"
                            f"Incident: `{pm.incident_id}` | Severity: {pm.severity.value}\n"
                            f"Region: `{pm.region}`\n\n"
                            f"*Required:* Fill in `contributing_factors` and `action_items` sections.\n"
                            f"Auto-populated: summary, timeline, root cause, impact, detection, resolution."
                        )
                    }
                }
            ]
        })

    # ─── Delivery ─────────────────────────────────────────────────────────────

    def _slack(self, channel: str, payload: str) -> None:
        if self.cfg.dry_run or not self.cfg.slack_webhook_url:
            logger.info("Notifier: [DRY RUN] Slack", extra={"channel": channel, "payload_len": len(payload)})
            return

        try:
            req = urllib.request.Request(
                self.cfg.slack_webhook_url,
                data=payload.encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception as e:
            logger.error("Notifier: Slack delivery failed", exc_info=e)

    def _pagerduty(self, incident: Incident, reason: str) -> None:
        """Fire a PagerDuty Events API v2 alert."""
        if self.cfg.dry_run or not self.cfg.pagerduty_routing_key:
            logger.info(
                "Notifier: [DRY RUN] PagerDuty",
                extra={"incident_id": incident.id, "severity": incident.severity.value}
            )
            return

        pd_severity = {
            "P0": "critical",
            "P1": "error",
            "P2": "warning",
            "P3": "info",
        }.get(incident.severity.value, "error")

        payload = json.dumps({
            "routing_key":  self.cfg.pagerduty_routing_key,
            "event_action": "trigger",
            "dedup_key":    incident.id,
            "payload": {
                "summary":   f"NimbusNet {incident.severity.value}: {incident.fault_type} in {incident.region}",
                "severity":  pd_severity,
                "source":    f"nimbusnet-sre/{incident.region}",
                "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(incident.detected_at_ns / 1e9)),
                "custom_details": {
                    "incident_id":      incident.id,
                    "region":           incident.region,
                    "fault_type":       incident.fault_type,
                    "reason":           reason,
                    "rtt_ms":           incident.rtt_ewma_us // 1000,
                    "retransmit_rate":  f"{incident.retransmit_rate:.1%}",
                    "runbook":          incident.runbook_id or "none",
                    "duration_s":       round(incident.duration_s),
                    "auto_resolved":    incident.was_auto_resolved,
                }
            }
        })

        try:
            req = urllib.request.Request(
                self.cfg.pagerduty_api_url,
                data=payload.encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
            logger.info("Notifier: PagerDuty alert sent", extra={"incident_id": incident.id})
        except Exception as e:
            logger.error("Notifier: PagerDuty delivery failed", exc_info=e)
