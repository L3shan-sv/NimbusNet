"""
NimbusNet Phase 6 — tc qdisc Fault Injection Engine
Injects network faults at the Linux traffic control layer.
Every session simultaneously:
  1. Stresses the healing pipeline (chaos test)
  2. Generates labeled training data for the ML flywheel (data generation)
"""

import subprocess
import time
import json
import uuid
import logging
import threading
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("nimbusnet.injection")


class FaultType(str, Enum):
    LATENCY       = "latency"        # add fixed delay
    LATENCY_JITTER= "latency_jitter" # delay + jitter
    PACKET_LOSS   = "packet_loss"    # random drop %
    PACKET_CORRUPT= "packet_corrupt" # bit corruption
    PACKET_REORDER= "packet_reorder" # reorder %
    BANDWIDTH_CAP  = "bandwidth_cap" # limit throughput
    DUPLICATE      = "duplicate"     # duplicate packets
    COMBINED       = "combined"      # latency + loss (worst-case)


@dataclass
class FaultProfile:
    fault_type: FaultType
    interface: str          = "eth0"
    latency_ms: int         = 0
    jitter_ms: int          = 0
    loss_pct: float         = 0.0
    corrupt_pct: float      = 0.0
    reorder_pct: float      = 0.0
    bandwidth_kbps: int     = 0     # 0 = no cap
    duplicate_pct: float    = 0.0
    duration_seconds: int   = 60
    ramp_seconds: int       = 5     # gradual onset
    # flywheel metadata
    expected_severity: str  = "DEGRADED"   # NOMINAL/WARNING/DEGRADED/CRITICAL
    expected_fault_class: int = 2          # 0-3 matching XGBoost classes


# Canonical fault profiles matching bootstrap_generator.py fault types
FAULT_PROFILES: dict[str, FaultProfile] = {
    "high_latency": FaultProfile(
        fault_type=FaultType.LATENCY,
        latency_ms=200, duration_seconds=120,
        expected_severity="DEGRADED", expected_fault_class=1,
    ),
    "packet_loss_5pct": FaultProfile(
        fault_type=FaultType.PACKET_LOSS,
        loss_pct=5.0, duration_seconds=90,
        expected_severity="WARNING", expected_fault_class=0,
    ),
    "packet_loss_severe": FaultProfile(
        fault_type=FaultType.PACKET_LOSS,
        loss_pct=25.0, duration_seconds=60,
        expected_severity="CRITICAL", expected_fault_class=3,
    ),
    "jitter_burst": FaultProfile(
        fault_type=FaultType.LATENCY_JITTER,
        latency_ms=50, jitter_ms=100, duration_seconds=90,
        expected_severity="WARNING", expected_fault_class=1,
    ),
    "bandwidth_constrained": FaultProfile(
        fault_type=FaultType.BANDWIDTH_CAP,
        bandwidth_kbps=512, duration_seconds=120,
        expected_severity="DEGRADED", expected_fault_class=2,
    ),
    "combined_worst_case": FaultProfile(
        fault_type=FaultType.COMBINED,
        latency_ms=300, loss_pct=15.0, duration_seconds=60,
        expected_severity="CRITICAL", expected_fault_class=3,
    ),
    "packet_reorder": FaultProfile(
        fault_type=FaultType.PACKET_REORDER,
        reorder_pct=30.0, latency_ms=20, duration_seconds=90,
        expected_severity="WARNING", expected_fault_class=1,
    ),
}


@dataclass
class InjectionSession:
    session_id: str             = field(default_factory=lambda: str(uuid.uuid4())[:8])
    profile_name: str           = ""
    profile: Optional[FaultProfile] = None
    start_time: float           = 0.0
    end_time: float             = 0.0
    active: bool                = False
    telemetry_samples: list     = field(default_factory=list)
    healing_triggered: bool     = False
    healing_latency_ms: float   = 0.0
    resolution: str             = ""    # HEALED / TIMEOUT / MANUAL_CLEAR
    flywheel_label: dict        = field(default_factory=dict)


