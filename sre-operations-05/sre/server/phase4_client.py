"""
phase4_client.py — HTTP client for Phase 4 control plane integration.

Sends runbook lifecycle notifications and manual override commands
to the Phase 4 control plane FSM.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)


class Phase4Client:
    """Simple HTTP client for Phase 4 control plane API calls."""

    def __init__(self, base_url: str = "http://nimbusnet-controlplane:9091", timeout_s: int = 5):
        self.base_url  = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def notify_runbook_started(self, region: str) -> None:
        self._post(f"/api/fsm/runbook-started", {"region": region})

    def notify_runbook_succeeded(self, region: str) -> None:
        self._post(f"/api/fsm/runbook-succeeded", {"region": region})

    def notify_runbook_failed(self, region: str) -> None:
        self._post(f"/api/fsm/runbook-failed", {"region": region})

    def notify_manual_override(self, region: str, target_state: str) -> None:
        self._post(f"/api/fsm/override", {"region": region, "target_state": target_state})

    def get_region_status(self) -> list[dict]:
        try:
            req = urllib.request.Request(f"{self.base_url}/status")
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read())
        except Exception as e:
            logger.error("Phase4Client: get_region_status failed", exc_info=e)
            return []

    def _post(self, path: str, payload: dict) -> None:
        try:
            data = json.dumps(payload).encode()
            req  = urllib.request.Request(
                f"{self.base_url}{path}",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout_s):
                pass
            logger.debug("Phase4Client: POST success", extra={"path": path})
        except Exception as e:
            logger.error(f"Phase4Client: POST {path} failed", exc_info=e)
            raise
