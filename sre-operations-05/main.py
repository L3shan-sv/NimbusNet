#!/usr/bin/env python3
"""
main.py — NimbusNet SRE Operations Service entrypoint.
Phase 5: Runbooks, Postmortems, Escalation, Notifications.
"""

import logging
import os
import sys

import uvicorn
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("sre.main")


def main():
    config_path = os.environ.get("CONFIG_PATH", "/etc/nimbusnet/sre_config.yaml")

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    from sre.runbooks.incident_manager import IncidentManager
    from sre.runbooks.executor import RunbookExecutor
    from sre.postmortem.generator import PostmortemGenerator
    from sre.notifications.notifier import Notifier, NotificationConfig
    from sre.server.phase4_client import Phase4Client
    from sre.server.server import create_app

    data_dir     = cfg.get("data_dir", "/opt/nimbusnet/sre-data")
    phase4_url   = cfg.get("phase4_url", "http://nimbusnet-controlplane:9091")
    ml_url       = cfg.get("ml_url", "http://nimbusnet-ml-scoring:8001")

    phase4_client = Phase4Client(base_url=phase4_url)
    notifier      = Notifier(NotificationConfig(
        slack_webhook_url=os.environ.get("SLACK_WEBHOOK_URL", ""),
        pagerduty_routing_key=os.environ.get("PAGERDUTY_ROUTING_KEY", ""),
        dry_run=cfg.get("dry_run", False),
    ))

    postmortem_gen = PostmortemGenerator(
        store_dir=f"{data_dir}/postmortems",
        ml_scoring_url=ml_url,
    )

    incident_manager = IncidentManager(
        store_dir=f"{data_dir}/incidents",
        on_incident_opened=notifier.on_incident_opened,
    )

    executor = RunbookExecutor(
        incident_manager=incident_manager,
        notify_fsm_started=phase4_client.notify_runbook_started,
        notify_fsm_succeeded=phase4_client.notify_runbook_succeeded,
        notify_fsm_failed=phase4_client.notify_runbook_failed,
        on_complete=lambda incident, result: postmortem_gen.generate(incident, result),
        dry_run=cfg.get("dry_run", False),
    )

    app = create_app(
        incident_manager=incident_manager,
        executor=executor,
        postmortem_gen=postmortem_gen,
        notifier=notifier,
        phase4_client=phase4_client,
    )

    host = cfg.get("host", "0.0.0.0")
    port = int(cfg.get("port", 8002))

    logger.info(f"NimbusNet SRE Operations Service starting on {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    sys.exit(main())
