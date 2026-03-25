"""
NimbusNet Phase 7 — GameDay Session Controller
Provides real-time control over running GameDay sessions.
Commands: abort, pause, resume, status, skip-scenario
"""

import json
import time
import threading
import logging
import signal
import socket
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import urllib.request

logger = logging.getLogger("nimbusnet.gameday_ctrl")


class SessionState(str, Enum):
    IDLE    = "IDLE"
    RUNNING = "RUNNING"
    PAUSED  = "PAUSED"
    ABORTED = "ABORTED"
    COMPLETE = "COMPLETE"


@dataclass
class SessionControl:
    session_id: str
    state: SessionState = SessionState.IDLE
    current_scenario: str = ""
    current_phase: int = 0
    total_phases: int = 0
    scenarios_completed: list[str] = field(default_factory=list)
    abort_reason: str = ""
    pause_reason: str = ""
    start_time: float = 0.0
    last_update: float = field(default_factory=time.time)


class GameDayController:
    """
    Controls a running GameDay session via a shared state file.
    The GameDay orchestrator (Phase 6) polls this file every second
    and respects the control signals.

    CLI usage:
      python -m gameday_control.controller status
      python -m gameday_control.controller abort --reason "unexpected production incident"
      python -m gameday_control.controller pause --reason "SRE investigating alert"
      python -m gameday_control.controller resume
      python -m gameday_control.controller skip
    """

    CONTROL_FILE = Path("/tmp/nimbusnet/gameday/control.json")
    STATUS_FILE  = Path("/tmp/nimbusnet/gameday/status.json")

    def __init__(self):
        self.CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)

    # ────────────────────────────────────────────────────────────
    # Commands (called from CLI / GitHub Actions)
    # ────────────────────────────────────────────────────────────

    def abort(self, reason: str = "Manual abort") -> bool:
        """Abort the current GameDay session. Triggers qdisc cleanup immediately."""
        ctrl = self._read_control()
        if ctrl and ctrl.state not in (SessionState.RUNNING, SessionState.PAUSED):
            print(f"No active session to abort (state: {ctrl.state if ctrl else 'IDLE'})")
            return False
        self._write_signal({"command": "abort", "reason": reason, "ts": time.time()})
        print(f"✅ Abort signal sent — reason: {reason}")
        print("   GameDay orchestrator will clear qdisc rules and write postmortem within 5s")
        self._notify_sre(f"GameDay ABORTED — {reason}")
        return True

    def pause(self, reason: str = "Manual pause") -> bool:
        """Pause after current phase completes."""
        ctrl = self._read_control()
        if not ctrl or ctrl.state != SessionState.RUNNING:
            print(f"Session not running (state: {ctrl.state if ctrl else 'IDLE'})")
            return False
        self._write_signal({"command": "pause", "reason": reason, "ts": time.time()})
        print(f"⏸ Pause signal sent — will pause after current phase")
        print(f"   Reason: {reason}")
        return True

    def resume(self) -> bool:
        """Resume a paused session."""
        ctrl = self._read_control()
        if not ctrl or ctrl.state != SessionState.PAUSED:
            print(f"Session not paused (state: {ctrl.state if ctrl else 'IDLE'})")
            return False
        self._write_signal({"command": "resume", "ts": time.time()})
        print("▶ Resume signal sent")
        return True

    def skip(self) -> bool:
        """Skip the current scenario, move to the next."""
        ctrl = self._read_control()
        if not ctrl or ctrl.state != SessionState.RUNNING:
            print(f"Session not running — cannot skip")
            return False
        self._write_signal({"command": "skip", "ts": time.time()})
        print(f"⏭ Skip signal sent — skipping scenario: {ctrl.current_scenario}")
        return True

    def status(self) -> dict:
        """Print and return current session status."""
        ctrl = self._read_control()
        status_data = self._read_status()

        if not ctrl:
            print("No active GameDay session")
            return {"state": "IDLE"}

        elapsed = time.time() - ctrl.start_time if ctrl.start_time else 0
        print(f"\n{'='*50}")
        print(f"NimbusNet GameDay Session: {ctrl.session_id}")
        print(f"{'='*50}")
        print(f"State:            {ctrl.state.value}")
        print(f"Current scenario: {ctrl.current_scenario or 'N/A'}")
        print(f"Phase:            {ctrl.current_phase}/{ctrl.total_phases}")
        print(f"Completed:        {ctrl.scenarios_completed}")
        print(f"Elapsed:          {elapsed:.0f}s")
        if status_data:
            print(f"Flywheel records: {status_data.get('flywheel_records', 0)}")
        print()
        return {"state": ctrl.state.value, "scenario": ctrl.current_scenario}

    # ────────────────────────────────────────────────────────────
    # Session registration (called by GameDay orchestrator)
    # ────────────────────────────────────────────────────────────

    def register_session(self, session_id: str, total_scenarios: int) -> "SessionHandle":
        """Called by GameDay orchestrator at session start. Returns a handle."""
        ctrl = SessionControl(
            session_id=session_id,
            state=SessionState.RUNNING,
            start_time=time.time(),
        )
        self._write_control(ctrl)
        return SessionHandle(ctrl, self)

    # ────────────────────────────────────────────────────────────
    # Internal
    # ────────────────────────────────────────────────────────────

    def _write_signal(self, signal_dict: dict):
        signal_path = Path("/tmp/nimbusnet/gameday/signal.json")
        with open(signal_path, "w") as f:
            json.dump(signal_dict, f)

    def read_signal(self) -> Optional[dict]:
        """Read and clear the pending control signal."""
        signal_path = Path("/tmp/nimbusnet/gameday/signal.json")
        if not signal_path.exists():
            return None
        try:
            with open(signal_path) as f:
                sig = json.load(f)
            signal_path.unlink()
            return sig
        except Exception:
            return None

    def _write_control(self, ctrl: SessionControl):
        with open(self.CONTROL_FILE, "w") as f:
            json.dump({
                "session_id": ctrl.session_id,
                "state": ctrl.state.value,
                "current_scenario": ctrl.current_scenario,
                "current_phase": ctrl.current_phase,
                "total_phases": ctrl.total_phases,
                "scenarios_completed": ctrl.scenarios_completed,
                "abort_reason": ctrl.abort_reason,
                "start_time": ctrl.start_time,
                "last_update": time.time(),
            }, f, indent=2)

    def _read_control(self) -> Optional[SessionControl]:
        if not self.CONTROL_FILE.exists():
            return None
        try:
            with open(self.CONTROL_FILE) as f:
                d = json.load(f)
            ctrl = SessionControl(session_id=d["session_id"])
            ctrl.state = SessionState(d["state"])
            ctrl.current_scenario = d.get("current_scenario", "")
            ctrl.current_phase = d.get("current_phase", 0)
            ctrl.total_phases = d.get("total_phases", 0)
            ctrl.scenarios_completed = d.get("scenarios_completed", [])
            ctrl.start_time = d.get("start_time", 0.0)
            return ctrl
        except Exception:
            return None

    def _read_status(self) -> Optional[dict]:
        if not self.STATUS_FILE.exists():
            return None
        try:
            with open(self.STATUS_FILE) as f:
                return json.load(f)
        except Exception:
            return None

    def _notify_sre(self, message: str):
        webhook_url = __import__("os").getenv("SLACK_WEBHOOK_URL")
        if not webhook_url:
            logger.info(f"SRE notification (no webhook configured): {message}")
            return
        try:
            payload = json.dumps({"text": f"🚨 *NimbusNet GameDay*: {message}"}).encode()
            req = urllib.request.Request(webhook_url, data=payload,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            logger.warning(f"Slack notification failed: {e}")


class SessionHandle:
    """
    Held by the GameDay orchestrator during a session.
    Provides scenario-level update methods and signal checking.
    """

    def __init__(self, ctrl: SessionControl, controller: GameDayController):
        self._ctrl = ctrl
        self._controller = controller

    def update(self, scenario: str, phase: int, total_phases: int):
        self._ctrl.current_scenario = scenario
        self._ctrl.current_phase = phase
        self._ctrl.total_phases = total_phases
        self._controller._write_control(self._ctrl)

    def complete_scenario(self, scenario: str):
        self._ctrl.scenarios_completed.append(scenario)
        self._ctrl.current_scenario = ""
        self._controller._write_control(self._ctrl)

    def check_signal(self) -> Optional[str]:
        """Returns command string if a signal is pending, else None."""
        sig = self._controller.read_signal()
        if not sig:
            return None
        cmd = sig.get("command")
        if cmd == "abort":
            self._ctrl.state = SessionState.ABORTED
            self._ctrl.abort_reason = sig.get("reason", "")
            self._controller._write_control(self._ctrl)
        elif cmd == "pause":
            self._ctrl.state = SessionState.PAUSED
            self._ctrl.pause_reason = sig.get("reason", "")
            self._controller._write_control(self._ctrl)
        elif cmd == "resume":
            self._ctrl.state = SessionState.RUNNING
            self._controller._write_control(self._ctrl)
        return cmd

    def wait_if_paused(self, poll_s: float = 1.0, max_wait_s: float = 3600.0):
        """Block while paused. Returns when resumed or aborted."""
        deadline = time.time() + max_wait_s
        while self._ctrl.state == SessionState.PAUSED and time.time() < deadline:
            sig = self.check_signal()
            if sig in ("resume", "abort"):
                break
            time.sleep(poll_s)

    def finish(self):
        self._ctrl.state = SessionState.COMPLETE
        self._controller._write_control(self._ctrl)


def main():
    import argparse, sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="NimbusNet GameDay Session Controller")
    subparsers = parser.add_subparsers(dest="command")

    abort_p = subparsers.add_parser("abort")
    abort_p.add_argument("--reason", default="Manual abort")

    pause_p = subparsers.add_parser("pause")
    pause_p.add_argument("--reason", default="Manual pause")

    subparsers.add_parser("resume")
    subparsers.add_parser("skip")
    subparsers.add_parser("status")

    args = parser.parse_args()
    ctrl = GameDayController()

    if args.command == "abort":
        success = ctrl.abort(args.reason)
        sys.exit(0 if success else 1)
    elif args.command == "pause":
        success = ctrl.pause(args.reason)
        sys.exit(0 if success else 1)
    elif args.command == "resume":
        success = ctrl.resume()
        sys.exit(0 if success else 1)
    elif args.command == "skip":
        success = ctrl.skip()
        sys.exit(0 if success else 1)
    elif args.command == "status":
        ctrl.status()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
