"""
NimbusNet Phase 6 — Chaos Runner
Orchestrates fault injection → healing measurement → CI gate evaluation.
Called by GitHub Actions on every PR merge.
"""

import time
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from injection.qdisc_injector import QdiscInjector, FaultProfile, InjectionSession, FAULT_PROFILES

logger = logging.getLogger("nimbusnet.chaos_runner")


# ────────────────────────────────────────────────────────────────
# Gate thresholds — these are the merge-blocking contracts
# ────────────────────────────────────────────────────────────────
HEALING_LATENCY_P99_THRESHOLD_MS = 30_000   # 30s — regression blocks merge
HEALING_LATENCY_REGRESSION_MS    = 200      # 200ms worse than baseline = regression
MIN_HEAL_RATE                    = 0.95     # 95% of sessions must auto-resolve
MAX_ERROR_BUDGET_CONSUMED_PCT    = 80.0     # freeze deploys above 80%


@dataclass
class ChaosScenario:
    name: str
    profile_name: str
    healing_timeout_s: int = 120
    expected_heal: bool    = True  # False for runbook-only scenarios
    weight: float          = 1.0   # for GameDay weighted selection


@dataclass
class RunResult:
    scenario: ChaosScenario
    session: InjectionSession
    healed: bool
    healing_latency_ms: float
    baseline_latency_ms: float   = 0.0
    regression_ms: float         = 0.0
    passed: bool                 = True
    fail_reason: str             = ""


@dataclass
class SuiteResult:
    suite_name: str
    scenarios_run: int           = 0
    scenarios_passed: int        = 0
    scenarios_failed: int        = 0
    heal_rate: float             = 0.0
    p50_latency_ms: float        = 0.0
    p99_latency_ms: float        = 0.0
    max_regression_ms: float     = 0.0
    error_budget_consumed_pct: float = 0.0
    gate_passed: bool            = False
    gate_failures: list[str]     = field(default_factory=list)
    run_results: list[RunResult] = field(default_factory=list)
    flywheel_records: int        = 0
    timestamp: float             = field(default_factory=time.time)


# ────────────────────────────────────────────────────────────────
# Standard CI scenario suite (runs on every PR merge)
# ────────────────────────────────────────────────────────────────
CI_SCENARIOS: list[ChaosScenario] = [
    ChaosScenario("high_latency_heal",     "high_latency",        healing_timeout_s=90),
    ChaosScenario("packet_loss_5pct_heal", "packet_loss_5pct",    healing_timeout_s=60),
    ChaosScenario("jitter_burst_heal",     "jitter_burst",        healing_timeout_s=75),
    ChaosScenario("bandwidth_cap_heal",    "bandwidth_constrained",healing_timeout_s=90),
    ChaosScenario("combined_worst_case",   "combined_worst_case", healing_timeout_s=120),
]

# Extended GameDay suite (weekly scheduled run)
GAMEDAY_SCENARIOS: list[ChaosScenario] = CI_SCENARIOS + [
    ChaosScenario("severe_loss_escalation","packet_loss_severe",  healing_timeout_s=60, expected_heal=False),
    ChaosScenario("reorder_chaos",         "packet_reorder",      healing_timeout_s=75),
]


