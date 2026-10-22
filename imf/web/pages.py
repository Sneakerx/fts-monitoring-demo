"""Portal pages: dashboard, parameters, explorer, map, timeline, products, events, reports."""
from __future__ import annotations

from datetime import timedelta
from itertools import groupby
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..models import (Event, Parameter, Product, ProductLink, ProposedOutput, Report, Sample, TimelineEntry,
                      VectorSample)
from ..monitoring import timeline as tl
from ..timeutil import parse_iso, utcnow
from .auth import CurrentUser, require_admin, require_expert, require_guest
from .common import (audit, baseline_filter, baselines, can_see, flash, latest_data_time, render, services,
                     visible_params_query)

router = APIRouter(include_in_schema=False)


def _safe_next(next_: str, default: str) -> str:
    return next_ if next_.startswith("/") and not next_.startswith("//") else default


# ---------------------------------------------------------------- dashboard
@router.get("/")
def dashboard(request: Request, user: CurrentUser = Depends(require_guest)):
    svc = services(request)
    with svc.db.session() as s:
        params = list(s.scalars(visible_params_query(user).options(
            selectinload(Parameter.states), selectinload(Parameter.limits))))
        groups = [(g, list(ps)) for g, ps in groupby(params, key=lambda p: p.group)]
        group_levels = {g: max((p.state.level if p.state else 0) for p in ps) for g, ps in groups}
        recent_events = []
        if user.is_expert:
            recent_events = list(s.scalars(select(Event).options(selectinload(Event.parameter))
                                           .where(baseline_filter(Event.baseline, svc.settings.pdgs.baseline))
                                           .order_by(Event.time.desc()).limit(8)))
        rq = select(Report)
        if not user.is_expert:
            rq = rq.where(Report.published.is_(True))
        latest_reports = list(s.scalars(rq.order_by(Report.period_end.desc(), Report.id.desc()).limit(4)))
        timeline = list(s.scalars(select(TimelineEntry).where(TimelineEntry.type.in_(
            ("DATA_GAP", "L1_GAP", "CALIBRATION_MODE", "ANOMALY", "ACTION", "BASELINE", "OPERATIONS", "MANUAL")))
            .order_by(TimelineEntry.start.desc()).limit(6))) if user.is_expert else []
    key = [c for c in ("T_DETECTOR", "ICE_LOSS", "NESR_IDX_MEAN", "SPEC_SHIFT") if any(p.code == c for p in params)]
    return render(request, "dashboard.html", user, groups=groups, group_levels=group_levels,
                  recent_events=recent_events, latest_reports=latest_reports, key_params=key, timeline=timeline)


# ---------------------------------------------------------------- parameters
@router.get("/parameters")
def parameter_list(request: Request, user: CurrentUser = Depends(require_guest)):
    svc = services(request)
    with svc.db.session() as s:
        params = list(s.scalars(visible_params_query(user).options(
            selectinload(Parameter.states), selectinload(Parameter.limits))))
        counts = dict(s.execute(select(Sample.parameter_id, func.count()).group_by(Sample.parameter_id)).all())
        counts |= dict(s.execute(select(VectorSample.parameter_id, func.count())
                                 .group_by(VectorSample.parameter_id)).all())
    groups = [(g, list(ps)) for g, ps in groupby(params, key=lambda p: p.group)]
    return render(request, "parameters.html", user, groups=groups, counts=counts)


