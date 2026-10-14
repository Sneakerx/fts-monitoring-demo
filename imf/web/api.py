"""JSON API used by the portal's charts and by external tools.

Read endpoints use the portal session (role-dependent). The write API under `/api/v1` lets externally
developed tools add parameters, samples, spectra and timeline entries (R8); it accepts an expert session
or a bearer token (Admin -> API tokens). Every written entry records its entry type and source (R2).
"""
from __future__ import annotations

import csv
import io
import json
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from .. import mdb
from ..models import (Annotation, Event, Parameter, PollRun, Product, ProposedOutput, ReferenceData, Sample,
                      TimelineEntry, VectorSample)
from ..monitoring import timeline as tl
from ..timeutil import iso, parse_iso, utcnow
from .auth import CurrentUser, api_writer, require_expert, require_guest
from .common import audit, baseline_filter, baselines, can_see, resolve_range, services, visible_params_query

router = APIRouter(prefix="/api", tags=["api"])
OVERLAY_TYPES = ("DATA_GAP", "L1_GAP", "CALIBRATION_MODE", "OPERATIONS", "MANUAL")


def _limit_dict(p: Parameter) -> dict | None:
    lim = p.current_limit
    if lim is None:
        return None
    return {k: getattr(lim, k) for k in ("warn_lo", "warn_hi", "alarm_lo", "alarm_hi", "persistence")}


def _req_dict(p: Parameter) -> dict | None:
    r = p.current_requirement
    return None if r is None else {"lo": r.lo, "hi": r.hi, "text": r.text, "reference": r.reference}


def _baseline(request: Request, baseline: str | None) -> str:
    return baseline or services(request).settings.pdgs.baseline


@router.get("/status")
def status(request: Request, user: CurrentUser = Depends(require_guest)) -> dict:
    svc = services(request)
    nominal = svc.settings.pdgs.baseline
    with svc.db.session() as s:
        params = list(s.scalars(visible_params_query(user).options(selectinload(Parameter.states))))
        levels = [p.state.level if p.state else 0 for p in params]
        l1 = Product.level == "L1"
        n_products = s.scalar(select(func.count(Product.id)).where(Product.status == "PROCESSED", l1,
                                                                   Product.baseline == nominal)) or 0
        n_failed = s.scalar(select(func.count(Product.id)).where(Product.status == "FAILED")) or 0
        n_all = s.scalar(select(func.count(Product.id))) or 0
        latest = s.scalar(select(func.max(Product.sensing_stop)).where(l1, Product.baseline == nominal))
        last_ingest = s.scalar(select(func.max(Product.ingested_at)))
        last_poll = s.scalars(select(PollRun).order_by(PollRun.id.desc()).limit(1)).first()
        visible_ids = [p.id for p in params]
        open_q = select(Event.severity, func.count()).where(Event.status != "CLOSED", Event.cleared_at.is_(None),
                                                            Event.category != "timeline",
                                                            baseline_filter(Event.baseline, nominal))
        if not user.is_expert:
            open_q = open_q.where(Event.parameter_id.in_(visible_ids))
        open_events = dict(s.execute(open_q.group_by(Event.severity)).all())
        unack = s.scalar(select(func.count(Event.id)).where(Event.status == "OPEN")) if user.is_expert else None
        actions = s.scalar(select(func.count(ProposedOutput.id)).where(ProposedOutput.status == "DRAFT")) \
            if user.is_expert else None
    overall = max(levels, default=0)
    return {
        "overall": ["NOMINAL", "WARNING", "ALARM"][overall], "baseline": nominal,
        "products_processed": n_products, "products_failed": n_failed, "products_total": n_all,
        "latest_sensing": iso(latest), "last_ingestion": iso(last_ingest),
        "last_poll": iso(last_poll.finished_at or last_poll.started_at) if last_poll else None,
        "last_poll_error": last_poll.error if last_poll else None,
        "polling": svc.poller.busy,
        "open_warnings": open_events.get("WARNING", 0), "open_alarms": open_events.get("ALARM", 0),
        "unacknowledged": unack, "proposals_pending": actions, "server_time": iso(utcnow()),
        "parameters": {p.code: {"level": p.state.level if p.state else 0,
                                "value": p.state.last_value if p.state else None} for p in params},
    }


