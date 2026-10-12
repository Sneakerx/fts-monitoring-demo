"""Portal pages: dashboard, parameters, explorer, products, events, reports."""
from __future__ import annotations

from datetime import timedelta
from itertools import groupby
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..models import Event, Parameter, Product, Report, Sample
from ..timeutil import parse_iso, utcnow
from .auth import CurrentUser, require_expert, require_guest
from .common import audit, can_see, flash, latest_data_time, render, services, visible_params_query

router = APIRouter(include_in_schema=False)


# ---------------------------------------------------------------- dashboard
@router.get("/")
def dashboard(request: Request, user: CurrentUser = Depends(require_guest)):
    svc = services(request)
    with svc.db.session() as s:
        params = list(s.scalars(visible_params_query(user).options(
            selectinload(Parameter.state), selectinload(Parameter.limits))))
        groups = [(g, list(ps)) for g, ps in groupby(params, key=lambda p: p.group)]
        group_levels = {g: max((p.state.level if p.state else 0) for p in ps) for g, ps in groups}
        recent_events = []
        if user.is_expert:
            recent_events = list(s.scalars(select(Event).options(selectinload(Event.parameter))
                                           .order_by(Event.time.desc()).limit(8)))
        rq = select(Report)
        if not user.is_expert:
            rq = rq.where(Report.published.is_(True))
        latest_reports = list(s.scalars(rq.order_by(Report.period_end.desc(), Report.id.desc()).limit(4)))
    key = [c for c in ("T_DETECTOR", "ICE_LOSS", "NESR_B1", "SPEC_SHIFT") if any(p.code == c for p in params)]
    return render(request, "dashboard.html", user, groups=groups, group_levels=group_levels,
                  recent_events=recent_events, latest_reports=latest_reports, key_params=key)


# ---------------------------------------------------------------- parameters
@router.get("/parameters")
def parameter_list(request: Request, user: CurrentUser = Depends(require_guest)):
    svc = services(request)
    with svc.db.session() as s:
        params = list(s.scalars(visible_params_query(user).options(
            selectinload(Parameter.state), selectinload(Parameter.limits))))
        counts = dict(s.execute(select(Sample.parameter_id, func.count()).group_by(Sample.parameter_id)).all())
    groups = [(g, list(ps)) for g, ps in groupby(params, key=lambda p: p.group)]
    return render(request, "parameters.html", user, groups=groups, counts=counts)


@router.get("/parameters/{code}")
def parameter_detail(request: Request, code: str, user: CurrentUser = Depends(require_guest)):
    svc = services(request)
    with svc.db.session() as s:
        p = s.scalar(select(Parameter).options(selectinload(Parameter.limits), selectinload(Parameter.state))
                     .where(Parameter.code == code))
        if not can_see(user, p):
            raise HTTPException(404, "Unknown parameter")
        stats = s.execute(select(func.count(Sample.id), func.min(Sample.value), func.max(Sample.value),
                                 func.avg(Sample.value)).where(Sample.parameter_id == p.id)).one()
        events = []
        if user.is_expert:
            events = list(s.scalars(select(Event).where(Event.parameter_id == p.id)
                                    .order_by(Event.time.desc()).limit(10)))
        others = list(s.scalars(visible_params_query(user)))
    return render(request, "parameter.html", user, p=p, stats=stats, events=events, others=others)


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


@router.get("/explorer")
def explorer(request: Request, codes: str = "", user: CurrentUser = Depends(require_guest)):
    svc = services(request)
    with svc.db.session() as s:
        params = list(s.scalars(visible_params_query(user)))
    selected = [c for c in codes.split(",") if c] or ["LASER_DRIFT", "SPEC_SHIFT"]
    allowed = {p.code for p in params}
    selected = [c for c in selected if c in allowed] or [p.code for p in params[:2]]
    groups = [(g, list(ps)) for g, ps in groupby(params, key=lambda p: p.group)]
    return render(request, "explorer.html", user, groups=groups, selected=selected)


# ---------------------------------------------------------------- products
@router.get("/products")
def products(request: Request, status: str = "", page: int = 1, user: CurrentUser = Depends(require_expert)):
    svc = services(request)
    per_page = 50
    with svc.db.session() as s:
        q = select(Product)
        cq = select(func.count(Product.id))
        if status:
            q, cq = q.where(Product.status == status), cq.where(Product.status == status)
        total = s.scalar(cq) or 0
        rows = list(s.scalars(q.order_by(Product.sensing_start.desc()).offset((page - 1) * per_page).limit(per_page)))
        stats = dict(s.execute(select(Product.status, func.count()).group_by(Product.status)).all())
        avg_ms = s.scalar(select(func.avg(Product.processing_ms)))
    return render(request, "products.html", user, products=rows, total=total, page=page, per_page=per_page,
                  status=status, stats=stats, avg_ms=avg_ms)


@router.get("/products/{pid}")
def product_detail(request: Request, pid: int, user: CurrentUser = Depends(require_expert)):
    svc = services(request)
    with svc.db.session() as s:
        prod = s.get(Product, pid)
        if prod is None:
            raise HTTPException(404)
        samples = s.execute(select(Sample, Parameter).join(Parameter).where(Sample.product_id == pid)
                            .order_by(Parameter.sort_order)).all()
        events = list(s.scalars(select(Event).options(selectinload(Event.parameter))
                                .where(Event.product_id == pid)))
        prev_id = s.scalar(select(Product.id).where(Product.sensing_start < prod.sensing_start)
                           .order_by(Product.sensing_start.desc()).limit(1))
        next_id = s.scalar(select(Product.id).where(Product.sensing_start > prod.sensing_start)
                           .order_by(Product.sensing_start).limit(1))
    return render(request, "product.html", user, prod=prod, samples=samples, events=events,
                  prev_id=prev_id, next_id=next_id)


# ---------------------------------------------------------------- events
@router.get("/events")
def events(request: Request, status: str = "active", severity: str = "", parameter: str = "",
           user: CurrentUser = Depends(require_expert)):
    svc = services(request)
    with svc.db.session() as s:
        q = select(Event).options(selectinload(Event.parameter), selectinload(Event.product))
        if status == "active":
            q = q.where(Event.status != "CLOSED")
        elif status:
            q = q.where(Event.status == status)
        if severity:
            q = q.where(Event.severity == severity)
        if parameter:
            q = q.join(Parameter).where(Parameter.code == parameter)
        rows = list(s.scalars(q.order_by(Event.time.desc()).limit(300)))
        params = list(s.scalars(select(Parameter).order_by(Parameter.sort_order)))
        counts = dict(s.execute(select(Event.status, func.count()).group_by(Event.status)).all())
    return render(request, "events.html", user, events=rows, status=status, severity=severity,
                  parameter=parameter, params=params, counts=counts)


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
    if not next.startswith("/") or next.startswith("//"):
        next = "/events"
    return RedirectResponse(next, status_code=303)


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