@router.get("/parameters/{code}")
def parameter_detail(request: Request, code: str, user: CurrentUser = Depends(require_guest)):
    svc = services(request)
    with svc.db.session() as s:
        p = s.scalar(select(Parameter).options(selectinload(Parameter.limits), selectinload(Parameter.states),
                                               selectinload(Parameter.requirements))
                     .where(Parameter.code == code))
        if not can_see(user, p):
            raise HTTPException(404, "Unknown parameter")
        model = VectorSample if p.kind == "vector" else Sample
        if p.kind == "vector":
            stats = (s.scalar(select(func.count(VectorSample.id)).where(VectorSample.parameter_id == p.id)), None, None, None)
        else:
            stats = s.execute(select(func.count(Sample.id), func.min(Sample.value), func.max(Sample.value),
                                     func.avg(Sample.value)).where(Sample.parameter_id == p.id)).one()
        series = [d for (d,) in s.execute(select(model.dims).where(model.parameter_id == p.id).distinct())]
        entry_types = dict(s.execute(select(model.entry_type, func.count()).where(model.parameter_id == p.id)
                                     .group_by(model.entry_type)).all())
        events = []
        if user.is_expert:
            events = list(s.scalars(select(Event).where(Event.parameter_id == p.id)
                                    .order_by(Event.time.desc()).limit(10)))
        others = list(s.scalars(visible_params_query(user)))
        bls = baselines(s, svc.settings.pdgs.baseline)
    return render(request, "parameter.html", user, p=p, stats=stats, events=events, others=others,
                  series=sorted(series), entry_types=entry_types, baselines=bls,
                  nominal=svc.settings.pdgs.baseline)


@router.post("/parameters/{code}/annotations")
def add_annotation(request: Request, code: str, time: str = Form(...), text: str = Form(...),
                   user: CurrentUser = Depends(require_expert)):
    from ..models import Annotation
    svc = services(request)
    with svc.db.session() as s:
        p = s.scalar(select(Parameter).where(Parameter.code == code))
        if p is None:
            raise HTTPException(404)
        try:
            t = parse_iso(time)
        except ValueError:
            flash(request, "Invalid time format (use YYYY-MM-DDTHH:MM)", "error")
            return RedirectResponse(f"/parameters/{code}", status_code=303)
        s.add(Annotation(parameter_id=p.id, time=t, text=text.strip()[:500], author=user.username))
        audit(s, user, "annotation.add", code, text[:200])
    flash(request, "Annotation added")
    return RedirectResponse(f"/parameters/{code}", status_code=303)


@router.post("/parameters/{code}/samples")
def add_manual_sample(request: Request, code: str, time: str = Form(...), value: str = Form(...),
                      dims: str = Form(""), comment: str = Form(""), user: CurrentUser = Depends(require_expert)):
    """Manual MDB population (R1): a single value entered by an expert."""
    from .. import mdb
    svc = services(request)
    with svc.db.session() as s:
        p = s.scalar(select(Parameter).options(selectinload(Parameter.limits)).where(Parameter.code == code))
        if p is None:
            raise HTTPException(404)
        try:
            events = mdb.add_sample(s, p, parse_iso(time), float(value), dims=mdb._parse_dims(dims),
                                    entry_type="manual", source=f"user:{user.username} {comment}".strip())
        except ValueError as exc:
            flash(request, f"Invalid manual value: {exc}", "error")
            return RedirectResponse(f"/parameters/{code}", status_code=303)
        svc.rules.on_limit_events(s, events)
        audit(s, user, "sample.manual", code, f"{time} = {value} {dims} {comment}")
    flash(request, "Manual value stored (entry type 'manual')")
    return RedirectResponse(f"/parameters/{code}", status_code=303)


@router.get("/explorer")
def explorer(request: Request, codes: str = "", user: CurrentUser = Depends(require_guest)):
    svc = services(request)
    with svc.db.session() as s:
        params = list(s.scalars(visible_params_query(user, kind="scalar")))
        bls = baselines(s, svc.settings.pdgs.baseline)
    selected = [c for c in codes.split(",") if c] or ["LASER_DRIFT", "SPEC_SHIFT"]
    allowed = {p.code for p in params}
    selected = [c for c in selected if c in allowed] or [p.code for p in params[:2]]
    groups = [(g, list(ps)) for g, ps in groupby(params, key=lambda p: p.group)]
    return render(request, "explorer.html", user, groups=groups, selected=selected, baselines=bls)


