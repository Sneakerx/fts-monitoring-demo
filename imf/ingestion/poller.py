"""PDGS poller: discovers new L1 products, downloads and processes them.

* Watermark on PublicationDate -> only new publications are requested.
* Idempotent: products already known (by PDGS Id) are skipped.
* Each product is processed in its own transaction; a failing product is recorded as FAILED
  and does not block the others.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from sqlalchemy import select

from ..db import Database, get_state, set_state
from ..models import PollRun, Product
from ..monitoring.notifier import Notifier
from ..processing.pipeline import Pipeline
from ..timeutil import iso, parse_iso, utcnow
from .pdgs_client import PdgsClient, PdgsError, ProductEntry

log = logging.getLogger(__name__)

WATERMARK_KEY = "poll.watermark"


@dataclass
class PollSummary:
    found: int = 0
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    error: str | None = None


class Poller:
    def __init__(self, db: Database, client: PdgsClient, pipeline: Pipeline, notifier: Notifier,
                 download_dir, keep_products: bool = False, page_size: int = 100):
        self.db = db
        self.client = client
        self.pipeline = pipeline
        self.notifier = notifier
        self.download_dir = download_dir
        self.keep_products = keep_products
        self.page_size = page_size
        self._lock = threading.Lock()
        self.busy = False

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
            while True:
                with self.db.session() as s:
                    wm = get_state(s, WATERMARK_KEY)
                entries = self.client.query_new(parse_iso(wm) if wm else None, top=self.page_size)
                summary.found += len(entries)
                for e in entries:
                    self._handle(e, summary)
                if len(entries) < self.page_size:
                    break
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

    def _handle(self, e: ProductEntry, summary: PollSummary) -> None:
        with self.db.session() as s:
            if s.scalar(select(Product.id).where(Product.pdgs_id == e.pdgs_id)) is not None:
                summary.skipped += 1
                self._advance(s, e)
                return
            product = Product(pdgs_id=e.pdgs_id, name=e.name, product_type=e.product_type, orbit=e.orbit,
                              sensing_start=e.sensing_start, sensing_stop=e.sensing_stop,
                              publication_date=e.publication_date, size_bytes=e.size_bytes, status="NEW")
            s.add(product)
            s.flush()
            t0 = time.perf_counter()
            path = None
            try:
                path = self.client.download(e, self.download_dir)
                result = self.pipeline.process(s, product, path)
                product.status = "PROCESSED"
                if result.algorithm_errors:
                    product.error = "; ".join(f"{k}: {v}" for k, v in result.algorithm_errors.items())
                summary.processed += 1
            except PdgsError:
                raise  # catalogue/download problem -> abort this poll, retry next cycle
            except Exception as exc:  # noqa: BLE001 - corrupted product etc.
                s.rollback()
                product = Product(pdgs_id=e.pdgs_id, name=e.name, product_type=e.product_type, orbit=e.orbit,
                                  sensing_start=e.sensing_start, sensing_stop=e.sensing_stop,
                                  publication_date=e.publication_date, size_bytes=e.size_bytes,
                                  status="FAILED", error=f"{type(exc).__name__}: {exc}")
                s.add(product)
                summary.failed += 1
                result = None
                log.error("Processing of %s failed: %s", e.name, exc)
            finally:
                if path is not None and not self.keep_products:
                    path.unlink(missing_ok=True)
            product.processing_ms = (time.perf_counter() - t0) * 1000.0
            self._advance(s, e)
            s.flush()
            if result:
                self.notifier.notify(result.new_events, "RAISED")
                self.notifier.notify(result.cleared_events, "CLEARED")

    @staticmethod
    def _advance(s, e: ProductEntry) -> None:
        wm = get_state(s, WATERMARK_KEY)
        if wm is None or parse_iso(wm) < e.publication_date:
            set_state(s, WATERMARK_KEY, iso(e.publication_date))
