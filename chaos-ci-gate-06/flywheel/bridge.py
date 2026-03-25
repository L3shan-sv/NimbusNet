"""
NimbusNet Phase 6 — Chaos→ML Flywheel Bridge
Watches the chaos JSONL output directory and pushes labeled records
to the Phase 3 ML flywheel /flywheel/ingest endpoint.
One chaos session = one labeled training record, zero manual work.
"""

import json
import time
import logging
import threading
import hashlib
from pathlib import Path
from typing import Optional
import urllib.request
import urllib.error

logger = logging.getLogger("nimbusnet.flywheel_bridge")


class FlywheelBridge:
    """
    Watches /tmp/nimbusnet/flywheel/chaos/ for new JSONL files.
    For each new file, transforms the chaos session record into the
    format expected by Phase 3 scoring_pipeline → flywheel.ingest().
    Then POSTs to the ML serving layer.
    """

    def __init__(
        self,
        watch_dir: str = "/tmp/nimbusnet/flywheel/chaos",
        ml_serving_url: str = "http://localhost:8080",
        poll_interval_s: float = 10.0,
        processed_log: str = "/tmp/nimbusnet/flywheel/processed.jsonl",
    ):
        self.watch_dir = Path(watch_dir)
        self.watch_dir.mkdir(parents=True, exist_ok=True)
        self.ml_url = ml_serving_url.rstrip("/")
        self.poll_interval = poll_interval_s
        self.processed_log = Path(processed_log)
        self._processed_ids: set[str] = self._load_processed()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ #
    #  Public API                                                           #
    # ------------------------------------------------------------------ #

    def start(self):
        """Start background polling thread."""
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info(f"FlywheelBridge started — watching {self.watch_dir}")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def ingest_file(self, path: Path) -> bool:
        """Ingest a single chaos JSONL file. Returns True on success."""
        try:
            with open(path) as f:
                raw = json.loads(f.read().strip())
        except Exception as e:
            logger.error(f"Failed to read {path}: {e}")
            return False

        record_id = raw.get("session_id", self._file_hash(path))
        if record_id in self._processed_ids:
            return True  # already ingested

        payload = self._transform(raw)
        if payload is None:
            return False

        success = self._post_to_ml(payload)
        if success:
            self._mark_processed(record_id, raw)
        return success

    # ------------------------------------------------------------------ #
    #  Internal                                                             #
    # ------------------------------------------------------------------ #

    def _poll_loop(self):
        while self._running:
            for jsonl_file in sorted(self.watch_dir.glob("chaos_*.jsonl")):
                self.ingest_file(jsonl_file)
            time.sleep(self.poll_interval)

    def _transform(self, raw: dict) -> Optional[dict]:
        """
        Convert chaos session record → Phase 3 flywheel.ingest() format.
        Mirrors the bootstrap_generator.py record structure.
        """
        if not raw.get("resolution") in ("HEALED", "MANUAL_CLEAR", "TIMEOUT"):
            logger.debug(f"Skipping {raw.get('session_id')} — resolution not final")
            return None

        # Map chaos fault_type → XGBoost fault_class (must match training labels)
        fault_class_map = {
            "latency": 1,
            "latency_jitter": 1,
            "packet_loss": 0 if raw.get("loss_pct", 0) < 10 else 3,
            "packet_corrupt": 2,
            "packet_reorder": 1,
            "bandwidth_cap": 2,
            "duplicate": 0,
            "combined": 3,
        }

        fault_type = raw.get("fault_type", "latency")
        fault_class = raw.get("fault_class_label") or fault_class_map.get(fault_type, 0)

        severity_map = {"NOMINAL": 0, "WARNING": 1, "DEGRADED": 2, "CRITICAL": 3}
        severity = severity_map.get(raw.get("severity_label", "DEGRADED"), 2)

        return {
            "source": "chaos_qdisc",
            "session_id": raw.get("session_id"),
            "timestamp": raw.get("timestamp", time.time()),
            "features": {
                "rtt_mean_ms": float(raw.get("latency_ms", 0)),
                "rtt_jitter_ms": float(raw.get("jitter_ms", 0)),
                "packet_loss_rate": float(raw.get("loss_pct", 0)) / 100.0,
                "retransmit_rate": float(raw.get("loss_pct", 0)) / 100.0 * 0.8,
                "bandwidth_utilization": 1.0 if raw.get("bandwidth_kbps", 0) > 0 else 0.3,
                "connection_error_rate": float(raw.get("loss_pct", 0)) / 200.0,
                "anomaly_score": min(100, int(raw.get("latency_ms", 0) / 5) + int(raw.get("loss_pct", 0) * 3)),
                "healing_latency_ms": float(raw.get("healing_latency_ms", 0)),
                "auto_healed": 1 if raw.get("resolution") == "HEALED" else 0,
            },
            "labels": {
                "fault_class": fault_class,
                "severity": severity,
                "fault_type": fault_type,
                "healed": raw.get("healing_triggered", False),
            },
            "metadata": {
                "profile_name": raw.get("profile_name"),
                "duration_seconds": raw.get("duration_seconds"),
                "sample_count": raw.get("sample_count", 0),
            },
        }

    def _post_to_ml(self, payload: dict) -> bool:
        """POST labeled record to Phase 3 flywheel ingest endpoint."""
        url = f"{self.ml_url}/flywheel/ingest"
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    logger.info(f"Ingested session {payload['session_id']} → ML flywheel")
                    return True
                logger.warning(f"ML endpoint returned {resp.status}")
                return False
        except urllib.error.URLError as e:
            logger.warning(f"ML endpoint unreachable ({e}) — will retry next poll")
            return False

    def _mark_processed(self, record_id: str, raw: dict):
        self._processed_ids.add(record_id)
        with open(self.processed_log, "a") as f:
            f.write(json.dumps({"id": record_id, "ts": time.time(), "profile": raw.get("profile_name")}) + "\n")

    def _load_processed(self) -> set[str]:
        if not self.processed_log.exists():
            return set()
        ids = set()
        try:
            with open(self.processed_log) as f:
                for line in f:
                    rec = json.loads(line.strip())
                    ids.add(rec["id"])
        except Exception:
            pass
        return ids

    @staticmethod
    def _file_hash(path: Path) -> str:
        return hashlib.md5(path.name.encode()).hexdigest()[:8]