@router.get("/map")
def map_page(request: Request, user: CurrentUser = Depends(require_guest)):
    return render(request, "map.html", user)


# ---------------------------------------------------------------- timeline
@router.get("/timeline")
def timeline_page(request: Request, type: str = "", user: CurrentUser = Depends(require_guest)):
    svc = services(request)
    with svc.db.session() as s:
        q = select(TimelineEntry)
        if type:
            q = q.where(TimelineEntry.type == type)
        if not user.is_expert:
            q = q.where(TimelineEntry.type.in_(("DATA_GAP", "L1_GAP", "CALIBRATION_MODE", "OPERATIONS", "BASELINE")))
        rows = list(s.scalars(q.order_by(TimelineEntry.start.desc()).limit(500)))
        counts = dict(s.execute(select(TimelineEntry.type, func.count()).group_by(TimelineEntry.type)).all())
    return render(request, "timeline.html", user, entries=rows, types=tl.TYPES, type=type, counts=counts)


@router.post("/timeline")
def timeline_add(request: Request, type: str = Form("MANUAL"), title: str = Form(...), start: str = Form(...),
                 stop: str = Form(""), user: CurrentUser = Depends(require_expert)):
    svc = services(request)
    try:
        t0, t1 = parse_iso(start), parse_iso(stop) if stop.strip() else None
    except ValueError:
        flash(request, "Invalid time (use YYYY-MM-DDTHH:MM)", "error")
        return RedirectResponse("/timeline", status_code=303)
    with svc.db.session() as s:
        tl.add_entry(s, type if type in tl.TYPES else "MANUAL", title.strip(), t0, t1, source="manual",
                     author=user.username)
        audit(s, user, "timeline.add", type, title[:200])
    flash(request, "Timeline entry added")
    return RedirectResponse("/timeline", status_code=303)


@router.post("/timeline/{tid}/delete")
def timeline_delete(request: Request, tid: int, user: CurrentUser = Depends(require_admin)):
    with services(request).db.session() as s:
        e = s.get(TimelineEntry, tid)
        if e is None:
            raise HTTPException(404)
        audit(s, user, "timeline.delete", e.type, f"#{tid} {e.title}")
        s.delete(e)
    flash(request, f"Timeline entry #{tid} deleted")
    return RedirectResponse("/timeline", status_code=303)


# ---------------------------------------------------------------- products
@router.get("/products")
def products(request: Request, status: str = "", type: str = "", baseline: str = "", page: int = 1,
             user: CurrentUser = Depends(require_expert)):
    svc = services(request)
    per_page = 50
    with svc.db.session() as s:
        q = select(Product)
        cq = select(func.count(Product.id))
        for cond, val in ((Product.status == status, status), (Product.product_type == type, type),
                          (Product.baseline == baseline, baseline)):
            if val:
                q, cq = q.where(cond), cq.where(cond)
        total = s.scalar(cq) or 0
        rows = list(s.scalars(q.order_by(Product.sensing_start.desc(), Product.product_type)
                              .offset((page - 1) * per_page).limit(per_page)))
        stats = dict(s.execute(select(Product.status, func.count()).group_by(Product.status)).all())
        by_type = s.execute(select(Product.product_type, Product.level, Product.baseline, func.count())
                            .group_by(Product.product_type, Product.level, Product.baseline)
                            .order_by(Product.level, Product.product_type)).all()
        avg_ms = s.scalar(select(func.avg(Product.processing_ms)).where(Product.level == "L1"))
    return render(request, "products.html", user, products=rows, total=total, page=page, per_page=per_page,
                  status=status, type=type, baseline=baseline, stats=stats, by_type=by_type, avg_ms=avg_ms)


