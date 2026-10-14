"""Composition root: builds and wires all IMF components."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .config import Settings
from .db import Database, seed
from .ingestion.campaigns import CampaignService
from .ingestion.pdgs_client import PdgsClient
from .ingestion.poller import Poller
from .models import Parameter
from .monitoring.notifier import Notifier
from .monitoring.rules import RuleEngine
from .processing.analyses import AnalysisService
from .processing.pipeline import Pipeline
from .processing.registry import configure
from .reporting.report import ReportService
from .timeutil import utcnow

log = logging.getLogger(__name__)


class Services:
    def __init__(self, settings: Settings, start_scheduler: bool = True):
        self.settings = settings
        Parameter.nominal_baseline = settings.pdgs.baseline
        configure(settings.processing.plugin_modules, settings.processing.external_tools)
        self.db = Database(settings.database.url)
        self.db.migrate(settings.pdgs.baseline)
        seed(self.db, settings)
        self.client = PdgsClient(settings.pdgs.base_url, settings.pdgs.product_types[0].type,
                                 settings.pdgs.timeout_s, settings.pdgs.retries)
        self.notifier = Notifier(settings.notifications, settings.mission.name)
        self.pipeline = Pipeline(settings.characterisation)
        self.rules = RuleEngine(settings)
        self.poller = Poller(self.db, self.client, self.pipeline, self.notifier, settings, self.rules)
        self.campaigns = CampaignService(self.db, self.poller)
        self.analyses = AnalysisService(self.db, settings, self.rules, self.notifier)
        self.reports = ReportService(self.db, settings)
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self._start_scheduler = start_scheduler

    def start(self) -> None:
        if not self._start_scheduler:
            return
        self.scheduler.add_job(self.poller.run_once, "interval", id="poll", name="Poll PDGS for new products",
                               seconds=self.settings.pdgs.poll_interval_s, next_run_time=utcnow(),
                               max_instances=1, coalesce=True)
        self.scheduler.add_job(self.postprocess, "interval", id="postprocess",
                               name="Analyses, trend rules and periodic reports",
                               seconds=self.settings.reporting.check_interval_s, max_instances=1, coalesce=True)
        self.scheduler.start()
        log.info("Scheduler started")

    def postprocess(self) -> None:
        """Windowed analyses and trend rules, then periodic reports (after a running backfill)."""
        if self.poller.busy:  # wait until a running backfill has finished
            return
        try:
            self.analyses.run_once()
        except Exception:  # noqa: BLE001
            log.exception("Analyses failed")
        try:
            self.reports.check_schedule()
        except Exception:  # noqa: BLE001
            log.exception("Report generation failed")

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self.client.close()