class QdiscInjector:
    """
    Wraps Linux tc qdisc commands. Requires CAP_NET_ADMIN.
    In CI: runs inside a network namespace with a veth pair.
    In production chaos: targets the real egress interface.
    """

    def __init__(
        self,
        interface: str = "eth0",
        flywheel_dir: str = "/tmp/nimbusnet/flywheel/chaos",
        dry_run: bool = False,
    ):
        self.interface = interface
        self.flywheel_dir = Path(flywheel_dir)
        self.flywheel_dir.mkdir(parents=True, exist_ok=True)
        self.dry_run = dry_run
        self._active_session: Optional[InjectionSession] = None
        self._lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ #
    #  Public API                                                           #
    # ------------------------------------------------------------------ #

    def inject(self, profile_name: str, profile: Optional[FaultProfile] = None) -> InjectionSession:
        """Start a fault injection session. Returns session for monitoring."""
        with self._lock:
            if self._active_session and self._active_session.active:
                raise RuntimeError(f"Session {self._active_session.session_id} already active — clear first")

            p = profile or FAULT_PROFILES.get(profile_name)
            if not p:
                raise ValueError(f"Unknown fault profile: {profile_name}. Available: {list(FAULT_PROFILES)}")

            session = InjectionSession(
                profile_name=profile_name,
                profile=p,
                start_time=time.time(),
                active=True,
            )
            self._active_session = session

        logger.info(f"[{session.session_id}] Injecting {profile_name} on {p.interface or self.interface}")
        self._apply_qdisc(p)
        self._start_monitor(session)
        return session

    def clear(self) -> Optional[InjectionSession]:
        """Remove all qdisc rules and finalize the session."""
        with self._lock:
            session = self._active_session
            if not session or not session.active:
                logger.warning("No active session to clear")
                return None
            session.active = False
            session.end_time = time.time()

        self._run_tc(f"tc qdisc del dev {self.interface} root", ignore_errors=True)
        logger.info(f"[{session.session_id}] Cleared qdisc rules")

        if session.resolution == "":
            session.resolution = "MANUAL_CLEAR"

        self._write_flywheel_label(session)
        return session

    def wait_for_healing(self, session: InjectionSession, timeout_s: int = 300) -> bool:
        """Block until the healing pipeline resolves or timeout. Returns True if healed."""
        deadline = session.start_time + timeout_s
        while time.time() < deadline:
            if session.healing_triggered and session.resolution in ("HEALED", "TIMEOUT"):
                return session.resolution == "HEALED"
            time.sleep(1)
        session.resolution = "TIMEOUT"
        return False

    # ------------------------------------------------------------------ #
    #  Internal                                                             #
    # ------------------------------------------------------------------ #

    def _apply_qdisc(self, p: FaultProfile):
        iface = p.interface or self.interface

        # Always start fresh
        self._run_tc(f"tc qdisc del dev {iface} root", ignore_errors=True)

        if p.fault_type == FaultType.BANDWIDTH_CAP and p.bandwidth_kbps > 0:
            # Token bucket filter for bandwidth cap
            self._run_tc(f"tc qdisc add dev {iface} root handle 1: tbf rate {p.bandwidth_kbps}kbit burst 32kbit latency 400ms")
            return

        # netem handles everything else
        cmd_parts = [f"tc qdisc add dev {iface} root netem"]

        if p.latency_ms > 0:
            cmd_parts.append(f"delay {p.latency_ms}ms")
            if p.jitter_ms > 0:
                cmd_parts.append(f"{p.jitter_ms}ms distribution normal")

        if p.loss_pct > 0:
            cmd_parts.append(f"loss {p.loss_pct}%")

        if p.corrupt_pct > 0:
            cmd_parts.append(f"corrupt {p.corrupt_pct}%")

        if p.reorder_pct > 0:
            # reorder requires a base delay
            base = max(p.latency_ms, 10)
            cmd_parts.append(f"delay {base}ms reorder {p.reorder_pct}%")

        if p.duplicate_pct > 0:
            cmd_parts.append(f"duplicate {p.duplicate_pct}%")

        if p.fault_type == FaultType.COMBINED:
            cmd_parts.append(f"delay {p.latency_ms}ms loss {p.loss_pct}%")

        self._run_tc(" ".join(cmd_parts))

    def _run_tc(self, cmd: str, ignore_errors: bool = False):
        if self.dry_run:
            logger.info(f"[DRY RUN] {cmd}")
            return
        result = subprocess.run(cmd.split(), capture_output=True, text=True)
        if result.returncode != 0 and not ignore_errors:
            raise RuntimeError(f"tc command failed: {result.stderr.strip()}")

    def _start_monitor(self, session: InjectionSession):
        def _monitor():
            deadline = session.start_time + session.profile.duration_seconds
            while time.time() < deadline and session.active:
                sample = self._collect_telemetry()
                session.telemetry_samples.append(sample)
                time.sleep(5)

            # Auto-clear when duration expires
            if session.active:
                session.resolution = "HEALED"
                session.healing_latency_ms = (time.time() - session.start_time) * 1000
                self.clear()

        self._monitor_thread = threading.Thread(target=_monitor, daemon=True)
        self._monitor_thread.start()

    def _collect_telemetry(self) -> dict:
        """Collect current network stats for flywheel labeling."""
        try:
            result = subprocess.run(
                ["tc", "-s", "qdisc", "show", "dev", self.interface],
                capture_output=True, text=True, timeout=2,
            )
            return {
                "timestamp": time.time(),
                "tc_stats": result.stdout[:500],
            }
        except Exception:
            return {"timestamp": time.time(), "tc_stats": "unavailable"}

    def _write_flywheel_label(self, session: InjectionSession):
        """Write labeled training record to the flywheel JSONL store."""
        if not session.profile:
            return

        label = {
            "session_id": session.session_id,
            "source": "chaos_qdisc",
            "profile_name": session.profile_name,
            "fault_type": session.profile.fault_type.value,
            "latency_ms": session.profile.latency_ms,
            "jitter_ms": session.profile.jitter_ms,
            "loss_pct": session.profile.loss_pct,
            "bandwidth_kbps": session.profile.bandwidth_kbps,
            "duration_seconds": session.profile.duration_seconds,
            "healing_triggered": session.healing_triggered,
            "healing_latency_ms": session.healing_latency_ms,
            "resolution": session.resolution,
            "severity_label": session.profile.expected_severity,
            "fault_class_label": session.profile.expected_fault_class,
            "sample_count": len(session.telemetry_samples),
            "timestamp": session.start_time,
        }
        session.flywheel_label = label

        out_path = self.flywheel_dir / f"chaos_{session.session_id}.jsonl"
        with open(out_path, "w") as f:
            f.write(json.dumps(label) + "\n")
        logger.info(f"[{session.session_id}] Flywheel label written → {out_path}")
