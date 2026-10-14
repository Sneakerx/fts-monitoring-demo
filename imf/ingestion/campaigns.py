"""Reprocessing campaigns: ingest the products of another processing baseline over the full mission
(or a period) in parallel to the nominal operation.

A campaign has its own watermark and worker (thread in the portal, or a separate process via
`python -m imf.cli campaign run <id>`). Its samples, series states and events are kept per baseline,
so the nominal monitoring is not affected; rules and notifications only act on the nominal baseline.
The web views offer a baseline selector to compare nominal and reprocessed results.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime

from sqlalchemy import select

from ..db import Database, get_state
from ..models import AuditLog, Campaign, TimelineEntry
from ..monitoring import timeline
from ..timeutil import parse_iso, utcnow
from .poller import Poller, PollSummary

log = logging.getLogger(__name__)


class CampaignService:
    def __init__(self, db: Database, poller: Poller):
        self.db = db
        self.poller = poller
        self._threads: dict[int, threading.Thread] = {}

    def create(self, name: str, baseline: str, product_type: str = "FTS_L1B", start: datetime | None = None,
               end: datetime | None = None, user: str = "system") -> int:
        if baseline == self.poller.nominal:
            raise ValueError("A reprocessing campaign needs a baseline other than the nominal one")
        with self.db.session() as s:
            if s.scalar(select(Campaign.id).where(Campaign.name == name)):
                raise ValueError(f"Campaign {name} already exists")
            c = Campaign(name=name, baseline=baseline, product_type=product_type, start=start, end=end,
                         status="PLANNED", created_by=user)
            s.add(c)
            s.add(AuditLog(username=user, action="campaign.create", target=name,
                           details=f"baseline={baseline} type={product_type} {start} - {end}"))
            s.flush()
            return c.id

    def is_running(self, cid: int) -> bool:
        t = self._threads.get(cid)
        return t is not None and t.is_alive()

    def start(self, cid: int, user: str = "system") -> threading.Thread:
        if self.is_running(cid):
            raise ValueError("Campaign already running")
        with self.db.session() as s:
            s.add(AuditLog(username=user, action="campaign.start", target=str(cid)))
        t = threading.Thread(target=self.run, args=(cid,), name=f"campaign-{cid}", daemon=True)
        self._threads[cid] = t
        t.start()
        return t

    def cancel(self, cid: int, user: str = "system") -> None:
        with self.db.session() as s:
            c = s.get(Campaign, cid)
            if c and c.status in ("PLANNED", "RUNNING"):
                c.status = "CANCELLED"
                s.add(AuditLog(username=user, action="campaign.cancel", target=c.name))

    def _status(self, cid: int) -> str:
        with self.db.session() as s:
            return s.get(Campaign, cid).status

    def run(self, cid: int) -> PollSummary:
        """Process the campaign until the catalogue is exhausted (blocking)."""
        with self.db.session() as s:
            c = s.get(Campaign, cid)
            if c is None or c.status in ("DONE", "CANCELLED"):
                return PollSummary(error="not runnable")
            c.status, c.started_at, c.error = "RUNNING", c.started_at or utcnow(), None
            baseline, ptype, start, end, name = c.baseline, c.product_type, c.start, c.end, c.name
        tcfg = self.poller.settings.pdgs.type_cfg(ptype)
        key = f"campaign.{cid}.watermark"
        summary = PollSummary()
        try:
            while True:
                with self.db.session() as s:
                    wm = get_state(s, key)
                entries = self.poller.client.query_new(parse_iso(wm) if wm else None, top=self.poller.page_size,
                                                       product_type=ptype, baseline=baseline)
                for e in entries:
                    if self._status(cid) == "CANCELLED":
                        return summary
                    if (start and e.sensing_start < start) or (end and e.sensing_start >= end):
                        summary.skipped += 1
                        with self.db.session() as s:
                            Poller._advance(s, e, key)
                        continue
                    summary.found += 1
                    self.poller.ingest(e, tcfg, summary, watermark_key=key, campaign_id=cid)
                with self.db.session() as s:
                    c = s.get(Campaign, cid)
                    c.n_found, c.n_processed, c.n_failed = summary.found, summary.processed, summary.failed
                if len(entries) < self.poller.page_size:
                    break
            with self.db.session() as s:
                c = s.get(Campaign, cid)
                c.status, c.finished_at = "DONE", utcnow()
                first = s.scalar(select(TimelineEntry.id).where(TimelineEntry.type == "BASELINE",
                                                                TimelineEntry.attributes.contains(f'"campaign": {cid}')))
                if first is None:
                    timeline.add_entry(s, "BASELINE", f"Reprocessing campaign {name} ({baseline}) completed: "
                                       f"{summary.processed} products", c.started_at, c.finished_at,
                                       attributes={"campaign": cid, "baseline": baseline}, baseline=baseline)
            log.info("Campaign %s finished: %s", name, summary)
        except Exception as exc:  # noqa: BLE001
            log.exception("Campaign %s failed", name)
            with self.db.session() as s:
                c = s.get(Campaign, cid)
                c.status, c.error, c.finished_at = "FAILED", str(exc)[:1000], utcnow()
            summary.error = str(exc)
        return summary