@router.get("/products/{pid}")
def product_detail(request: Request, pid: int, user: CurrentUser = Depends(require_expert)):
    svc = services(request)
    with svc.db.session() as s:
        prod = s.get(Product, pid)
        if prod is None:
            raise HTTPException(404)
        samples = s.execute(select(Sample, Parameter).join(Parameter).where(Sample.product_id == pid)
                            .order_by(Parameter.sort_order, Sample.dims)).all()
        vectors = s.execute(select(VectorSample, Parameter).join(Parameter).where(VectorSample.product_id == pid)
                            .order_by(Parameter.sort_order, VectorSample.dims)).all()
        events = list(s.scalars(select(Event).options(selectinload(Event.parameter))
                                .where(Event.product_id == pid)))
        links = s.execute(select(ProductLink, Product).join(Product, Product.id == ProductLink.source_id)
                          .where(ProductLink.target_id == pid)).all()
        links += s.execute(select(ProductLink, Product).join(Product, Product.id == ProductLink.target_id)
                           .where(ProductLink.source_id == pid)).all()
        same = (Product.product_type == prod.product_type, Product.baseline == prod.baseline)
        prev_id = s.scalar(select(Product.id).where(*same, Product.sensing_start < prod.sensing_start)
                           .order_by(Product.sensing_start.desc()).limit(1))
        next_id = s.scalar(select(Product.id).where(*same, Product.sensing_start > prod.sensing_start)
                           .order_by(Product.sensing_start).limit(1))
    return render(request, "product.html", user, prod=prod, samples=samples, vectors=vectors, events=events,
                  links=links, prev_id=prev_id, next_id=next_id)


# ---------------------------------------------------------------- events
@router.get("/events")
def events(request: Request, status: str = "active", severity: str = "", parameter: str = "", category: str = "",
           user: CurrentUser = Depends(require_expert)):
    svc = services(request)
    with svc.db.session() as s:
        q = select(Event).options(selectinload(Event.parameter), selectinload(Event.product),
                                  selectinload(Event.proposals))
        if status == "active":
            q = q.where(Event.status != "CLOSED")
        elif status:
            q = q.where(Event.status == status)
        if severity:
            q = q.where(Event.severity == severity)
        if category:
            q = q.where(Event.category == category)
        if parameter:
            q = q.join(Parameter).where(Parameter.code == parameter)
        rows = list(s.scalars(q.order_by(Event.time.desc()).limit(300)))
        params = list(s.scalars(select(Parameter).where(Parameter.kind == "scalar").order_by(Parameter.sort_order)))
        counts = dict(s.execute(select(Event.status, func.count()).group_by(Event.status)).all())
        cat_counts = dict(s.execute(select(Event.category, func.count()).group_by(Event.category)).all())
    return render(request, "events.html", user, events=rows, status=status, severity=severity,
                  parameter=parameter, category=category, params=params, counts=counts, cat_counts=cat_counts,
                  nominal=svc.settings.pdgs.baseline)


@router.get("/events/{eid}")
def event_detail(request: Request, eid: int, user: CurrentUser = Depends(require_expert)):
    svc = services(request)
    with svc.db.session() as s:
        ev = s.scalar(select(Event).options(selectinload(Event.parameter), selectinload(Event.product),
                                            selectinload(Event.proposals)).where(Event.id == eid))
        if ev is None:
            raise HTTPException(404)
        entries = list(s.scalars(select(TimelineEntry).where(TimelineEntry.event_id == eid)))
    return render(request, "event.html", user, ev=ev, entries=entries, rules=svc.rules.rules)


@router.post("/events/{eid}/ack")
def acknowledge(request: Request, eid: int, comment: str = Form(""), next: str = Form("/events"),
                user: CurrentUser = Depends(require_expert)):
    svc = services(request)
    with svc.db.session() as s:
        ev = s.get(Event, eid)
        if ev is None:
            raise HTTPException(404)
        if ev.status == "OPEN":
            ev.status = "CLOSED" if ev.cleared_at else "ACKNOWLEDGED"
            ev.ack_by, ev.ack_at = user.username, utcnow()
            ev.comment = comment.strip()[:1000] or None
            audit(s, user, "event.acknowledge", f"event #{eid}", comment[:200])
            flash(request, f"Event #{eid} acknowledged")
    return RedirectResponse(_safe_next(next, "/events"), status_code=303)