@router.get("/baselines")
def list_baselines(request: Request, user: CurrentUser = Depends(require_guest)) -> dict:
    svc = services(request)
    with svc.db.session() as s:
        return {"nominal": svc.settings.pdgs.baseline, "baselines": baselines(s, svc.settings.pdgs.baseline)}


@router.get("/series")
def series(request: Request, codes: str, range: str = "30d", start: str | None = None, end: str | None = None,
           baseline: str | None = None, user: CurrentUser = Depends(require_guest)) -> dict:
    """Time series per parameter; dimensioned parameters return one trace per series (dims)."""
    svc = services(request)
    bl = _baseline(request, baseline)
    out = {}
    with svc.db.session() as s:
        t0, t1 = resolve_range(s, range, start, end)
        for code in [c.strip() for c in codes.split(",") if c.strip()][:8]:
            p = s.scalar(select(Parameter).options(selectinload(Parameter.limits), selectinload(Parameter.requirements))
                         .where(Parameter.code == code))
            if not can_see(user, p) or p.kind != "scalar":
                continue
            q = select(Sample.time, Sample.value, Sample.level, Sample.dims, Sample.entry_type, Product.name,
                       Product.id.label("pid")).outerjoin(Product) \
                .where(Sample.parameter_id == p.id, baseline_filter(Sample.baseline, bl)).order_by(Sample.time)
            eq = select(Event).where(Event.parameter_id == p.id, baseline_filter(Event.baseline, bl))
            aq = select(Annotation).where(Annotation.parameter_id == p.id)
            if t0:
                q = q.where(Sample.time >= t0)
                eq = eq.where(or_(Event.cleared_at.is_(None), Event.cleared_at >= t0))
                aq = aq.where(Annotation.time >= t0)
            if t1:
                q, eq, aq = q.where(Sample.time <= t1), eq.where(Event.time <= t1), aq.where(Annotation.time <= t1)
            traces: dict[str, dict] = {}
            for r in s.execute(q):
                tr = traces.setdefault(r.dims, {"dims": r.dims, "t": [], "v": [], "level": [], "product": [],
                                                "pid": [], "entry": []})
                tr["t"].append(iso(r.time))
                tr["v"].append(r.value)
                tr["level"].append(r.level)
                tr["product"].append(r.name or f"({r.entry_type})")
                tr["pid"].append(r.pid)
                tr["entry"].append(r.entry_type)
            if not user.is_expert:
                for tr in traces.values():
                    tr["product"] = tr["pid"] = None
            out[code] = {
                "code": p.code, "name": p.name, "unit": p.unit, "group": p.group, "dims": p.dims,
                "traces": [traces[k] for k in sorted(traces)],
                "limit": _limit_dict(p), "requirement": _req_dict(p),
                "events": [{"id": e.id, "severity": e.severity, "time": iso(e.time), "value": e.value, "dims": e.dims,
                            "cleared": iso(e.cleared_at), "status": e.status, "category": e.category}
                           for e in s.scalars(eq)] if user.is_expert else [],
                "annotations": [{"time": iso(a.time), "text": a.text, "author": a.author} for a in s.scalars(aq)],
            }
        tq = select(TimelineEntry).where(TimelineEntry.type.in_(OVERLAY_TYPES),
                                         baseline_filter(TimelineEntry.baseline, bl))
        if t0:
            tq = tq.where(or_(TimelineEntry.stop.is_(None), TimelineEntry.stop >= t0))
        if t1:
            tq = tq.where(TimelineEntry.start <= t1)
        overlay = [{"type": e.type, "title": e.title, "start": iso(e.start), "stop": iso(e.stop)}
                   for e in s.scalars(tq.order_by(TimelineEntry.start).limit(500))]
    return {"start": iso(t0), "end": iso(t1), "baseline": bl, "series": out, "timeline": overlay}


