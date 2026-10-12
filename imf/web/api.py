"""JSON API used by the portal's charts (and usable by external tools)."""
from __future__ import annotations

import csv
import io
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from ..models import Annotation, Event, Parameter, PollRun, Product, Sample
from ..timeutil import iso, utcnow
from .auth import CurrentUser, require_expert, require_guest
from .common import can_see, resolve_range, services, visible_params_query

router = APIRouter(prefix="/api", tags=["api"])


def _limit_dict(p: Parameter) -> dict | None:
    lim = p.current_limit
    if lim is None:
        return None
    return {k: getattr(lim, k) for k in ("warn_lo", "warn_hi", "alarm_lo", "alarm_hi", "persistence")}


@router.get("/status")
def status(request: Request, user: CurrentUser = Depends(require_guest)) -> dict:
    svc = services(request)
    with svc.db.session() as s:
        params = list(s.scalars(visible_params_query(user).options(selectinload(Parameter.state))))
        levels = [p.state.level if p.state else 0 for p in params]
        n_products = s.scalar(select(func.count(Product.id)).where(Product.status == "PROCESSED")) or 0
        n_failed = s.scalar(select(func.count(Product.id)).where(Product.status == "FAILED")) or 0
        latest = s.scalar(select(func.max(Product.sensing_stop)))
        last_ingest = s.scalar(select(func.max(Product.ingested_at)))
        last_poll = s.scalars(select(PollRun).order_by(PollRun.id.desc()).limit(1)).first()
        visible_ids = [p.id for p in params]
        open_q = select(Event.severity, func.count()).where(Event.status != "CLOSED", Event.cleared_at.is_(None))
        if not user.is_expert:
            open_q = open_q.where(Event.parameter_id.in_(visible_ids))
        open_events = dict(s.execute(open_q.group_by(Event.severity)).all())
        unack = s.scalar(select(func.count(Event.id)).where(Event.status == "OPEN")) if user.is_expert else None
    overall = max(levels, default=0)
    return {
        "overall": ["NOMINAL", "WARNING", "ALARM"][overall],
        "products_processed": n_products, "products_failed": n_failed,
        "latest_sensing": iso(latest), "last_ingestion": iso(last_ingest),
        "last_poll": iso(last_poll.finished_at or last_poll.started_at) if last_poll else None,
        "last_poll_error": last_poll.error if last_poll else None,
        "polling": svc.poller.busy,
        "open_warnings": open_events.get("WARNING", 0), "open_alarms": open_events.get("ALARM", 0),
        "unacknowledged": unack, "server_time": iso(utcnow()),
        "parameters": {p.code: {"level": p.state.level if p.state else 0,
                                "value": p.state.last_value if p.state else None} for p in params},
    }


@router.get("/series")
def series(request: Request, codes: str, range: str = "30d", start: str | None = None, end: str | None = None,
           user: CurrentUser = Depends(require_guest)) -> dict:
    svc = services(request)
    out = {}
    with svc.db.session() as s:
        t0, t1 = resolve_range(s, range, start, end)
        for code in [c.strip() for c in codes.split(",") if c.strip()][:8]:
            p = s.scalar(select(Parameter).options(selectinload(Parameter.limits)).where(Parameter.code == code))
            if not can_see(user, p):
                continue
            q = select(Sample.time, Sample.value, Sample.level, Product.name, Product.id.label("pid")).join(Product) \
                .where(Sample.parameter_id == p.id).order_by(Sample.time)
            eq = select(Event).where(Event.parameter_id == p.id)
            aq = select(Annotation).where(Annotation.parameter_id == p.id)
            if t0:
                q = q.where(Sample.time >= t0)
                eq = eq.where(or_(Event.cleared_at.is_(None), Event.cleared_at >= t0))
                aq = aq.where(Annotation.time >= t0)
            if t1:
                q, eq, aq = q.where(Sample.time <= t1), eq.where(Event.time <= t1), aq.where(Annotation.time <= t1)
            rows = s.execute(q).all()
            out[code] = {
                "code": p.code, "name": p.name, "unit": p.unit, "group": p.group,
                "t": [iso(r.time) for r in rows], "v": [r.value for r in rows], "level": [r.level for r in rows],
                "product": [r.name for r in rows] if user.is_expert else None,
                "pid": [r.pid for r in rows] if user.is_expert else None,
                "limit": _limit_dict(p),
                "events": [{"id": e.id, "severity": e.severity, "time": iso(e.time), "value": e.value,
                            "cleared": iso(e.cleared_at), "status": e.status}
                           for e in s.scalars(eq)] if user.is_expert else [],
                "annotations": [{"time": iso(a.time), "text": a.text, "author": a.author} for a in s.scalars(aq)],
            }
    return {"start": iso(t0), "end": iso(t1), "series": out}


@router.get("/ingestion")
def ingestion(request: Request, days: int = Query(30, ge=1, le=400),
              user: CurrentUser = Depends(require_guest)) -> dict:
    svc = services(request)
    with svc.db.session() as s:
        latest = s.scalar(select(func.max(Product.sensing_start)))
        if latest is None:
            return {"days": [], "processed": [], "failed": [], "expected": 0}
        t0 = latest - timedelta(days=days)
        rows = s.execute(select(func.date(Product.sensing_start), Product.status, func.count())
                         .where(Product.sensing_start >= t0)
                         .group_by(func.date(Product.sensing_start), Product.status)).all()
    by_day: dict[str, dict] = {}
    for d, st, n in rows:
        by_day.setdefault(str(d), {"PROCESSED": 0, "FAILED": 0})[st] = n
    keys = sorted(by_day)
    expected = 24 * 60 / svc.settings.mission.orbit_period_min
    return {"days": keys, "processed": [by_day[k].get("PROCESSED", 0) for k in keys],
            "failed": [by_day[k].get("FAILED", 0) for k in keys], "expected": expected}


@router.get("/parameters")
def parameters(request: Request, user: CurrentUser = Depends(require_guest)) -> list[dict]:
    svc = services(request)
    with svc.db.session() as s:
        params = s.scalars(visible_params_query(user).options(
            selectinload(Parameter.limits), selectinload(Parameter.state)))
        return [{"code": p.code, "name": p.name, "unit": p.unit, "group": p.group, "visibility": p.visibility,
                 "algorithm": p.algorithm, "limit": _limit_dict(p),
                 "level": p.state.level if p.state else 0,
                 "last_value": p.state.last_value if p.state else None,
                 "last_time": iso(p.state.last_time) if p.state else None} for p in params]


@router.get("/parameters/{code}/csv")
def export_csv(request: Request, code: str, range: str = "all", start: str | None = None, end: str | None = None,
               user: CurrentUser = Depends(require_expert)):
    svc = services(request)
    with svc.db.session() as s:
        p = s.scalar(select(Parameter).where(Parameter.code == code))
        if p is None:
            raise HTTPException(404)
        t0, t1 = resolve_range(s, range, start, end)
        q = select(Sample.time, Sample.value, Sample.level, Product.name, Product.orbit).join(Product) \
            .where(Sample.parameter_id == p.id).order_by(Sample.time)
        if t0:
            q = q.where(Sample.time >= t0)
        if t1:
            q = q.where(Sample.time <= t1)
        rows = s.execute(q).all()
        unit = p.unit
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_utc", f"{code} [{unit}]", "level", "orbit", "product"])
    for r in rows:
        w.writerow([iso(r.time), f"{r.value:.6g}", ["NOMINAL", "WARNING", "ALARM"][r.level], r.orbit, r.name])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{code}.csv"'})