@router.post("/proposals/{pid}/{decision}")
def review_proposal(request: Request, pid: int, decision: str, comment: str = Form(""),
                    user: CurrentUser = Depends(require_expert)):
    """Expert approval of a proposed output (value / file / telecommand). Approval is recorded only -
    implementation (e.g. processor configuration update, uplink) is performed by operations."""
    if decision not in ("approve", "reject"):
        raise HTTPException(404)
    svc = services(request)
    with svc.db.session() as s:
        po = s.get(ProposedOutput, pid)
        if po is None:
            raise HTTPException(404)
        if po.status != "DRAFT":
            flash(request, f"Proposal #{pid} was already {po.status.lower()}", "error")
        else:
            po.status = "APPROVED" if decision == "approve" else "REJECTED"
            po.reviewed_by, po.reviewed_at, po.review_comment = user.username, utcnow(), comment.strip()[:1000] or None
            audit(s, user, f"proposal.{decision}", f"proposal #{pid} ({po.rule})", f"{po.title}: {comment[:200]}")
            flash(request, f"Proposal #{pid} {po.status.lower()}")
        eid = po.event_id
    return RedirectResponse(f"/events/{eid}", status_code=303)


# ---------------------------------------------------------------- reports
@router.get("/reports")
def reports(request: Request, kind: str = "", user: CurrentUser = Depends(require_guest)):
    svc = services(request)
    with svc.db.session() as s:
        q = select(Report)
        if not user.is_expert:
            q = q.where(Report.published.is_(True))
        if kind:
            q = q.where(Report.kind == kind)
        rows = list(s.scalars(q.order_by(Report.period_end.desc(), Report.id.desc()).limit(200)))
        latest = latest_data_time(s)
    return render(request, "reports.html", user, reports=rows, kind=kind, latest=latest)


def _report_for(request: Request, rid: int, user: CurrentUser) -> Report:
    with services(request).db.session() as s:
        rep = s.get(Report, rid)
    if rep is None or (not rep.published and not user.is_expert):
        raise HTTPException(404)
    return rep


@router.get("/reports/{rid}/view")
def report_view(request: Request, rid: int, user: CurrentUser = Depends(require_guest)):
    rep = _report_for(request, rid, user)
    return HTMLResponse(Path(rep.html_path).read_text(encoding="utf-8"))


@router.get("/reports/{rid}/pdf")
def report_pdf(request: Request, rid: int, user: CurrentUser = Depends(require_guest)):
    rep = _report_for(request, rid, user)
    return FileResponse(rep.pdf_path, media_type="application/pdf", filename=Path(rep.pdf_path).name)


@router.post("/reports/generate")
def report_generate(request: Request, span: str = Form("1d"), start: str = Form(""), end: str = Form(""),
                    publish: bool = Form(False), user: CurrentUser = Depends(require_expert)):
    svc = services(request)
    with svc.db.session() as s:
        latest = latest_data_time(s)
    if latest is None:
        flash(request, "No data available yet", "error")
        return RedirectResponse("/reports", status_code=303)
    try:
        if span == "custom":
            t0, t1 = parse_iso(start), parse_iso(end)
        else:
            t1 = latest + timedelta(minutes=1)
            t0 = t1 - {"1d": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30)}[span]
    except (ValueError, KeyError):
        flash(request, "Invalid report period", "error")
        return RedirectResponse("/reports", status_code=303)
    if t1 <= t0:
        flash(request, "Report end must be after start", "error")
        return RedirectResponse("/reports", status_code=303)
    rid = svc.reports.generate("adhoc", t0, t1, created_by=user.username, publish=publish and user.is_admin)
    flash(request, "Ad-hoc report generated")
    return RedirectResponse(f"/reports?highlight={rid}", status_code=303)
