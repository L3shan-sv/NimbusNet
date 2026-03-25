"""
NimbusNet Phase 6 — GameDay Framework
Structured GameDay sessions for complex multi-fault scenarios.
GameDay sessions:
  1. Cover fault combinations synthetic bootstrap data cannot generate
  2. Generate the richest flywheel training records in the system
  3. Produce postmortems even on success (for runbook improvement)
  4. Always notify SRE (GameDay = planned chaos, SRE watches live)
"""

import json
import time
import logging
import random
from dataclasses import dataclass, field
from typing import Optional

from injection.qdisc_injector import QdiscInjector, FaultProfile, FaultType
from runner.chaos_runner import ChaosRunner, ChaosScenario, RunResult

logger = logging.getLogger("nimbusnet.gameday")


@dataclass
class GameDayScenario:
    """Multi-phase fault scenario — fault evolves over time."""
    name: str
    description: str
    phases: list[dict]          # [{fault_profile, duration_s, label}]
    expected_max_healing_ms: int = 60_000
    sre_watch_required: bool     = True


# ────────────────────────────────────────────────────────────────
# Standard GameDay scenarios (run weekly)
# These combine faults in ways synthetic data never generates
# ────────────────────────────────────────────────────────────────
GAMEDAY_SCENARIOS: list[GameDayScenario] = [
    GameDayScenario(
        name="cascading_degradation",
        description="Latency spike that evolves into packet loss — tests LSTM sequence detection",
        phases=[
            {"profile": FaultProfile(FaultType.LATENCY, latency_ms=100, duration_seconds=30), "label": "initial_spike"},
            {"profile": FaultProfile(FaultType.LATENCY_JITTER, latency_ms=150, jitter_ms=80, duration_seconds=30), "label": "instability"},
            {"profile": FaultProfile(FaultType.COMBINED, latency_ms=200, loss_pct=10.0, duration_seconds=60), "label": "full_degradation"},
        ],
        expected_max_healing_ms=90_000,
    ),
    GameDayScenario(
        name="bandwidth_starvation_recovery",
        description="Progressive bandwidth reduction to zero — tests bandit exploration floor",
        phases=[
            {"profile": FaultProfile(FaultType.BANDWIDTH_CAP, bandwidth_kbps=2048, duration_seconds=30), "label": "constrained"},
            {"profile": FaultProfile(FaultType.BANDWIDTH_CAP, bandwidth_kbps=256, duration_seconds=30), "label": "severe"},
            {"profile": FaultProfile(FaultType.BANDWIDTH_CAP, bandwidth_kbps=64, duration_seconds=30), "label": "near_zero"},
        ],
        expected_max_healing_ms=60_000,
    ),
    GameDayScenario(
        name="flapping_interface",
        description="Alternating healthy/degraded to test anti-flapping in FSM",
        phases=[
            {"profile": FaultProfile(FaultType.PACKET_LOSS, loss_pct=20.0, duration_seconds=15), "label": "fault_1"},
            {"profile": FaultProfile(FaultType.LATENCY, latency_ms=0, duration_seconds=10), "label": "recovery_window"},
            {"profile": FaultProfile(FaultType.PACKET_LOSS, loss_pct=25.0, duration_seconds=15), "label": "fault_2"},
            {"profile": FaultProfile(FaultType.LATENCY, latency_ms=0, duration_seconds=10), "label": "recovery_window_2"},
            {"profile": FaultProfile(FaultType.COMBINED, latency_ms=250, loss_pct=30.0, duration_seconds=30), "label": "final_failure"},
        ],
        expected_max_healing_ms=120_000,
    ),
    GameDayScenario(
        name="silent_corruption",
        description="Packet corruption without loss — tests Isolation Forest on subtle anomalies",
        phases=[
            {"profile": FaultProfile(FaultType.PACKET_CORRUPT, corrupt_pct=0.5, duration_seconds=60), "label": "subtle_corruption"},
            {"profile": FaultProfile(FaultType.PACKET_CORRUPT, corrupt_pct=5.0, duration_seconds=60), "label": "significant_corruption"},
        ],
        expected_max_healing_ms=90_000,
    ),
    GameDayScenario(
        name="dual_region_simultaneous",
        description="Simulates both regions degrading simultaneously — cold standby trigger test",
        phases=[
            # Phase 1 on default interface, Phase 2 on secondary (simulated)
            {"profile": FaultProfile(FaultType.COMBINED, latency_ms=300, loss_pct=20.0, duration_seconds=90), "label": "dual_critical"},
        ],
        expected_max_healing_ms=300_000,  # Cold standby provisioning takes 3-5min
        sre_watch_required=True,
    ),
]


@dataclass
class GameDayReport:
    session_name: str
    date: str
    facilitator: str
    observers: list[str]
    scenarios_run: list[str]
    scenarios_passed: int
    scenarios_failed: int
    total_fault_minutes: float
    unique_fault_combinations: int
    flywheel_records_generated: int
    healing_performance: dict
    runbook_observations: list[str]       # manual observations during session
    postmortem_triggers: list[str]        # scenarios requiring postmortem
    action_items: list[str]
    next_gameday_scenarios: list[str]     # discovered gaps to address
    timestamp: float = field(default_factory=time.time)


