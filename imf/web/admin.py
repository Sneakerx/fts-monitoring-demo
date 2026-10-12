"""Administration: users, limits, parameter visibility, jobs, audit log, system health."""
from __future__ import annotations

import threading

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..models import AuditLog, Limit, Parameter, PollRun, Product, Report, Sample, User
from ..security import ROLE_RANK, hash_password
from .auth import CurrentUser, require_admin
from .common import audit, flash, render, services

router = APIRouter(prefix="/admin", include_in_schema=False)


def _opt_float(v: str) -> float | None:
    v = v.strip()
    return float(v) if v else None


@router.get("")
def admin_home(request: Request, user: CurrentUser = Depends(require_admin)):
    svc = services(request)
    with svc.db.session() as s:
        users = list(s.scalars(select(User).order_by(User.username)))
        params = list(s.scalars(select(Parameter).options(selectinload(Parameter.limits))
                                .order_by(Parameter.sort_order)))
        audit_rows = list(s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(60)))
        polls = list(s.scalars(select(PollRun).order_by(PollRun.id.desc()).limit(10)))
        db_stats = {
            "products": s.scalar(select(func.count(Product.id))),
            "samples": s.scalar(select(func.count(Sample.id))),
            "reports": s.scalar(select(func.count(Report.id))),
        }
    pdgs_status = None
    try:
        base = svc.settings.pdgs.base_url.rsplit("/odata", 1)[0]
        pdgs_status = httpx.get(f"{base}/status", timeout=2).json()
    except (httpx.HTTPError, ValueError):
        pass
    jobs = [{"id": j.id, "name": j.name, "next": j.next_run_time} for j in svc.scheduler.get_jobs()] \
        if svc.scheduler.running else []
    return render(request, "admin.html", user, users=users, params=params, audit_rows=audit_rows, polls=polls,
                  db_stats=db_stats, jobs=jobs, pdgs_status=pdgs_status, roles=list(ROLE_RANK),
                  settings=svc.settings, poll_busy=svc.poller.busy)


@router.post("/users")
def create_user(request: Request, username: str = Form(...), full_name: str = Form(""), password: str = Form(...),
                role: str = Form(...), user: CurrentUser = Depends(require_admin)):
    if role not in ROLE_RANK or not username.strip() or len(password) < 4:
        flash(request, "Invalid user data (password min. 4 characters)", "error")
        return RedirectResponse("/admin#users", status_code=303)
    svc = services(request)
    with svc.db.session() as s:
        if s.scalar(select(User).where(User.username == username.strip())):
            flash(request, f"User {username} already exists", "error")
            return RedirectResponse("/admin#users", status_code=303)
        s.add(User(username=username.strip(), full_name=full_name.strip(), pw_hash=hash_password(password), role=role))
        audit(s, user, "user.create", username, f"role={role}")
    flash(request, f"User {username} created")
    return RedirectResponse("/admin#users", status_code=303)


@router.post("/users/{uid}")
def update_user(request: Request, uid: int, role: str = Form(...), active: bool = Form(False),
                password: str = Form(""), user: CurrentUser = Depends(require_admin)):
    svc = services(request)
    with svc.db.session() as s:
        u = s.get(User, uid)
        if u is None or role not in ROLE_RANK:
            raise HTTPException(400)
        if u.username == user.username and (role != "admin" or not active):
            flash(request, "You cannot demote or deactivate your own account", "error")
            return RedirectResponse("/admin#users", status_code=303)
        changes = []
        if u.role != role:
            changes.append(f"role {u.role}->{role}")
            u.role = role
        if u.active != active:
            changes.append(f"active={active}")
            u.active = active
        if password:
            u.pw_hash = hash_password(password)
            changes.append("password reset")
        if changes:
            audit(s, user, "user.update", u.username, ", ".join(changes))
    flash(request, "User updated")
    return RedirectResponse("/admin#users", status_code=303)


@router.post("/limits/{code}")
def update_limits(request: Request, code: str, warn_lo: str = Form(""), warn_hi: str = Form(""),
                  alarm_lo: str = Form(""), alarm_hi: str = Form(""), persistence: int = Form(1),
                  comment: str = Form(""), next: str = Form("/admin#limits"),
                  user: CurrentUser = Depends(require_admin)):
    svc = services(request)
    try:
        vals = dict(warn_lo=_opt_float(warn_lo), warn_hi=_opt_float(warn_hi),
                    alarm_lo=_opt_float(alarm_lo), alarm_hi=_opt_float(alarm_hi))
    except ValueError:
        flash(request, "Limits must be numbers", "error")
        return RedirectResponse(next, status_code=303)
    lo_ok = vals["alarm_lo"] is None or vals["warn_lo"] is None or vals["alarm_lo"] <= vals["warn_lo"]
    hi_ok = vals["alarm_hi"] is None or vals["warn_hi"] is None or vals["alarm_hi"] >= vals["warn_hi"]
    if not (lo_ok and hi_ok) or persistence < 1:
        flash(request, "Inconsistent limits: alarm limits must lie outside warning limits", "error")
        return RedirectResponse(next, status_code=303)
    with svc.db.session() as s:
        p = s.scalar(select(Parameter).where(Parameter.code == code))
        if p is None:
            raise HTTPException(404)
        old = p.current_limit
        s.add(Limit(parameter_id=p.id, persistence=persistence, changed_by=user.username,
                    comment=comment.strip()[:500], **vals))
        before = "none" if old is None else \
            f"W[{old.warn_lo},{old.warn_hi}] A[{old.alarm_lo},{old.alarm_hi}] P{old.persistence}"
        after = f"W[{vals['warn_lo']},{vals['warn_hi']}] A[{vals['alarm_lo']},{vals['alarm_hi']}] P{persistence}"
        audit(s, user, "limits.update", code, f"{before} -> {after}. {comment}".strip())
    flash(request, f"Limits of {code} updated (apply to new data)")
    if not next.startswith("/") or next.startswith("//"):
        next = "/admin#limits"
    return RedirectResponse(next, status_code=303)


@router.post("/visibility/{code}")
def toggle_visibility(request: Request, code: str, user: CurrentUser = Depends(require_admin)):
    svc = services(request)
    with svc.db.session() as s:
        p = s.scalar(select(Parameter).where(Parameter.code == code))
        if p is None:
            raise HTTPException(404)
        p.visibility = "restricted" if p.visibility == "public" else "public"
        audit(s, user, "parameter.visibility", code, p.visibility)
    return RedirectResponse("/admin#limits", status_code=303)


@router.post("/reports/{rid}/publish")
def toggle_publish(request: Request, rid: int, user: CurrentUser = Depends(require_admin)):
    svc = services(request)
    with svc.db.session() as s:
        rep = s.get(Report, rid)
        if rep is None:
            raise HTTPException(404)
        rep.published = not rep.published
        audit(s, user, "report.publish" if rep.published else "report.unpublish", rep.title)
    return RedirectResponse("/reports", status_code=303)


@router.post("/jobs/{job}")
def trigger_job(request: Request, job: str, user: CurrentUser = Depends(require_admin)):
    svc = services(request)
    if job == "poll":
        threading.Thread(target=svc.poller.run_once, kwargs={"trigger": f"manual:{user.username}"},
                         daemon=True).start()
        msg = "Polling triggered"
    elif job == "reports":
        threading.Thread(target=svc.reports.check_schedule, daemon=True).start()
        msg = "Periodic report check triggered"
    else:
        raise HTTPException(404)
    with svc.db.session() as s:
        audit(s, user, "job.trigger", job)
    flash(request, msg)
    return RedirectResponse("/admin#system", status_code=303)