class ChaosRunner:
    """
    Runs chaos scenarios in sequence, measures healing latency,
    writes flywheel labels, and evaluates CI gate pass/fail.
    """

    def __init__(
        self,
        interface: str = "eth0",
        baseline_store: str = "/tmp/nimbusnet/chaos/baseline.json",
        flywheel_dir: str = "/tmp/nimbusnet/flywheel/chaos",
        dry_run: bool = False,
        results_dir: str = "/tmp/nimbusnet/chaos/results",
    ):
        self.injector = QdiscInjector(interface=interface, flywheel_dir=flywheel_dir, dry_run=dry_run)
        self.baseline_path = Path(baseline_store)
        self.results_dir = Path(results_dir)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.dry_run = dry_run
        self._baseline: dict[str, float] = self._load_baseline()

    # ------------------------------------------------------------------ #
    #  Public API                                                           #
    # ------------------------------------------------------------------ #

    def run_ci_suite(self) -> SuiteResult:
        return self._run_suite("ci", CI_SCENARIOS)

    def run_gameday_suite(self) -> SuiteResult:
        return self._run_suite("gameday", GAMEDAY_SCENARIOS)

    def run_single(self, scenario_name: str, timeout_s: int = 120) -> RunResult:
        profile_name = scenario_name
        if profile_name not in FAULT_PROFILES:
            raise ValueError(f"Unknown profile: {profile_name}")
        scenario = ChaosScenario(f"adhoc_{scenario_name}", profile_name, healing_timeout_s=timeout_s)
        return self._run_scenario(scenario)

    def update_baseline(self, suite_result: SuiteResult):
        """Update baseline latencies after a clean main-branch run."""
        new_baseline = {}
        for rr in suite_result.run_results:
            if rr.healed and rr.healing_latency_ms > 0:
                new_baseline[rr.scenario.profile_name] = rr.healing_latency_ms
        self.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.baseline_path, "w") as f:
            json.dump(new_baseline, f, indent=2)
        self._baseline = new_baseline
        logger.info(f"Baseline updated: {new_baseline}")

    # ------------------------------------------------------------------ #
    #  Internal                                                             #
    # ------------------------------------------------------------------ #

    def _run_suite(self, suite_name: str, scenarios: list[ChaosScenario]) -> SuiteResult:
        result = SuiteResult(suite_name=suite_name, scenarios_run=len(scenarios))
        latencies: list[float] = []

        for scenario in scenarios:
            logger.info(f"▶ Running scenario: {scenario.name}")
            rr = self._run_scenario(scenario)
            result.run_results.append(rr)
            result.scenarios_run += 0  # already counted above

            if rr.passed:
                result.scenarios_passed += 1
            else:
                result.scenarios_failed += 1
                result.gate_failures.append(f"{scenario.name}: {rr.fail_reason}")

            if rr.healed:
                latencies.append(rr.healing_latency_ms)

            result.flywheel_records += 1

            # Cool-down between scenarios
            time.sleep(10)

        # Compute aggregate stats
        result.scenarios_run = len(scenarios)
        if latencies:
            latencies_sorted = sorted(latencies)
            n = len(latencies_sorted)
            result.p50_latency_ms = latencies_sorted[n // 2]
            result.p99_latency_ms = latencies_sorted[int(n * 0.99)]

        healed_count = sum(1 for rr in result.run_results if rr.healed and rr.scenario.expected_heal)
        expected_heal_count = sum(1 for s in scenarios if s.expected_heal)
        result.heal_rate = healed_count / expected_heal_count if expected_heal_count > 0 else 1.0

        regressions = [rr.regression_ms for rr in result.run_results if rr.regression_ms > 0]
        result.max_regression_ms = max(regressions) if regressions else 0.0

        # Simulate error budget (real impl reads from Phase 5 incident manager)
        result.error_budget_consumed_pct = self._fetch_error_budget_consumed()

        # Gate evaluation
        result.gate_passed, result.gate_failures = self._evaluate_gate(result)

        self._write_suite_result(result)
        return result

    def _run_scenario(self, scenario: ChaosScenario) -> RunResult:
        baseline = self._baseline.get(scenario.profile_name, 0.0)

        try:
            session = self.injector.inject(scenario.profile_name)
            healed = self.injector.wait_for_healing(session, timeout_s=scenario.healing_timeout_s)
            healing_latency = session.healing_latency_ms

            regression = max(0.0, healing_latency - baseline - HEALING_LATENCY_REGRESSION_MS)

            rr = RunResult(
                scenario=scenario,
                session=session,
                healed=healed,
                healing_latency_ms=healing_latency,
                baseline_latency_ms=baseline,
                regression_ms=regression,
            )

            # Gate check for this individual scenario
            if scenario.expected_heal and not healed:
                rr.passed = False
                rr.fail_reason = f"Expected auto-heal but timed out after {scenario.healing_timeout_s}s"
            elif regression > HEALING_LATENCY_REGRESSION_MS:
                rr.passed = False
                rr.fail_reason = (
                    f"Healing latency regression {regression:.0f}ms > "
                    f"{HEALING_LATENCY_REGRESSION_MS}ms threshold"
                )

            return rr

        except Exception as e:
            logger.error(f"Scenario {scenario.name} crashed: {e}")
            dummy_session = InjectionSession(profile_name=scenario.profile_name, resolution="ERROR")
            return RunResult(
                scenario=scenario,
                session=dummy_session,
                healed=False,
                healing_latency_ms=0.0,
                passed=False,
                fail_reason=f"Exception: {e}",
            )
        finally:
            # Always clean up — never leave qdisc rules in place
            try:
                self.injector.clear()
            except Exception:
                pass

    def _evaluate_gate(self, result: SuiteResult) -> tuple[bool, list[str]]:
        failures = list(result.gate_failures)  # copy scenario-level failures

        if result.p99_latency_ms > HEALING_LATENCY_P99_THRESHOLD_MS:
            failures.append(
                f"P99 healing latency {result.p99_latency_ms:.0f}ms > "
                f"{HEALING_LATENCY_P99_THRESHOLD_MS}ms threshold"
            )

        if result.heal_rate < MIN_HEAL_RATE:
            failures.append(
                f"Heal rate {result.heal_rate:.2%} < {MIN_HEAL_RATE:.2%} minimum"
            )

        if result.error_budget_consumed_pct >= MAX_ERROR_BUDGET_CONSUMED_PCT:
            failures.append(
                f"Error budget {result.error_budget_consumed_pct:.1f}% consumed — "
                f"deploy frozen (threshold {MAX_ERROR_BUDGET_CONSUMED_PCT}%)"
            )

        return len(failures) == 0, failures

    def _fetch_error_budget_consumed(self) -> float:
        """Read from Phase 5 incident manager state. Falls back to 0 in CI."""
        state_path = Path("/tmp/nimbusnet/sre/error_budget.json")
        if state_path.exists():
            try:
                with open(state_path) as f:
                    data = json.load(f)
                return float(data.get("consumed_pct", 0.0))
            except Exception:
                pass
        return 0.0

    def _load_baseline(self) -> dict[str, float]:
        if self.baseline_path.exists():
            try:
                with open(self.baseline_path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _write_suite_result(self, result: SuiteResult):
        path = self.results_dir / f"suite_{result.suite_name}_{int(result.timestamp)}.json"
        with open(path, "w") as f:
            # Manual serialize — dataclasses with nested dataclasses need help
            json.dump(self._serialize_suite(result), f, indent=2)
        logger.info(f"Suite result written → {path}")

    def _serialize_suite(self, result: SuiteResult) -> dict:
        d = {
            "suite_name": result.suite_name,
            "gate_passed": result.gate_passed,
            "gate_failures": result.gate_failures,
            "scenarios_run": result.scenarios_run,
            "scenarios_passed": result.scenarios_passed,
            "scenarios_failed": result.scenarios_failed,
            "heal_rate": result.heal_rate,
            "p50_latency_ms": result.p50_latency_ms,
            "p99_latency_ms": result.p99_latency_ms,
            "max_regression_ms": result.max_regression_ms,
            "error_budget_consumed_pct": result.error_budget_consumed_pct,
            "flywheel_records": result.flywheel_records,
            "timestamp": result.timestamp,
            "run_results": [
                {
                    "scenario": rr.scenario.name,
                    "profile": rr.scenario.profile_name,
                    "healed": rr.healed,
                    "healing_latency_ms": rr.healing_latency_ms,
                    "baseline_latency_ms": rr.baseline_latency_ms,
                    "regression_ms": rr.regression_ms,
                    "passed": rr.passed,
                    "fail_reason": rr.fail_reason,
                    "resolution": rr.session.resolution,
                }
                for rr in result.run_results
            ],
        }
        return d


# ────────────────────────────────────────────────────────────────
# CLI entrypoint for GitHub Actions
# ────────────────────────────────────────────────────────────────
def main():
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="NimbusNet Chaos Runner")
    parser.add_argument("--suite", choices=["ci", "gameday", "single"], default="ci")
    parser.add_argument("--scenario", help="Profile name for --suite single")
    parser.add_argument("--interface", default="eth0")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()

    runner = ChaosRunner(interface=args.interface, dry_run=args.dry_run)

    if args.suite == "ci":
        result = runner.run_ci_suite()
    elif args.suite == "gameday":
        result = runner.run_gameday_suite()
    elif args.suite == "single":
        if not args.scenario:
            print("ERROR: --scenario required for --suite single", file=sys.stderr)
            sys.exit(1)
        rr = runner.run_single(args.scenario)
        result_dict = {
            "gate_passed": rr.passed,
            "healed": rr.healed,
            "healing_latency_ms": rr.healing_latency_ms,
            "fail_reason": rr.fail_reason,
        }
        print(json.dumps(result_dict, indent=2))
        sys.exit(0 if rr.passed else 1)

    # Print summary
    print(f"\n{'='*60}")
    print(f"NimbusNet Chaos Gate — {result.suite_name.upper()}")
    print(f"{'='*60}")
    print(f"Gate passed:          {'✅ YES' if result.gate_passed else '❌ NO'}")
    print(f"Scenarios:            {result.scenarios_passed}/{result.scenarios_run} passed")
    print(f"Heal rate:            {result.heal_rate:.1%}")
    print(f"P50 healing latency:  {result.p50_latency_ms:.0f}ms")
    print(f"P99 healing latency:  {result.p99_latency_ms:.0f}ms")
    print(f"Max regression:       {result.max_regression_ms:.0f}ms")
    print(f"Error budget used:    {result.error_budget_consumed_pct:.1f}%")
    print(f"Flywheel records:     {result.flywheel_records}")

    if result.gate_failures:
        print(f"\nGate failures:")
        for f in result.gate_failures:
            print(f"  ✗ {f}")

    if args.update_baseline and result.gate_passed:
        runner.update_baseline(result)
        print("\nBaseline updated ✓")

    sys.exit(0 if result.gate_passed else 1)


if __name__ == "__main__":
    main()
