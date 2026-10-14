"""Periodic and ad-hoc monitoring reports (self-contained HTML + PDF)."""
from __future__ import annotations

import base64
import json
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
from ..models import (INDEPENDENT, AuditLog, Event, Parameter, Product, ReferenceData, Report, Sample, TimelineEntry,
                      VectorSample)
from ..timeutil import fmt, utcnow
from .plots import INK, group_figure, spectrum_figure, to_png

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
    def _l1_types(self) -> list[str]:
        return [t.type for t in self.settings.pdgs.product_types if t.level == "L1"]

    def collect(self, start: datetime, end: datetime, baseline: str | None = None) -> dict:
        bl = baseline or self.settings.pdgs.baseline
        bls = [bl, INDEPENDENT]
        with self.db.session() as s:
            params = list(s.scalars(select(Parameter).options(selectinload(Parameter.limits),
                                                              selectinload(Parameter.requirements))
                                    .order_by(Parameter.sort_order)))
            rows = s.execute(select(Sample.parameter_id, Sample.dims, Sample.time, Sample.value, Sample.level)
                             .where(Sample.time >= start, Sample.time < end, Sample.baseline.in_(bls))
                             .order_by(Sample.time)).all()
            events = list(s.scalars(
                select(Event).options(selectinload(Event.parameter), selectinload(Event.proposals))
                .where(Event.time < end, or_(Event.cleared_at.is_(None), Event.cleared_at >= start),
                       Event.baseline.in_(bls))
                .order_by(Event.time)))
            l1 = (Product.sensing_start >= start, Product.sensing_start < end, Product.baseline == bl,
                  Product.product_type.in_(self._l1_types()))
            n_products = s.scalar(select(func.count(Product.id)).where(*l1)) or 0
            n_failed = s.scalar(select(func.count(Product.id)).where(*l1, Product.status == "FAILED")) or 0
            n_l0 = s.scalar(select(func.count(Product.id)).where(
                Product.sensing_start >= start, Product.sensing_start < end, Product.level == "L0")) or 0
            timeline = list(s.scalars(select(TimelineEntry).where(
                TimelineEntry.start < end, or_(TimelineEntry.stop.is_(None), TimelineEntry.stop >= start),
                TimelineEntry.baseline.in_(bls)).order_by(TimelineEntry.start)))
            spectra = self._spectra(s, start, end, bl)

        by_param: dict[int, dict[str, list]] = {p.id: {} for p in params}
        for r in rows:
            by_param.setdefault(r.parameter_id, {}).setdefault(r.dims, []).append(r)

        ev_by_param: dict[int, list[Event]] = {}
        for ev in events:
            if ev.parameter_id is not None:
                ev_by_param.setdefault(ev.parameter_id, []).append(ev)

        stats, groups, compliance = [], {}, []
        for p in params:
            if p.kind != "scalar":
                continue
            series = by_param.get(p.id, {})
            data = [r for rs in series.values() for r in rs]
            vals = [r.value for r in data]
            status = "NOMINAL"
            for ev in ev_by_param.get(p.id, []):
                if STATUS_RANK.get(ev.severity, 0) > STATUS_RANK[status]:
                    status = ev.severity
            st = {
                "param": p, "n": len(vals), "status": status, "n_series": len(series),
                "min": min(vals) if vals else None, "max": max(vals) if vals else None,
                "mean": sum(vals) / len(vals) if vals else None,
                "std": _std(vals), "n_warn": sum(1 for r in data if r.level == 1),
                "n_alarm": sum(1 for r in data if r.level == 2),
            }
            stats.append(st)
            req = p.current_requirement
            if req is not None:
                ok = sum(1 for v in vals if req.complies(v))
                compliance.append({"param": p, "req": req, "n": len(vals), "n_ok": ok,
                                   "pct": 100.0 * ok / len(vals) if vals else None,
                                   "mean": st["mean"], "std": st["std"], "min": st["min"], "max": st["max"]})
            groups.setdefault(p.group, []).append({
                "code": p.code, "name": p.name, "unit": p.unit, "limit": p.current_limit,
                "lines": [{"label": d, "times": [r.time for r in rs], "values": [r.value for r in rs],
                           "levels": [r.level for r in rs]} for d, rs in sorted(series.items())] or
                         [{"label": "", "times": [], "values": [], "levels": []}],
            })

        expected = (end - start).total_seconds() / (self.settings.mission.orbit_period_min * 60.0)
        overall = max((st["status"] for st in stats), key=STATUS_RANK.get, default="NOMINAL")
        actions = [e for e in events if e.category == "action"]
        return {
            "start": start, "end": end, "baseline": bl, "params": params, "stats": stats, "groups": groups,
            "events": events, "actions": actions, "n_products": n_products, "n_failed": n_failed, "n_l0": n_l0,
            "n_expected": int(round(expected)),
            "completeness": min(100.0, 100.0 * n_products / expected) if expected else 0.0,
            "overall": overall, "compliance": compliance, "timeline": timeline, "spectra": spectra,
        }

    @staticmethod
    def _spectra(s, start: datetime, end: datetime, baseline: str) -> list[dict]:
        """Period-mean spectral NESR per band and the latest NESR template (M9)."""
        p = s.scalar(select(Parameter).where(Parameter.code == "NESR_SPECTRAL"))
        if p is None:
            return []
        acc: dict[str, list] = {}
        for vs in s.scalars(select(VectorSample).where(VectorSample.parameter_id == p.id, VectorSample.time >= start,
                                                       VectorSample.time < end, VectorSample.baseline == baseline)):
            acc.setdefault(vs.dims, []).append(vs)
        out = []
        for dims, vss in sorted(acc.items()):
            axis = json.loads(vss[0].axis)
            vals = [json.loads(v.values) for v in vss]
            mean = [sum(col) / len(col) for col in zip(*vals)]
            band = dims.split("=", 1)[-1]
            ref = s.scalar(select(ReferenceData).where(ReferenceData.name == "NESR_TEMPLATE", ReferenceData.dims == dims)
                           .order_by(ReferenceData.version.desc()).limit(1))
            out.append({"band": band, "axis": axis, "values": mean, "n": len(vss),
                        "template_axis": json.loads(ref.axis) if ref else None,
                        "template_values": json.loads(ref.values) if ref else None,
                        "ratio": (sum(mean) / len(mean)) / (sum(json.loads(ref.values)) / len(json.loads(ref.values)))
                        if ref else None})
        return out

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
            if data["spectra"]:
                figures.append(("NESR vs. spectral template",
                                spectrum_figure("Spectral NESR vs. spectral template (period mean)",
                                                "nW/(cm²·sr·cm⁻¹)", data["spectra"])))
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
            fig.text(0.04, 0.86, f"Overall status: {data['overall']}    L1 products ({data['baseline']}): "
                     f"{data['n_products']} / {data['n_expected']} expected ({data['completeness']:.1f} %), failed: "
                     f"{data['n_failed']}    L0 products: {data['n_l0']}    Events: {len(data['events'])}",
                     fontsize=10, color=INK)
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
                lines = [f"#{e.id:<5d} {e.severity:8s} {e.category:8s} {_short(e.label, 34):34s} {fmt(e.time)}  "
                         f"{_short(e.limit_text or (e.rule or ''), 26):26s} {e.status}" for e in data["events"][:45]]
                fig.text(0.04, 0.90, "\n".join(lines), fontsize=8, family="monospace", va="top", color=INK)
                pdf.savefig(fig)
            # -- requirements, actions and timeline page
            if data["compliance"] or data["actions"] or data["timeline"]:
                fig = Figure(figsize=(11.69, 8.27))
                fig.text(0.04, 0.94, "Requirement compliance, actions and mission timeline", fontsize=14,
                         fontweight="bold", color=INK)
                lines = ["REQUIREMENT COMPLIANCE"]
                lines += [f"  {c['param'].code:16s} {c['req'].text:34s} {c['n_ok']:5d}/{c['n']:<5d} "
                          f"{_num(c['pct'])} %  mean {_num(c['mean'])}  std {_num(c['std'])}" for c in data["compliance"]]
                lines += ["", "ACTIONS (proposed outputs await expert approval)"]
                for e in data["actions"][:12]:
                    lines.append(f"  #{e.id:<5d} {e.rule or '':22s} {_short(e.recommended_action or e.title, 90)}")
                    for po in e.proposals:
                        lines.append(f"         -> {po.kind:11s} {_short(po.title, 50):50s} {po.status}")
                lines += ["", "MISSION TIMELINE"]
                lines += [f"  {fmt(t.start)} – {fmt(t.stop) if t.stop else 'ongoing':16s} {t.type:16s} {_short(t.title, 70)}"
                          for t in data["timeline"][:20]]
                fig.text(0.04, 0.90, "\n".join(lines), fontsize=7.5, family="monospace", va="top", color=INK)
                pdf.savefig(fig)
            for f in figures:
                pdf.savefig(f)

    # ------------------------------------------------------------------
    def check_schedule(self) -> list[int]:
        """Generate periodic reports for complete periods (data-time driven)."""
        cfg = self.settings.reporting
        with self.db.session() as s:
            latest = s.scalar(select(func.max(Product.sensing_stop)).where(
                Product.product_type.in_(self._l1_types()), Product.baseline == self.settings.pdgs.baseline))
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
            has_data = s.scalar(select(Product.id).where(Product.sensing_start >= start, Product.sensing_start < end,
                                                         Product.product_type.in_(self._l1_types())).limit(1))
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


def _short(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


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
