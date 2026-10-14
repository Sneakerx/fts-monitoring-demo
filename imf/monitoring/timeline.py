"""Mission timeline: generated systematically from the PDGS data (data gaps, L1 processing gaps,
calibration modes, anomalies, actions) and maintained manually by the instrument experts."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import TimelineEntry
from ..timeutil import utcnow

TYPES = {
    "DATA_GAP": "No products received from the PDGS",
    "L1_GAP": "L0 data not processed to L1",
    "CALIBRATION_MODE": "Special calibration mode",
    "ANOMALY": "Instrument anomaly (alarm)",
    "ACTION": "Action recommended by a rule",
    "BASELINE": "Processing baseline / reprocessing",
    "MANUAL": "Manual entry",
    "OPERATIONS": "Operations event (e.g. decontamination, manoeuvre)",
}


def add_entry(s: Session, type_: str, title: str, start: datetime, stop: datetime | None = None, *,
              source: str = "auto", rule: str | None = None, attributes: dict | None = None,
              product_id: int | None = None, event_id: int | None = None, baseline: str = "",
              author: str = "system", merge_within: timedelta | None = None) -> TimelineEntry:
    """Add a timeline entry; with `merge_within`, an adjacent auto entry of the same type is extended
    instead (e.g. a data gap that spans two analysis windows)."""
    if merge_within is not None:
        prev = s.scalar(select(TimelineEntry).where(
            TimelineEntry.type == type_, TimelineEntry.source == source, TimelineEntry.baseline == baseline,
            TimelineEntry.start <= start, TimelineEntry.stop >= start - merge_within)
            .order_by(TimelineEntry.start.desc()).limit(1))
        if prev is not None:
            if stop is not None and (prev.stop is None or stop > prev.stop):
                prev.stop = stop
            return prev
    entry = TimelineEntry(type=type_, title=title[:200], start=start, stop=stop, source=source, rule=rule,
                          attributes=json.dumps(attributes or {}), product_id=product_id, event_id=event_id,
                          baseline=baseline, author=author, entry_time=utcnow())
    s.add(entry)
    return entry
