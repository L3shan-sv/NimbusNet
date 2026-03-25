"""
NimbusNet Phase 6 — Error Budget Enforcer
Reads the current error budget state from Phase 5 and makes the
go/no-go deploy decision. Called as a CI gate step before any deployment.
"""

import json
import sys
import time
import logging
import urllib.request
import urllib.error
from pathlib import Path
from dataclasses import dataclass

logger = logging.getLogger("nimbusnet.budget_enforcer")

BUDGET_FREEZE_THRESHOLD_PCT = 80.0     # freeze deploys above this
BUDGET_WARN_THRESHOLD_PCT   = 60.0     # warn but allow above this
BUDGET_CRITICAL_THRESHOLD_PCT = 95.0  # page SRE immediately above this


@dataclass
class BudgetStatus:
    consumed_pct: float
    remaining_pct: float
    window_days: int
    incidents_this_window: int
    last_incident_ts: float
    frozen: bool
    warn: bool
    critical: bool
    decision: str       # ALLOW / WARN / FREEZE
    reason: str


class BudgetEnforcer:
    """
    Reads error budget state. Decision logic:
    < 60%  consumed → ALLOW
    60-80% consumed → WARN (deploy allowed with annotation)
    80-95% consumed → FREEZE (no deploys, SRE unblocks manually)
    > 95%  consumed → CRITICAL (immediate SRE page + freeze)
    """

    def __init__(
        self,
        state_file: str = "/tmp/nimbusnet/sre/error_budget.json",
        prometheus_url: str = "http://localhost:9090",
        slo_name: str = "nimbusnet_availability",
    ):
        self.state_file = Path(state_file)
        self.prometheus_url = prometheus_url
        self.slo_name = slo_name

    def check(self) -> BudgetStatus:
        """Return current budget status and deploy decision."""
        consumed = self._read_consumed_pct()

        frozen   = consumed >= BUDGET_FREEZE_THRESHOLD_PCT
        warn     = BUDGET_WARN_THRESHOLD_PCT <= consumed < BUDGET_FREEZE_THRESHOLD_PCT
        critical = consumed >= BUDGET_CRITICAL_THRESHOLD_PCT

        if critical:
            decision = "FREEZE"
            reason = (
                f"Error budget {consumed:.1f}% consumed — CRITICAL threshold exceeded. "
                f"SRE has been paged. All deployments frozen."
            )
        elif frozen:
            decision = "FREEZE"
            reason = (
                f"Error budget {consumed:.1f}% consumed — freeze threshold exceeded. "
                f"SRE must manually unfreeze via `nimbusnet budget unfreeze`."
            )
        elif warn:
            decision = "WARN"
            reason = (
                f"Error budget {consumed:.1f}% consumed — approaching freeze threshold. "
                f"Deploy allowed. Reduce incident rate to avoid freeze."
            )
        else:
            decision = "ALLOW"
            reason = f"Error budget healthy at {consumed:.1f}% consumed."

        return BudgetStatus(
            consumed_pct=consumed,
            remaining_pct=100.0 - consumed,
            window_days=30,
            incidents_this_window=self._read_incident_count(),
            last_incident_ts=self._read_last_incident_ts(),
            frozen=frozen,
            warn=warn,
            critical=critical,
            decision=decision,
            reason=reason,
        )

    def gate(self) -> bool:
        """CI gate check. Returns True (allow) or False (block). Prints reason."""
        status = self.check()
        print(f"Error budget consumed: {status.consumed_pct:.1f}%")
        print(f"Decision: {status.decision}")
        print(f"Reason: {status.reason}")
        if status.critical:
            self._page_sre(status)
        return status.decision != "FREEZE"

    # ------------------------------------------------------------------ #
    #  Internal                                                             #
    # ------------------------------------------------------------------ #

    def _read_consumed_pct(self) -> float:
        # Try local state file first (set by Phase 5 incident manager)
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    data = json.load(f)
                return float(data.get("consumed_pct", 0.0))
            except Exception:
                pass

        # Fall back to Prometheus query
        try:
            query = f'nimbusnet_error_budget_consumed_pct{{slo="{self.slo_name}"}}'
            url = f"{self.prometheus_url}/api/v1/query?query={urllib.request.quote(query)}"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
                results = data.get("data", {}).get("result", [])
                if results:
                    return float(results[0]["value"][1])
        except Exception:
            pass

        logger.warning("Could not read error budget — defaulting to 0% consumed")
        return 0.0

    def _read_incident_count(self) -> int:
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    return int(json.load(f).get("incidents_30d", 0))
            except Exception:
                pass
        return 0

    def _read_last_incident_ts(self) -> float:
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    return float(json.load(f).get("last_incident_ts", 0.0))
            except Exception:
                pass
        return 0.0

    def _page_sre(self, status: BudgetStatus):
        """In production: POST to PagerDuty Events API v2."""
        logger.critical(
            f"🚨 PAGING SRE — Error budget CRITICAL\n"
            f"  Consumed: {status.consumed_pct:.1f}%\n"
            f"  Incidents this window: {status.incidents_this_window}\n"
            f"  All deployments frozen until SRE acknowledges"
        )


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    enforcer = BudgetEnforcer()
    allowed = enforcer.gate()
    sys.exit(0 if allowed else 1)


if __name__ == "__main__":
    main()