@router.get("/spectrum")
def spectrum(request: Request, code: str = "NESR_SPECTRAL", range: str = "1d", start: str | None = None,
             end: str | None = None, baseline: str | None = None, template: str = "NESR_TEMPLATE",
             user: CurrentUser = Depends(require_guest)) -> dict:
    """Vector parameter per series: latest spectrum, mean over the range, and the reference template."""
    svc = services(request)
    bl = _baseline(request, baseline)
    with svc.db.session() as s:
        p = s.scalar(select(Parameter).where(Parameter.code == code))
        if not can_see(user, p) or p.kind != "vector":
            raise HTTPException(404, "Unknown vector parameter")
        anchor = s.scalar(select(func.max(VectorSample.time)).where(VectorSample.parameter_id == p.id))
        if start or end:
            t0, t1 = resolve_range(s, range, start, end)
        else:
            span = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}.get(range)
            t1 = anchor
            t0 = anchor - timedelta(days=span) if (anchor and span) else None
        q = select(VectorSample).where(VectorSample.parameter_id == p.id, baseline_filter(VectorSample.baseline, bl))
        if t0:
            q = q.where(VectorSample.time >= t0)
        if t1:
            q = q.where(VectorSample.time <= t1)
        groups: dict[str, list[VectorSample]] = {}
        for vs in s.scalars(q.order_by(VectorSample.time)):
            groups.setdefault(vs.dims, []).append(vs)
        out = {}
        for d, vss in sorted(groups.items()):
            vals = [json.loads(v.values) for v in vss]
            out[d] = {"axis": json.loads(vss[-1].axis), "n": len(vss),
                      "mean": [sum(c) / len(c) for c in zip(*vals)],
                      "latest": {"time": iso(vss[-1].time), "values": vals[-1], "product_id": vss[-1].product_id}}
        tmpl = {}
        if template:
            for d in out:
                ref = mdb.reference(s, template, d)
                if ref:
                    tmpl[d] = {"axis": json.loads(ref.axis), "values": json.loads(ref.values), "version": ref.version,
                               "unit": ref.unit, "source": ref.source}
    return {"code": p.code, "name": p.name, "unit": p.unit, "start": iso(t0), "end": iso(t1), "baseline": bl,
            "series": out, "template": tmpl}


@router.get("/geo")
def geo(request: Request, range: str = "1d", start: str | None = None, end: str | None = None,
        user: CurrentUser = Depends(require_guest)) -> dict:
    """Ground tracks (spectrogram footprints) of the orbits in the range (max. 60 orbits)."""
    svc = services(request)
    bl = svc.settings.pdgs.baseline
    with svc.db.session() as s:
        p = s.scalar(select(Parameter).where(Parameter.code == "GEO_TRACK"))
        if not can_see(user, p):
            return {"tracks": []}
        anchor = s.scalar(select(func.max(VectorSample.time)).where(VectorSample.parameter_id == p.id))
        if anchor is None:
            return {"tracks": []}
        if start or end:
            t0, t1 = resolve_range(s, range, start, end)
        else:
            t1, t0 = anchor, anchor - timedelta(days={"1d": 1, "7d": 7, "30d": 30}.get(range, 1))
        rows = s.execute(select(VectorSample, Product.orbit, Product.name).outerjoin(Product, VectorSample.product_id == Product.id)
                         .where(VectorSample.parameter_id == p.id, VectorSample.time >= t0, VectorSample.time <= t1,
                                baseline_filter(VectorSample.baseline, bl))
                         .order_by(VectorSample.time.desc()).limit(60)).all()
    return {"start": iso(t0), "end": iso(t1),
            "tracks": [{"time": iso(vs.time), "orbit": orbit, "product": name if user.is_expert else None,
                        "lon": json.loads(vs.axis), "lat": json.loads(vs.values)} for vs, orbit, name in rows]}


