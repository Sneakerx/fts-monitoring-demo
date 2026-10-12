"""Periodic and ad-hoc monitoring reports (self-contained HTML + PDF)."""
from __future__ import annotations

import base64
import logging
import math
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from ..config import Settings
from ..db import Database
from ..models import AuditLog, Event, Parameter, Product, Report, Sample
from ..timeutil import fmt, utcnow
from .plots import INK, group_figure, to_png

log = logging.getLogger(__name__)
TEMPLATES = Path(__file__).parent / "templates"
STATUS_RANK = {"NOMINAL": 0, "WARNING": 1, "ALARM": 2}


class ReportService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.out_dir = settings.reporting.output_dir
        self.env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
        self.env.filters["fmt"] = fmt
        self.env.filters["num"] = _num
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def collect(self, start: datetime, end: datetime) -> dict:
        with self.db.session() as s:
            params = list(s.scalars(select(Parameter).options(selectinload(Parameter.limits))
                                    .order_by(Parameter.sort_order)))
            rows = s.execute(select(Sample.parameter_id, Sample.time, Sample.value, Sample.level)
                             .where(Sample.time >= start, Sample.time < end).order_by(Sample.time)).all()
            events = list(s.scalars(
                select(Event).options(selectinload(Event.parameter))
                .where(Event.time < end, or_(Event.cleared_at.is_(None), Event.cleared_at >= start))
                .order_by(Event.time)))
            n_products = s.scalar(select(func.count(Product.id)).where(
                Product.sensing_start >= start, Product.sensing_start < end)) or 0
            n_failed = s.scalar(select(func.count(Product.id)).where(
                Product.sensing_start >= start, Product.sensing_start < end, Product.status == "FAILED")) or 0

        by_param: dict[int, list] = {p.id: [] for p in params}
        for r in rows:
            by_param.setdefault(r.parameter_id, []).append(r)

        ev_by_param: dict[int, list[Event]] = {}
        for ev in events:
            ev_by_param.setdefault(ev.parameter_id, []).append(ev)

        stats, groups = [], {}
        for p in params:
            data = by_param.get(p.id, [])
            vals = [r.value for r in data]
            status = "NOMINAL"
            for ev in ev_by_param.get(p.id, []):
                if STATUS_RANK[ev.severity] > STATUS_RANK[status]:
                    status = ev.severity
            st = {
                "param": p, "n": len(vals), "status": status,
                "min": min(vals) if vals else None, "max": max(vals) if vals else None,
                "mean": sum(vals) / len(vals) if vals else None,
                "std": _std(vals), "n_warn": sum(1 for r in data if r.level == 1),
                "n_alarm": sum(1 for r in data if r.level == 2),
            }
            stats.append(st)
            groups.setdefault(p.group, []).append({
                "code": p.code, "name": p.name, "unit": p.unit, "limit": p.current_limit,
                "times": [r.time for r in data], "values": vals, "levels": [r.level for r in data],
            })

        expected = (end - start).total_seconds() / (self.settings.mission.orbit_period_min * 60.0)
        overall = max((st["status"] for st in stats), key=STATUS_RANK.get, default="NOMINAL")
        return {
            "start": start, "end": end, "params": params, "stats": stats, "groups": groups,
            "events": events, "n_products": n_products, "n_failed": n_failed,
            "n_expected": int(round(expected)),
            "completeness": min(100.0, 100.0 * n_products / expected) if expected else 0.0,
            "overall": overall,
        }

    def generate(self, kind: str, start: datetime, end: datetime, created_by: str = "scheduler",
                 publish: bool | None = None) -> int:
        with self._lock:
            data = self.collect(start, end)
            title = _title(self.settings.mission.name, kind, start, end)
            stem = f"IMF_{kind.upper()}_{start:%Y%m%dT%H%M}_{end:%Y%m%dT%H%M}_{utcnow():%Y%m%d%H%M%S}"
            self.out_dir.mkdir(parents=True, exist_ok=True)
            html_path = self.out_dir / f"{stem}.html"
            pdf_path = self.out_dir / f"{stem}.pdf"

            figures = [(g, group_figure(g, series, start, end)) for g, series in data["groups"].items()]
            pngs = [(g, base64.b64encode(to_png(fig)).decode()) for g, fig in figures]

            html = self.env.get_template("report.html").render(
                title=title, kind=kind, mission=self.settings.mission, generated=utcnow(),
                created_by=created_by, plots=pngs, **data)
            html_path.write_text(html, encoding="utf-8")
            self._write_pdf(pdf_path, title, data, [f for _, f in figures])

            publish = self.settings.reporting.auto_publish if publish is None else publish
            with self.db.session() as s:
                rep = Report(kind=kind, title=title, period_start=start, period_end=end,
                             html_path=str(html_path), pdf_path=str(pdf_path), published=publish,
                             created_by=created_by, n_products=data["n_products"],
                             n_events=len(data["events"]), overall_status=data["overall"])
                s.add(rep)
                s.add(AuditLog(username=created_by, action="report.generate", target=title,
                               details=f"{kind} {fmt(start)} - {fmt(end)}"))
                s.flush()
                rid = rep.id
            log.info("Generated report %s", title)
            return rid

    def _write_pdf(self, path: Path, title: str, data: dict, figures: list[Figure]) -> None:
        with PdfPages(path) as pdf:
            # -- cover / summary page
            fig = Figure(figsize=(11.69, 8.27))
            fig.text(0.04, 0.94, title, fontsize=17, fontweight="bold", color=INK)
            fig.text(0.04, 0.905, f"{self.settings.mission.instrument}  ·  period {fmt(data['start'])} – "
                     f"{fmt(data['end'])} UTC  ·  generated {fmt(utcnow())} UTC", fontsize=9, color="#555")
            fig.text(0.04, 0.86, f"Overall status: {data['overall']}    Products: {data['n_products']} / "
                     f"{data['n_expected']} expected ({data['completeness']:.1f} %), failed: {data['n_failed']}"
                     f"    Events: {len(data['events'])}", fontsize=10, color=INK)
            ax = fig.add_axes((0.04, 0.05, 0.92, 0.78))
            ax.axis("off")
            cells, colors = [], []
            palette = {"NOMINAL": "#e3f3e6", "WARNING": "#fff2cc", "ALARM": "#f8d7d7"}
            for st in data["stats"]:
                p = st["param"]
                cells.append([p.group, p.code, p.unit, str(st["n"]), _num(st["min"]), _num(st["mean"]),
                              _num(st["max"]), _num(st["std"]), str(st["n_warn"]), str(st["n_alarm"]), st["status"]])
                colors.append(["white"] * 10 + [palette[st["status"]]])
            tbl = ax.table(cellText=cells, cellColours=colors, loc="upper center", cellLoc="left",
                           colLabels=["Group", "Parameter", "Unit", "N", "Min", "Mean", "Max", "Std",
                                      "#Warn", "#Alarm", "Status"])
            tbl.auto_set_font_size(False)
            tbl.set_fontsize(7.5)
            tbl.scale(1, 1.25)
            pdf.savefig(fig)
            # -- event page
            if data["events"]:
                fig = Figure(figsize=(11.69, 8.27))
                fig.text(0.04, 0.94, "Events in period", fontsize=14, fontweight="bold", color=INK)
                lines = [f"#{e.id:<5d} {e.severity:8s} {e.parameter.code:16s} {fmt(e.time)}  value {e.value:.4g} "
                         f"{e.limit_text:28s} {e.status}" for e in data["events"][:45]]
                fig.text(0.04, 0.90, "\n".join(lines), fontsize=8, family="monospace", va="top", color=INK)
                pdf.savefig(fig)
            for f in figures:
                pdf.savefig(f)

    # ------------------------------------------------------------------
    def check_schedule(self) -> list[int]:
        """Generate periodic reports for complete periods (data-time driven)."""
        cfg = self.settings.reporting
        with self.db.session() as s:
            latest = s.scalar(select(func.max(Product.sensing_stop)))
        if latest is None:
            return []
        horizon = latest - timedelta(hours=cfg.completeness_margin_h)
        created = []
        day_end = datetime(horizon.year, horizon.month, horizon.day)
        for k in range(cfg.daily_backfill):
            end = day_end - timedelta(days=k)
            created += self._ensure("daily", end - timedelta(days=1), end)
        week_end = day_end - timedelta(days=day_end.weekday())  # Monday 00:00
        for k in range(cfg.weekly_backfill):
            end = week_end - timedelta(weeks=k)
            created += self._ensure("weekly", end - timedelta(weeks=1), end)
        return created

    def _ensure(self, kind: str, start: datetime, end: datetime) -> list[int]:
        with self.db.session() as s:
            exists = s.scalar(select(Report.id).where(Report.kind == kind, Report.period_start == start,
                                                      Report.period_end == end))
            has_data = s.scalar(select(Product.id).where(Product.sensing_start >= start,
                                                         Product.sensing_start < end).limit(1))
        if exists or not has_data:
            return []
        return [self.generate(kind, start, end)]


def _title(mission: str, kind: str, start: datetime, end: datetime) -> str:
    short = re.sub(r"\s*\(.*\)", "", mission)
    if kind == "daily":
        return f"{short} IMF Daily Report {start:%Y-%m-%d}"
    if kind == "weekly":
        iso_year, week, _ = start.isocalendar()
        return f"{short} IMF Weekly Report {iso_year}-W{week:02d}"
    return f"{short} IMF Report {start:%Y-%m-%d %H:%M} – {end:%Y-%m-%d %H:%M}"


def _std(vals: list[float]) -> float | None:
    if len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def _num(v: float | None) -> str:
    if v is None:
        return "–"
    if v != 0 and (abs(v) >= 1e5 or abs(v) < 1e-3):
        return f"{v:.3e}"
    return f"{v:.4g}" if abs(v) < 1000 else f"{v:.1f}"