class GameDayOrchestrator:
    """
    Runs a structured GameDay session.
    Unlike CI runs, GameDay sessions:
    - Run multi-phase fault scenarios
    - Generate richer flywheel training data
    - Always notify SRE before start
    - Write a structured GameDay report postmortem
    """

    def __init__(
        self,
        interface: str = "eth0",
        flywheel_dir: str = "/tmp/nimbusnet/flywheel/chaos",
        results_dir: str = "/tmp/nimbusnet/chaos/gameday",
        dry_run: bool = False,
    ):
        self.injector = QdiscInjector(interface=interface, flywheel_dir=flywheel_dir, dry_run=dry_run)
        self.results_dir = __import__("pathlib").Path(results_dir)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.dry_run = dry_run

    def run_session(
        self,
        scenarios: Optional[list[GameDayScenario]] = None,
        facilitator: str = "nimbusnet-automation",
        observers: Optional[list[str]] = None,
    ) -> GameDayReport:
        scenarios = scenarios or GAMEDAY_SCENARIOS
        observers = observers or []

        logger.info(f"🎮 GameDay session starting — {len(scenarios)} scenarios, facilitator: {facilitator}")
        self._notify_sre_gameday_start(scenarios, facilitator, observers)

        results = []
        flywheel_count = 0
        fault_minutes = 0.0

        for scenario in scenarios:
            logger.info(f"▶ GameDay scenario: {scenario.name}")
            result = self._run_multiphase(scenario)
            results.append(result)
            flywheel_count += len(scenario.phases)
            fault_minutes += sum(p["profile"].duration_seconds for p in scenario.phases) / 60.0
            time.sleep(30)  # SRE observation window between scenarios

        passed = sum(1 for r in results if r.get("passed", False))
        failed = len(results) - passed

        report = GameDayReport(
            session_name=f"gameday_{int(time.time())}",
            date=time.strftime("%Y-%m-%d"),
            facilitator=facilitator,
            observers=observers,
            scenarios_run=[s.name for s in scenarios],
            scenarios_passed=passed,
            scenarios_failed=failed,
            total_fault_minutes=fault_minutes,
            unique_fault_combinations=sum(len(s.phases) for s in scenarios),
            flywheel_records_generated=flywheel_count,
            healing_performance={
                r["scenario"]: {
                    "healing_latency_ms": r.get("healing_latency_ms", 0),
                    "all_phases_healed": r.get("passed", False),
                }
                for r in results
            },
            runbook_observations=[
                "See phase-specific logs for detailed step timing",
                "Cold standby scenario requires manual review of Terraform output",
            ],
            postmortem_triggers=[s.name for s, r in zip(scenarios, results) if not r.get("passed", True)],
            action_items=self._generate_action_items(results),
            next_gameday_scenarios=self._suggest_next_scenarios(results),
        )

        self._write_report(report)
        self._notify_sre_gameday_complete(report)
        return report

    def _run_multiphase(self, scenario: GameDayScenario) -> dict:
        """Run each phase in sequence, track healing across phase transitions."""
        phase_results = []
        overall_start = time.time()
        healed_all = True

        for i, phase in enumerate(scenario.phases):
            profile: FaultProfile = phase["profile"]
            label: str = phase["label"]

            logger.info(f"  Phase {i+1}/{len(scenario.phases)}: {label}")
            try:
                session = self.injector.inject(f"gameday_{scenario.name}_{label}", profile)
                # Wait for the phase duration, then move to next phase
                time.sleep(profile.duration_seconds)
                self.injector.clear()
                phase_results.append({
                    "phase": label,
                    "duration_s": profile.duration_seconds,
                    "session_id": session.session_id,
                    "resolution": session.resolution,
                })
            except Exception as e:
                logger.error(f"Phase {label} failed: {e}")
                healed_all = False
                try:
                    self.injector.clear()
                except Exception:
                    pass

            # Brief gap between phases
            time.sleep(5)

        total_ms = (time.time() - overall_start) * 1000
        return {
            "scenario": scenario.name,
            "phases": phase_results,
            "healing_latency_ms": total_ms,
            "passed": healed_all and total_ms < scenario.expected_max_healing_ms,
        }

    def _generate_action_items(self, results: list[dict]) -> list[str]:
        items = []
        for r in results:
            if not r.get("passed"):
                items.append(f"Investigate {r['scenario']} — exceeded healing budget or phase failure")
        if not items:
            items.append("No action items — all scenarios within SLO")
        return items

    def _suggest_next_scenarios(self, results: list[dict]) -> list[str]:
        suggestions = []
        if any("flapping" in r["scenario"] for r in results):
            suggestions.append("bgp_route_oscillation — test control plane under flapping conditions")
        if any("dual_region" in r["scenario"] for r in results):
            suggestions.append("cold_standby_partial_failure — terraform provisioning with one AZ unavailable")
        suggestions.append("ml_model_staleness — inject fault after disabling flywheel to test degradation detection")
        return suggestions

    def _notify_sre_gameday_start(self, scenarios, facilitator, observers):
        """In production: POST to Slack webhook. Here: log."""
        logger.info(
            f"🔔 SRE NOTIFICATION — GameDay starting\n"
            f"  Facilitator: {facilitator}\n"
            f"  Observers: {observers}\n"
            f"  Scenarios: {[s.name for s in scenarios]}\n"
            f"  This is planned chaos — no PagerDuty pages will fire during this session"
        )

    def _notify_sre_gameday_complete(self, report: GameDayReport):
        logger.info(
            f"✅ SRE NOTIFICATION — GameDay complete\n"
            f"  Passed: {report.scenarios_passed}/{len(report.scenarios_run)}\n"
            f"  Fault minutes: {report.total_fault_minutes:.1f}\n"
            f"  Flywheel records: {report.flywheel_records_generated}\n"
            f"  Action items: {report.action_items}"
        )

    def _write_report(self, report: GameDayReport):
        import dataclasses
        path = self.results_dir / f"{report.session_name}.json"
        with open(path, "w") as f:
            json.dump(dataclasses.asdict(report), f, indent=2)
        logger.info(f"GameDay report written → {path}")