@router.get("/timeline")
def timeline(request: Request, range: str = "all", start: str | None = None, end: str | None = None,
             types: str = "", user: CurrentUser = Depends(require_guest)) -> dict:
    svc = services(request)
    with svc.db.session() as s:
        t0, t1 = resolve_range(s, range, start, end)
        q = select(TimelineEntry).where(baseline_filter(TimelineEntry.baseline, svc.settings.pdgs.baseline))
        if types:
            q = q.where(TimelineEntry.type.in_([t.strip() for t in types.split(",")]))
        if not user.is_expert:
            q = q.where(TimelineEntry.type.in_(("DATA_GAP", "L1_GAP", "CALIBRATION_MODE", "OPERATIONS", "BASELINE")))
        if t0:
            q = q.where(or_(TimelineEntry.stop.is_(None), TimelineEntry.stop >= t0))
        if t1:
            q = q.where(TimelineEntry.start <= t1)
        rows = list(s.scalars(q.order_by(TimelineEntry.start).limit(2000)))
        latest = s.scalar(select(func.max(Sample.time)))
    return {"start": iso(t0), "end": iso(t1), "latest": iso(latest), "types": tl.TYPES,
            "entries": [{"id": e.id, "type": e.type, "title": e.title, "start": iso(e.start), "stop": iso(e.stop),
                         "source": e.source, "rule": e.rule, "event_id": e.event_id if user.is_expert else None,
                         "author": e.author} for e in rows]}


@router.get("/requirements/{code}/stats")
def requirement_stats(request: Request, code: str, range: str = "all", start: str | None = None,
                      end: str | None = None, baseline: str | None = None,
                      user: CurrentUser = Depends(require_guest)) -> dict:
    """Statistics of a parameter versus its requirement (M8)."""
    svc = services(request)
    bl = _baseline(request, baseline)
    with svc.db.session() as s:
        p = s.scalar(select(Parameter).options(selectinload(Parameter.requirements)).where(Parameter.code == code))
        if not can_see(user, p) or p.current_requirement is None:
            raise HTTPException(404, "No requirement for this parameter")
        req = p.current_requirement
        t0, t1 = resolve_range(s, range, start, end)
        q = select(Sample.value).where(Sample.parameter_id == p.id, baseline_filter(Sample.baseline, bl))
        if t0:
            q = q.where(Sample.time >= t0)
        if t1:
            q = q.where(Sample.time <= t1)
        vals = list(s.scalars(q))
    n = len(vals)
    ok = sum(1 for v in vals if req.complies(v))
    mean = sum(vals) / n if n else None
    std = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5 if n > 1 else None
    srt = sorted(vals)
    pct = (lambda q_: srt[min(n - 1, int(q_ * n))] if n else None)
    return {"code": code, "requirement": _req_dict(p), "start": iso(t0), "end": iso(t1), "baseline": bl, "n": n,
            "n_compliant": ok, "compliance_percent": 100.0 * ok / n if n else None, "mean": mean, "std": std,
            "min": srt[0] if n else None, "max": srt[-1] if n else None, "p05": pct(0.05), "p50": pct(0.5),
            "p95": pct(0.95)}


@router.get("/ingestion")
def ingestion(request: Request, days: int = Query(30, ge=1, le=400),
              user: CurrentUser = Depends(require_guest)) -> dict:
    svc = services(request)
    nominal = svc.settings.pdgs.baseline
    with svc.db.session() as s:
        latest = s.scalar(select(func.max(Product.sensing_start)))
        if latest is None:
            return {"days": [], "processed": [], "failed": [], "l0": [], "expected": 0}
        t0 = latest - timedelta(days=days)
        rows = s.execute(select(func.date(Product.sensing_start), Product.status, func.count())
                         .where(Product.sensing_start >= t0, Product.level == "L1", Product.baseline == nominal)
                         .group_by(func.date(Product.sensing_start), Product.status)).all()
        l0 = dict(s.execute(select(func.date(Product.sensing_start), func.count())
                            .where(Product.sensing_start >= t0, Product.product_type == "FTS_L0_EO")
                            .group_by(func.date(Product.sensing_start))).all())
    by_day: dict[str, dict] = {}
    for d, st, n in rows:
        by_day.setdefault(str(d), {"PROCESSED": 0, "FAILED": 0})[st] = n
    keys = sorted(set(by_day) | {str(k) for k in l0})
    expected = 24 * 60 / svc.settings.mission.orbit_period_min
    return {"days": keys, "processed": [by_day.get(k, {}).get("PROCESSED", 0) for k in keys],
            "failed": [by_day.get(k, {}).get("FAILED", 0) for k in keys],
            "l0": [l0.get(k, 0) for k in keys], "expected": expected}


