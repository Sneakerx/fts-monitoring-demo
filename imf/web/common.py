"""Shared helpers for the web layer."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import INDEPENDENT, AuditLog, Parameter, Product, Sample
from ..reporting.report import _num
from ..services import Services
from ..timeutil import fmt, iso, parse_iso
from .auth import CurrentUser

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["fmt"] = fmt
TEMPLATES.env.filters["fmts"] = lambda d: fmt(d, True)
TEMPLATES.env.filters["num"] = _num
TEMPLATES.env.filters["iso"] = iso

LEVEL_CLASS = {0: "NOMINAL", 1: "WARNING", 2: "ALARM"}
TEMPLATES.env.globals["LEVEL_CLASS"] = LEVEL_CLASS
TEMPLATES.env.filters["fromjson"] = lambda s: json.loads(s) if s else {}

RANGES = {"1d": timedelta(days=1), "7d": timedelta(days=7), "14d": timedelta(days=14), "30d": timedelta(days=30),
          "90d": timedelta(days=90), "all": None}


def services(request: Request) -> Services:
    return request.app.state.services


def render(request: Request, name: str, user: CurrentUser | None, **ctx) -> object:
    svc = services(request)
    flash = request.session.pop("flash", None)
    return TEMPLATES.TemplateResponse(request, name, {
        "user": user, "mission": svc.settings.mission, "flash": flash, "path": request.url.path, **ctx})


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def audit(s: Session, user: CurrentUser | str, action: str, target: str = "", details: str = "") -> None:
    name = user if isinstance(user, str) else user.username
    s.add(AuditLog(username=name, action=action, target=target, details=details))


def visible_params_query(user: CurrentUser, kind: str | None = None):
    q = select(Parameter).order_by(Parameter.sort_order)
    if not user.is_expert:
        q = q.where(Parameter.visibility == "public")
    if kind:
        q = q.where(Parameter.kind == kind)
    return q


def baselines(s: Session, nominal: str) -> list[str]:
    """Processing baselines present in the MDB, nominal first."""
    found = [b for (b,) in s.execute(select(Product.baseline).where(Product.baseline != INDEPENDENT).distinct())]
    return [nominal] + sorted(b for b in found if b != nominal)


def baseline_filter(col, baseline: str):
    """Baseline view: the baseline's own data plus baseline-independent data (L0, manual)."""
    return col.in_([baseline, INDEPENDENT])


def can_see(user: CurrentUser, p: Parameter | None) -> bool:
    return p is not None and (user.is_expert or p.visibility == "public")


def latest_data_time(s: Session) -> datetime | None:
    return s.scalar(select(func.max(Sample.time)))


def resolve_range(s: Session, rng: str | None, start: str | None, end: str | None) -> tuple[datetime | None, datetime | None]:
    """Time windows are anchored at the latest data time (not wall clock)."""
    if start or end:
        return (parse_iso(start) if start else None, parse_iso(end) if end else None)
    anchor = latest_data_time(s)
    span = RANGES.get(rng or "30d", RANGES["30d"])
    if anchor is None or span is None:
        return None, None
    return anchor - span, anchor + timedelta(hours=1)
