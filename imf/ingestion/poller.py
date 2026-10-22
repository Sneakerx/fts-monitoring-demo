"""PDGS poller: discovers new products of all configured types, downloads and processes them.

* One watermark on PublicationDate per product type -> only new publications are requested.
* The L1 type is polled for the nominal baseline only (reprocessed baselines: `campaigns.py`).
* Header-only types (typically L0) are not downloaded: their catalogue attributes are stored and processed.
* Idempotent: products already known (by PDGS Id) are skipped.
* Each product is processed in its own transaction; a failing product is recorded as FAILED
  and does not block the others.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass

from sqlalchemy import select

from ..config import ProductTypeCfg, Settings
from ..db import Database, get_state, set_state
from ..models import INDEPENDENT, PollRun, Product
from ..monitoring.notifier import Notifier
from ..processing.pipeline import Pipeline
from ..timeutil import iso, parse_iso, utcnow
from .pdgs_client import PdgsClient, PdgsError, ProductEntry

log = logging.getLogger(__name__)

WATERMARK_PREFIX = "poll.watermark."


@dataclass
class PollSummary:
    found: int = 0
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    error: str | None = None


class Poller:
    def __init__(self, db: Database, client: PdgsClient, pipeline: Pipeline, notifier: Notifier,
                 settings: Settings, rules=None):
        self.db = db
        self.client = client
        self.pipeline = pipeline
        self.notifier = notifier
        self.settings = settings
        self.rules = rules
        self.download_dir = settings.pdgs.download_dir
        self.keep_products = settings.pdgs.keep_products
        self.page_size = settings.pdgs.page_size
        self._lock = threading.Lock()
        self.busy = False

    @property
    def nominal(self) -> str:
        return self.settings.pdgs.baseline

    def run_once(self, trigger: str = "scheduler") -> PollSummary:
        if not self._lock.acquire(blocking=False):
            log.info("Poll already running - skipped")
            return PollSummary(error="already running")
        self.busy = True
        summary = PollSummary()
        with self.db.session() as s:
            run = PollRun(trigger=trigger)
            s.add(run)
            s.flush()
            run_id = run.id
        try:
            # in the order of pdgs.levels (e.g. L0 first), so L1 analyses find their inputs
            pdgs = self.settings.pdgs
            for tcfg in sorted(pdgs.product_types, key=lambda t: pdgs.level_order(t.level)):
                self._poll_type(tcfg, summary)
        except PdgsError as exc:
            summary.error = str(exc)
            log.error("Polling failed: %s", exc)
        finally:
            with self.db.session() as s:
                run = s.get(PollRun, run_id)
                run.finished_at = utcnow()
                run.found, run.processed = summary.found, summary.processed
                run.failed, run.skipped, run.error = summary.failed, summary.skipped, summary.error
            self.busy = False
            self._lock.release()
        if summary.found:
            log.info("Poll finished: %s", summary)
        return summary

    def _poll_type(self, tcfg: ProductTypeCfg, summary: PollSummary) -> None:
        key = WATERMARK_PREFIX + tcfg.type
        baseline = self.nominal if tcfg.filters_baseline else None
        while True:
            with self.db.session() as s:
                wm = get_state(s, key)
            entries = self.client.query_new(parse_iso(wm) if wm else None, top=self.page_size,
                                            product_type=tcfg.type, baseline=baseline)
            summary.found += len(entries)
            for e in entries:
                self.ingest(e, tcfg, summary, watermark_key=key)
            if len(entries) < self.page_size:
                break

    def ingest(self, e: ProductEntry, tcfg: ProductTypeCfg, summary: PollSummary, watermark_key: str,
               campaign_id: int | None = None) -> None:
        """Ingest one catalogue entry (own transaction). Used by the poller and by reprocessing campaigns."""
        with self.db.session() as s:
            if s.scalar(select(Product.id).where(Product.pdgs_id == e.pdgs_id)) is not None:
                summary.skipped += 1
                self._advance(s, e, watermark_key)
                return
            baseline = (e.baseline or self.nominal) if tcfg.level == "L1" else INDEPENDENT
            fields = dict(pdgs_id=e.pdgs_id, name=e.name, product_type=e.product_type, level=tcfg.level,
                          orbit=e.orbit, sensing_start=e.sensing_start, sensing_stop=e.sensing_stop,
                          publication_date=e.publication_date, size_bytes=e.size_bytes, baseline=baseline,
                          processor_version=_str(e.attributes.get("processorVersion")),
                          product_version=_str(e.attributes.get("productVersion")),
                          header=json.dumps(e.attributes) if e.attributes else None, campaign_id=campaign_id)
            product = Product(status="NEW", **fields)
            s.add(product)
            s.flush()
            t0 = time.perf_counter()
            path = None
            nominal = baseline in (self.nominal, INDEPENDENT) and campaign_id is None
            try:
                if tcfg.mode == "header":
                    header = {**e.attributes, "_sensing_start": iso(e.sensing_start),
                              "_sensing_stop": iso(e.sensing_stop)}
                    result = self.pipeline.process(s, product, None, header)
                else:
                    path, product.checksum = self.client.download(e, self.download_dir)
                    result = self.pipeline.process(s, product, path,
                                                   entry_type="reprocessed" if campaign_id else "derived")
                product.status = "PROCESSED"
                if result.algorithm_errors:
                    product.error = "; ".join(f"{k}: {v}" for k, v in result.algorithm_errors.items())
                rule_events = []
                if self.rules is not None and nominal:
                    rule_events = self.rules.on_product(s, product)
                    self.rules.on_limit_events(s, result.new_events)
                    self.rules.on_cleared(s, result.cleared_events)
                summary.processed += 1
            except PdgsError:
                raise  # catalogue/download problem -> abort this poll, retry next cycle
            except Exception as exc:  # noqa: BLE001 - corrupted product etc.
                s.rollback()
                product = Product(status="FAILED", error=f"{type(exc).__name__}: {exc}", **fields)
                s.add(product)
                summary.failed += 1
                result, rule_events = None, []
                log.error("Processing of %s failed: %s", e.name, exc)
            finally:
                if path is not None and not self.keep_products:
                    path.unlink(missing_ok=True)
            product.processing_ms = (time.perf_counter() - t0) * 1000.0
            self._advance(s, e, watermark_key)
            s.flush()
            if result and nominal:
                self.notifier.notify(result.new_events + rule_events, "RAISED")
                self.notifier.notify(result.cleared_events, "CLEARED")

    @staticmethod
    def _advance(s, e: ProductEntry, key: str) -> None:
        wm = get_state(s, key)
        if wm is None or parse_iso(wm) < e.publication_date:
            set_state(s, key, iso(e.publication_date))


def _str(v) -> str | None:
    return None if v is None else str(v)[:16]