@router.get("/parameters")
def parameters(request: Request, user: CurrentUser = Depends(require_guest)) -> list[dict]:
    svc = services(request)
    with svc.db.session() as s:
        params = s.scalars(visible_params_query(user).options(
            selectinload(Parameter.limits), selectinload(Parameter.states), selectinload(Parameter.requirements)))
        return [{"code": p.code, "name": p.name, "unit": p.unit, "group": p.group, "visibility": p.visibility,
                 "kind": p.kind, "dims": p.dims, "origin": p.origin,
                 "algorithm": p.algorithm, "limit": _limit_dict(p), "requirement": _req_dict(p),
                 "level": p.state.level if p.state else 0,
                 "last_value": p.state.last_value if p.state else None,
                 "last_time": iso(p.state.last_time) if p.state else None} for p in params]


@router.get("/parameters/{code}/csv")
def export_csv(request: Request, code: str, range: str = "all", start: str | None = None, end: str | None = None,
               baseline: str | None = None, user: CurrentUser = Depends(require_expert)):
    svc = services(request)
    bl = _baseline(request, baseline)
    with svc.db.session() as s:
        p = s.scalar(select(Parameter).where(Parameter.code == code))
        if p is None:
            raise HTTPException(404)
        t0, t1 = resolve_range(s, range, start, end)
        q = select(Sample.time, Sample.value, Sample.level, Sample.dims, Sample.baseline, Sample.entry_type,
                   Sample.source, Sample.entry_time, Product.name, Product.orbit).outerjoin(Product) \
            .where(Sample.parameter_id == p.id, baseline_filter(Sample.baseline, bl)).order_by(Sample.time)
        if t0:
            q = q.where(Sample.time >= t0)
        if t1:
            q = q.where(Sample.time <= t1)
        rows = s.execute(q).all()
        unit = p.unit
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_utc", f"{code} [{unit}]", "dims", "baseline", "level", "orbit", "product", "entry_type",
                "source", "entry_time_utc"])
    for r in rows:
        w.writerow([iso(r.time), f"{r.value:.6g}", r.dims, r.baseline, ["NOMINAL", "WARNING", "ALARM"][r.level],
                    r.orbit, r.name or "", r.entry_type, r.source, iso(r.entry_time)])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{code}.csv"'})


@router.get("/export")
def export(request: Request, range: str = "all", start: str | None = None, end: str | None = None,
           baseline: str | None = None, codes: str = "", user: CurrentUser = Depends(require_expert)):
    """MDB export (R5): ZIP with samples, spectra, events, timeline, products, reference data, metadata."""
    svc = services(request)
    buf = io.BytesIO()
    with svc.db.session() as s:
        t0, t1 = resolve_range(s, range, start, end)
        if range == "all" and not (start or end):
            t0 = t1 = None
        counts = mdb.export_zip(s, buf, start=t0, end=t1, baseline=baseline or None,
                                codes=[c.strip() for c in codes.split(",") if c.strip()] or None)
        audit(s, user, "mdb.export", "", f"{counts} start={iso(t0)} end={iso(t1)} baseline={baseline} codes={codes}")
    name = f"imf_export_{utcnow():%Y%m%dT%H%M%S}.zip"
    return StreamingResponse(iter([buf.getvalue()]), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/proposals/{pid}/file")
def proposal_file(request: Request, pid: int, user: CurrentUser = Depends(require_expert)):
    with services(request).db.session() as s:
        po = s.get(ProposedOutput, pid)
    if po is None or not po.file_path or not Path(po.file_path).exists():
        raise HTTPException(404)
    return FileResponse(po.file_path, filename=Path(po.file_path).name)


@router.get("/reference/{name}")
def reference_data(request: Request, name: str, user: CurrentUser = Depends(require_guest)) -> list[dict]:
    with services(request).db.session() as s:
        rows = list(s.scalars(select(ReferenceData).where(ReferenceData.name == name)
                              .order_by(ReferenceData.dims, ReferenceData.version)))
    return [{"dims": r.dims, "version": r.version, "unit": r.unit, "source": r.source, "checksum": r.checksum,
             "entry_time": iso(r.entry_time), "author": r.author, "axis": json.loads(r.axis),
             "values": json.loads(r.values)} for r in rows]


# =============================================================================== write API (R8)
class ParameterIn(BaseModel):
    code: str
    name: str
    unit: str = ""
    group: str = "External"
    description: str = ""
    visibility: str = "restricted"
    kind: str = "scalar"
    dims: str = ""


class SampleIn(BaseModel):
    code: str
    time: str
    value: float
    dims: str = ""
    baseline: str = ""


class VectorIn(BaseModel):
    code: str
    time: str
    axis: list[float]
    values: list[float]
    dims: str = ""
    baseline: str = ""


class TimelineIn(BaseModel):
    type: str = "MANUAL"
    title: str
    start: str
    stop: str | None = None
    attributes: dict = Field(default_factory=dict)


def _entry_type(user: CurrentUser) -> str:
    return "external" if user.username.startswith("token:") else "manual"


@router.get("/v1/whoami")
def whoami(user: CurrentUser = Depends(api_writer)) -> dict:
    return {"username": user.username, "role": user.role}


@router.post("/v1/parameters", status_code=201)
def api_create_parameter(request: Request, body: ParameterIn, user: CurrentUser = Depends(api_writer)) -> dict:
    if not user.is_admin:
        raise HTTPException(403, "creating parameters requires the admin role")
    with services(request).db.session() as s:
        try:
            p = mdb.create_parameter(s, body.code, body.name, unit=body.unit, group=body.group,
                                     description=body.description, visibility=body.visibility, kind=body.kind,
                                     dims=body.dims, origin="external" if _entry_type(user) == "external" else "manual")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        audit(s, user, "parameter.create", p.code, f"kind={p.kind} via API")
        return {"code": p.code, "id": p.id}


@router.post("/v1/samples", status_code=201)
def api_samples(request: Request, body: list[SampleIn], user: CurrentUser = Depends(api_writer)) -> dict:
    svc = services(request)
    source = f"api:{user.username}"
    n, events = 0, []
    with svc.db.session() as s:
        params = {p.code: p for p in s.scalars(select(Parameter).options(selectinload(Parameter.limits))
                                                .where(Parameter.code.in_({b.code for b in body})))}
        try:
            for b in sorted(body, key=lambda x: parse_iso(x.time)):
                p = params.get(b.code)
                if p is None:
                    raise ValueError(f"unknown parameter {b.code}")
                events += mdb.add_sample(s, p, parse_iso(b.time), b.value, dims=mdb._parse_dims(b.dims),
                                         baseline=b.baseline, entry_type=_entry_type(user), source=source)
                n += 1
        except ValueError as exc:
            s.rollback()
            raise HTTPException(400, str(exc))
        svc.rules.on_limit_events(s, events)
        audit(s, user, "samples.write", ",".join(sorted(params)), f"{n} samples via API")
        s.flush()
        svc.notifier.notify(events, "RAISED")
    return {"written": n, "events": len(events)}


@router.post("/v1/vectors", status_code=201)
def api_vectors(request: Request, body: list[VectorIn], user: CurrentUser = Depends(api_writer)) -> dict:
    n = 0
    with services(request).db.session() as s:
        for b in body:
            p = s.scalar(select(Parameter).where(Parameter.code == b.code))
            if p is None or p.kind != "vector":
                raise HTTPException(400, f"unknown vector parameter {b.code}")
            try:
                mdb.add_vector(s, p, parse_iso(b.time), b.axis, b.values, dims=mdb._parse_dims(b.dims),
                               baseline=b.baseline, entry_type=_entry_type(user), source=f"api:{user.username}")
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            n += 1
        audit(s, user, "vectors.write", "", f"{n} vectors via API")
    return {"written": n}


@router.post("/v1/timeline", status_code=201)
def api_timeline(request: Request, body: list[TimelineIn], user: CurrentUser = Depends(api_writer)) -> dict:
    with services(request).db.session() as s:
        ids = []
        for b in body:
            e = tl.add_entry(s, b.type.upper(), b.title, parse_iso(b.start), parse_iso(b.stop) if b.stop else None,
                             source="external" if _entry_type(user) == "external" else "manual",
                             attributes=b.attributes, author=user.username)
            s.flush()
            ids.append(e.id)
        audit(s, user, "timeline.write", "", f"{len(ids)} entries via API")
    return {"written": len(ids), "ids": ids}
