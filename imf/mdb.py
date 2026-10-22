"""Monitoring-database management: manual population and imports (R1), purge (R4), export (R5),
backup / restore / check / repair (R11) and API tokens for external tools (R8).

All functions work on a session (web, API) or a `Database` (CLI) and are audited by the callers.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import secrets
import shutil
import sqlite3
import subprocess
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, delete, distinct, exists, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from .db import Database, get_state, set_state
from .migrate import current_revision, head_revision
from .models import (INDEPENDENT, LEVEL_NAMES, ApiToken, Event, Parameter, ParameterState, Product, ProductLink,
                     ProposedOutput, ReferenceData, Report, Sample, SystemState, TimelineEntry, VectorSample)
from .monitoring import timeline
from .monitoring.limits import advance_state, check_sample, classify
from .processing.pipeline import compact
from .processing.registry import dims as make_dims
from .timeutil import iso, parse_iso, utcnow

log = logging.getLogger(__name__)
SOURCE_ENTRY_TYPES = ("manual", "imported", "external")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rows(data: bytes | str) -> list[dict]:
    text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


@dataclass
class ImportResult:
    n: int = 0
    errors: list[str] = field(default_factory=list)
    new_events: list[Event] = field(default_factory=list)

    def summary(self) -> str:
        return f"{self.n} imported" + (f", {len(self.errors)} errors: " + "; ".join(self.errors[:5]) if self.errors else "")


# =============================================================================== parameters
def create_parameter(s: Session, code: str, name: str, *, unit: str = "", group: str = "Manual",
                     description: str = "", visibility: str = "restricted", kind: str = "scalar", dims: str = "",
                     origin: str = "manual") -> Parameter:
    code = code.strip().upper()
    if not code or not code.replace("_", "").isalnum():
        raise ValueError("Parameter code must be alphanumeric (A-Z, 0-9, _)")
    if s.scalar(select(Parameter.id).where(Parameter.code == code)):
        raise ValueError(f"Parameter {code} already exists")
    if kind not in ("scalar", "vector") or visibility not in ("public", "restricted"):
        raise ValueError("Invalid kind or visibility")
    order = (s.scalar(select(func.max(Parameter.sort_order))) or 0) + 1
    p = Parameter(code=code, name=name.strip() or code, unit=unit.strip(), group=group.strip() or "Manual",
                  description=description.strip(), visibility=visibility, kind=kind, dims=dims.strip(),
                  origin=origin, algorithm=origin, sort_order=order)
    s.add(p)
    s.flush()
    return p


def add_sample(s: Session, p: Parameter, time: datetime, value: float, *, dims: str = "", baseline: str = "",
               entry_type: str = "manual", source: str = "") -> list[Event]:
    """Store a manual / imported / external sample. Samples newer than the series state are limit-checked
    (persistence, events); historic samples are only classified so the running state stays consistent."""
    if entry_type not in SOURCE_ENTRY_TYPES:
        raise ValueError(f"entry type must be one of {SOURCE_ENTRY_TYPES}")
    if p.kind != "scalar":
        raise ValueError(f"{p.code} is a vector parameter")
    st = s.scalar(select(ParameterState).where(ParameterState.parameter_id == p.id,
                                               ParameterState.baseline == baseline, ParameterState.series == dims))
    events: list[Event] = []
    if st is None or st.last_time is None or time >= st.last_time:
        chk = check_sample(s, p, value, time, None, baseline, dims)
        level, events = chk.level, chk.new_events
    else:
        level = classify(value, p.current_limit)[0]
    s.add(Sample(parameter_id=p.id, product_id=None, time=time, value=value, level=level, dims=dims,
                 baseline=baseline, entry_type=entry_type, source=source[:128], entry_time=utcnow()))
    return events


def add_vector(s: Session, p: Parameter, time: datetime, axis: list[float], values: list[float], *, dims: str = "",
               baseline: str = "", entry_type: str = "manual", source: str = "") -> VectorSample:
    if len(axis) != len(values) or not axis:
        raise ValueError("axis and values must have the same, non-zero length")
    vs = VectorSample(parameter_id=p.id, product_id=None, time=time, dims=dims, baseline=baseline,
                      axis=compact(axis), values=compact(values), entry_type=entry_type, source=source[:128],
                      entry_time=utcnow())
    s.add(vs)
    return vs


def _parse_dims(raw: str | None) -> str:
    """'sb=3;band=B1' or 'sb=3,band=B1' -> normalised key."""
    if not raw or not raw.strip():
        return ""
    parts = dict(p.split("=", 1) for p in raw.replace(",", ";").split(";") if "=" in p)
    return make_dims(**{k.strip(): v.strip() for k, v in parts.items()})


def import_samples_csv(s: Session, data: bytes | str, filename: str, author: str,
                       entry_type: str = "imported") -> ImportResult:
    """CSV columns: time, code, value [, dims] [, baseline]. Parameters must exist."""
    raw = data.encode() if isinstance(data, str) else data
    source = f"file:{Path(filename).name}#{sha256(raw)[:12]} ({author})"
    res = ImportResult()
    params = {p.code: p for p in s.scalars(select(Parameter).options(selectinload(Parameter.limits)))}
    rows = []
    for i, r in enumerate(_rows(raw), start=2):
        try:
            p = params[(r.get("code") or "").strip().upper()]
            rows.append((parse_iso(r["time"].strip()), p, float(r["value"]), _parse_dims(r.get("dims")),
                         (r.get("baseline") or "").strip()))
        except KeyError as exc:
            res.errors.append(f"line {i}: unknown parameter or missing column {exc}")
        except ValueError as exc:
            res.errors.append(f"line {i}: {exc}")
    for t, p, v, d, bl in sorted(rows, key=lambda x: x[0]):
        try:
            res.new_events += add_sample(s, p, t, v, dims=d, baseline=bl, entry_type=entry_type, source=source)
            res.n += 1
        except ValueError as exc:
            res.errors.append(f"{p.code} {iso(t)}: {exc}")
    return res


def import_timeline_csv(s: Session, data: bytes | str, filename: str, author: str) -> ImportResult:
    """CSV columns: type, title, start [, stop] - e.g. the mission timeline maintained by operations."""
    raw = data.encode() if isinstance(data, str) else data
    res = ImportResult()
    for i, r in enumerate(_rows(raw), start=2):
        try:
            typ = (r.get("type") or "MANUAL").strip().upper()
            stop = (r.get("stop") or "").strip()
            timeline.add_entry(s, typ, r["title"].strip(), parse_iso(r["start"].strip()),
                               parse_iso(stop) if stop else None, source="imported", author=author,
                               attributes={"file": Path(filename).name, "sha256": sha256(raw)[:12]})
            res.n += 1
        except (KeyError, ValueError, AttributeError) as exc:
            res.errors.append(f"line {i}: {exc}")
    return res


def import_reference_csv(s: Session, path_or_data: Path | bytes | str, name: str, *, unit: str = "",
                         description: str = "", author: str = "system", filename: str | None = None
                         ) -> list[ReferenceData]:
    """CSV columns: [dim columns...,] axis, value. One reference record (new version) per dims combination."""
    if isinstance(path_or_data, Path):
        raw, filename = path_or_data.read_bytes(), filename or path_or_data.name
    else:
        raw = path_or_data.encode() if isinstance(path_or_data, str) else path_or_data
    rows = _rows(raw)
    if not rows:
        raise ValueError("empty reference file")
    cols = list(rows[0].keys())
    if len(cols) < 2:
        raise ValueError("reference file needs at least an axis and a value column")
    dim_cols, axis_col, val_col = cols[:-2], cols[-2], cols[-1]
    groups: dict[str, tuple[list, list]] = {}
    for r in rows:
        key = make_dims(**{c: r[c].strip() for c in dim_cols}) if dim_cols else ""
        ax, vals = groups.setdefault(key, ([], []))
        ax.append(float(r[axis_col]))
        vals.append(float(r[val_col]))
    out = []
    checksum = sha256(raw)
    for key, (ax, vals) in groups.items():
        version = (s.scalar(select(func.max(ReferenceData.version)).where(ReferenceData.name == name,
                                                                          ReferenceData.dims == key)) or 0) + 1
        ref = ReferenceData(name=name, dims=key, version=version, description=description, unit=unit,
                            axis=compact(ax), values=compact(vals), source=f"file:{filename}", checksum=checksum,
                            author=author, entry_time=utcnow())
        s.add(ref)
        out.append(ref)
    s.flush()
    return out


def reference(s: Session, name: str, dims: str = "") -> ReferenceData | None:
    """Latest version of a reference data record."""
    return s.scalar(select(ReferenceData).where(ReferenceData.name == name, ReferenceData.dims == dims)
                    .order_by(ReferenceData.version.desc()).limit(1))


# =============================================================================== purge
@dataclass
class PurgeFilter:
    baseline: str | None = None
    product_type: str | None = None
    parameter: str | None = None
    start: datetime | None = None
    end: datetime | None = None

    def describe(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in self.__dict__.items() if v is not None) or "everything"


def purge(s: Session, f: PurgeFilter, dry_run: bool = True, reset_watermarks: bool = False) -> dict:
    """Remove part of the MDB. Product-based filters remove products with everything derived from them;
    a parameter filter removes the parameter's samples / vectors / events in the period.
    Returns counts; with dry_run nothing is changed."""
    if f.parameter is None and f.product_type is None and f.baseline is None and f.start is None and f.end is None:
        raise ValueError("Refusing to purge without any filter")
    counts: dict[str, int] = {}
    param_ids: list[int] | None = None
    if f.parameter:
        pid = s.scalar(select(Parameter.id).where(Parameter.code == f.parameter))
        if pid is None:
            raise ValueError(f"Unknown parameter {f.parameter}")
        param_ids = [pid]

    def time_filter(col):
        conds = []
        if f.start:
            conds.append(col >= f.start)
        if f.end:
            conds.append(col < f.end)
        return conds

    # products selected by the filter (removed unless this is a parameter purge)
    product_ids = select(Product.id).where(*time_filter(Product.sensing_start))
    if f.product_type:
        product_ids = product_ids.where(Product.product_type == f.product_type)
    if f.baseline is not None:
        product_ids = product_ids.where(Product.baseline == f.baseline)

    sample_conds, vector_conds, event_conds = [], [], []
    if param_ids is not None:
        sample_conds.append(Sample.parameter_id.in_(param_ids))
        vector_conds.append(VectorSample.parameter_id.in_(param_ids))
        event_conds.append(Event.parameter_id.in_(param_ids))
        sample_conds += time_filter(Sample.time)
        vector_conds += time_filter(VectorSample.time)
        event_conds += time_filter(Event.time)
        if f.baseline is not None:
            sample_conds.append(Sample.baseline == f.baseline)
            vector_conds.append(VectorSample.baseline == f.baseline)
            event_conds.append(Event.baseline == f.baseline)
        if f.product_type:
            sample_conds.append(Sample.product_id.in_(product_ids))
            vector_conds.append(VectorSample.product_id.in_(product_ids))
            event_conds.append(Event.product_id.in_(product_ids))
    else:
        derived = [Sample.product_id.in_(product_ids)]
        dv = [VectorSample.product_id.in_(product_ids)]
        de = [Event.product_id.in_(product_ids)]
        if not f.product_type:  # results without product (analyses, manual) in the same period / baseline
            extra = [Sample.product_id.is_(None), *time_filter(Sample.time)]
            extra_v = [VectorSample.product_id.is_(None), *time_filter(VectorSample.time)]
            extra_e = [Event.product_id.is_(None), Event.category != "timeline", *time_filter(Event.time)]
            if f.baseline is not None:
                extra.append(Sample.baseline == f.baseline)
                extra_v.append(VectorSample.baseline == f.baseline)
                extra_e.append(Event.baseline == f.baseline)
            derived = [or_(derived[0], and_(*extra))]
            dv = [or_(dv[0], and_(*extra_v))]
            de = [or_(de[0], and_(*extra_e))]
        sample_conds, vector_conds, event_conds = derived, dv, de

    event_ids = select(Event.id).where(*event_conds)
    counts["samples"] = s.scalar(select(func.count(Sample.id)).where(*sample_conds)) or 0
    counts["vector_samples"] = s.scalar(select(func.count(VectorSample.id)).where(*vector_conds)) or 0
    counts["events"] = s.scalar(select(func.count(Event.id)).where(*event_conds)) or 0
    counts["proposed_outputs"] = s.scalar(select(func.count(ProposedOutput.id))
                                          .where(ProposedOutput.event_id.in_(event_ids))) or 0
    counts["products"] = 0 if param_ids is not None else (s.scalar(
        select(func.count()).select_from(product_ids.subquery())) or 0)
    if dry_run:
        return counts

    affected_params = [r for (r,) in s.execute(select(distinct(Sample.parameter_id)).where(*sample_conds))]
    if param_ids is None and reset_watermarks:
        _reset_watermarks(s, product_ids)
    s.execute(delete(ProposedOutput).where(ProposedOutput.event_id.in_(event_ids)))
    s.execute(update(TimelineEntry).where(TimelineEntry.event_id.in_(event_ids)).values(event_id=None))
    s.execute(delete(Event).where(Event.id.in_(event_ids)))
    s.execute(delete(Sample).where(*sample_conds))
    s.execute(delete(VectorSample).where(*vector_conds))
    if param_ids is None:
        ids = [r for (r,) in s.execute(product_ids)]
        for chunk in range(0, len(ids), 500):
            part = ids[chunk:chunk + 500]
            s.execute(update(TimelineEntry).where(TimelineEntry.product_id.in_(part)).values(product_id=None))
            s.execute(delete(ProductLink).where(or_(ProductLink.source_id.in_(part), ProductLink.target_id.in_(part))))
            s.execute(update(Event).where(Event.product_id.in_(part)).values(product_id=None))
            s.execute(delete(Product).where(Product.id.in_(part)))
    s.flush()
    counts["states_rebuilt"] = rebuild_states(s, affected_params)
    return counts


def _reset_watermarks(s: Session, product_ids) -> None:
    """Let the poller / analyses re-ingest and re-analyse the purged period."""
    rows = s.execute(select(Product.product_type, Product.campaign_id, func.min(Product.publication_date),
                            func.min(Product.sensing_start)).where(Product.id.in_(product_ids))
                     .group_by(Product.product_type, Product.campaign_id)).all()
    for ptype, cid, pub, sensing in rows:
        key = f"campaign.{cid}.watermark" if cid else f"poll.watermark.{ptype}"
        wm = get_state(s, key)
        new = pub - timedelta(milliseconds=1)
        if wm is None or parse_iso(wm) > new:
            set_state(s, key, iso(new))
        day = datetime(sensing.year, sensing.month, sensing.day)
        for row in s.scalars(select(SystemState).where(SystemState.key.like("analysis.%"))):
            if parse_iso(row.value) > day:
                row.value = iso(day)


# =============================================================================== repair
def rebuild_states(s: Session, param_ids: list[int] | None = None) -> int:
    """Rebuild the persistence state of every series by replaying its samples with the current limits."""
    q = select(Parameter).options(selectinload(Parameter.limits))
    if param_ids is not None:
        if not param_ids:
            return 0
        q = q.where(Parameter.id.in_(param_ids))
    n = 0
    for p in s.scalars(q):
        s.execute(delete(ParameterState).where(ParameterState.parameter_id == p.id))
        limit = p.current_limit
        persistence = max(1, limit.persistence if limit else 1)
        states: dict[tuple[str, str], ParameterState] = {}
        for bl, d, t, v in s.execute(select(Sample.baseline, Sample.dims, Sample.time, Sample.value)
                                     .where(Sample.parameter_id == p.id).order_by(Sample.time)):
            st = states.get((bl, d))
            if st is None:
                st = states[(bl, d)] = ParameterState(parameter_id=p.id, baseline=bl, series=d, level=0,
                                                      candidate_level=0, candidate_count=0)
            advance_state(st, classify(v, limit)[0], persistence)
            st.last_value, st.last_time = v, t
        s.add_all(states.values())
        n += len(states)
    s.flush()
    return n


def reconcile_events(s: Session) -> dict:
    """Make open limit events consistent with the (rebuilt) series states."""
    closed = opened = 0
    rank = {"WARNING": 1, "ALARM": 2}
    for st in s.scalars(select(ParameterState)):
        open_ev = list(s.scalars(select(Event).where(
            Event.parameter_id == st.parameter_id, Event.baseline == st.baseline, Event.dims == st.series,
            Event.limit_text != "", Event.cleared_at.is_(None))))
        for ev in open_ev:
            if rank.get(ev.severity, 0) > st.level:
                ev.cleared_at = st.last_time
                ev.status = "CLOSED" if ev.status == "ACKNOWLEDGED" else ev.status
                ev.comment = ((ev.comment or "") + " [cleared by MDB repair]").strip()
                closed += 1
        if st.level > 0 and not any(rank.get(ev.severity, 0) >= st.level and ev.cleared_at is None for ev in open_ev):
            p = s.get(Parameter, st.parameter_id)
            s.add(Event(parameter_id=p.id, severity=LEVEL_NAMES[st.level], time=st.last_time or utcnow(),
                        value=st.last_value, limit_text="(state after MDB repair)", last_time=st.last_time,
                        last_value=st.last_value, worst_value=st.last_value, n_violations=1, status="OPEN",
                        baseline=st.baseline, dims=st.series, category="limit",
                        title=f"{p.name} level {LEVEL_NAMES[st.level]} after MDB repair",
                        comment="opened by MDB repair"))
            opened += 1
    return {"events_cleared": closed, "events_opened": opened}


def check(db: Database) -> dict:
    """Consistency check of the MDB (read-only)."""
    out: dict = {"checked_at": iso(utcnow())}
    with db.engine.connect() as conn:
        if db.is_sqlite:
            out["integrity"] = conn.exec_driver_sql("PRAGMA integrity_check").scalar()
    out["schema_revision"] = current_revision(db.engine)
    out["schema_head"] = head_revision()
    with db.session() as s:
        out["orphan_samples"] = s.scalar(select(func.count(Sample.id)).where(
            Sample.product_id.is_not(None), ~exists().where(Product.id == Sample.product_id))) or 0
        out["orphan_vector_samples"] = s.scalar(select(func.count(VectorSample.id)).where(
            VectorSample.product_id.is_not(None), ~exists().where(Product.id == VectorSample.product_id))) or 0
        out["orphan_links"] = s.scalar(select(func.count(ProductLink.id)).where(
            ~exists().where(Product.id == ProductLink.source_id) | ~exists().where(Product.id == ProductLink.target_id))) or 0
        latest = {(r[0], r[1], r[2]): r[3] for r in s.execute(
            select(Sample.parameter_id, Sample.baseline, Sample.dims, func.max(Sample.time))
            .group_by(Sample.parameter_id, Sample.baseline, Sample.dims))}
        states = {(st.parameter_id, st.baseline, st.series): st.last_time for st in s.scalars(select(ParameterState))}
        out["series_without_state"] = sum(1 for k in latest if k not in states)
        out["stale_states"] = sum(1 for k, t in latest.items() if k in states and states[k] is not None and states[k] < t)
        out["missing_report_files"] = sum(1 for r in s.scalars(select(Report))
                                          if not Path(r.html_path).exists() or not Path(r.pdf_path).exists())
        out["missing_output_files"] = sum(1 for po in s.scalars(select(ProposedOutput).where(
            ProposedOutput.file_path.is_not(None))) if not Path(po.file_path).exists())
        out["counts"] = {name: s.scalar(select(func.count()).select_from(m)) for name, m in (
            ("products", Product), ("samples", Sample), ("vector_samples", VectorSample), ("events", Event),
            ("timeline_entries", TimelineEntry), ("reference_data", ReferenceData))}
    problems = [k for k in ("orphan_samples", "orphan_vector_samples", "orphan_links", "series_without_state",
                            "stale_states") if out[k]]
    if out.get("integrity", "ok") != "ok":
        problems.append("integrity")
    if out["schema_revision"] != out["schema_head"]:
        problems.append("schema_revision")
    out["problems"] = problems
    out["ok"] = not problems
    return out


def repair(db: Database) -> dict:
    """Remove orphans, rebuild all series states and reconcile open events with them."""
    out: dict = {}
    with db.session() as s:
        out["orphan_samples_removed"] = s.execute(delete(Sample).where(
            Sample.product_id.is_not(None), ~exists().where(Product.id == Sample.product_id))).rowcount
        out["orphan_vectors_removed"] = s.execute(delete(VectorSample).where(
            VectorSample.product_id.is_not(None), ~exists().where(Product.id == VectorSample.product_id))).rowcount
        out["orphan_links_removed"] = s.execute(delete(ProductLink).where(
            ~exists().where(Product.id == ProductLink.source_id) | ~exists().where(Product.id == ProductLink.target_id))).rowcount
        out["states_rebuilt"] = rebuild_states(s)
        out |= reconcile_events(s)
    return out


# =============================================================================== backup / restore
def _sqlite_path(db: Database) -> Path:
    return Path(db.engine.url.database)


def backup(db: Database, out_dir: Path) -> Path:
    """Online backup (SQLite backup API / pg_dump) plus a JSON sidecar with revision, counts and checksum."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%S")
    if db.is_sqlite:
        target = out_dir / f"imf_{stamp}.db"
        raw = db.engine.raw_connection()
        try:
            dst = sqlite3.connect(target)
            with dst:
                raw.driver_connection.backup(dst)
            dst.close()
        finally:
            raw.close()
    else:
        target = out_dir / f"imf_{stamp}.dump"
        url = db.engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
        subprocess.run(["pg_dump", "-Fc", "-f", str(target), url], check=True)
    meta = {"created": iso(utcnow()), "file": target.name, "sha256": sha256(target.read_bytes()),
            "schema_revision": current_revision(db.engine)}
    with db.session() as s:
        meta["counts"] = {"products": s.scalar(select(func.count(Product.id))),
                          "samples": s.scalar(select(func.count(Sample.id)))}
    target.with_suffix(target.suffix + ".json").write_text(json.dumps(meta, indent=2))
    return target


def list_backups(out_dir: Path) -> list[dict]:
    if not out_dir.exists():
        return []
    out = []
    for f in sorted(out_dir.glob("imf_*.*"), reverse=True):
        if f.suffix == ".json":
            continue
        meta_file = f.with_suffix(f.suffix + ".json")
        meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
        out.append({"name": f.name, "size": f.stat().st_size, **meta})
    return out


def restore(db_url: str, backup_file: Path) -> None:
    """Replace the MDB with a backup (the IMF services must be stopped). The backup is verified first."""
    if not backup_file.exists():
        raise FileNotFoundError(backup_file)
    meta_file = backup_file.with_suffix(backup_file.suffix + ".json")
    if meta_file.exists():
        expected = json.loads(meta_file.read_text()).get("sha256")
        if expected and expected != sha256(backup_file.read_bytes()):
            raise ValueError("Backup checksum mismatch - file corrupted")
    if db_url.startswith("sqlite"):
        con = sqlite3.connect(backup_file)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup failed the SQLite integrity check")
        finally:
            con.close()
        target = Path(db_url.split("///", 1)[1])
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        shutil.copyfile(backup_file, target)
    else:
        subprocess.run(["pg_restore", "--clean", "--if-exists", "-d", db_url, str(backup_file)], check=True)


# =============================================================================== export
def export_zip(s: Session, out: io.BufferedIOBase | Path, *, start: datetime | None = None,
               end: datetime | None = None, baseline: str | None = None, codes: list[str] | None = None) -> dict:
    """Export (part of) the MDB as a ZIP of CSV / JSON-lines files with a metadata sidecar."""
    def tf(col):
        return [c for c in ((col >= start) if start else None, (col < end) if end else None) if c is not None]

    params = list(s.scalars(select(Parameter).options(selectinload(Parameter.limits),
                                                      selectinload(Parameter.requirements))
                            .order_by(Parameter.sort_order)))
    if codes:
        params = [p for p in params if p.code in codes]
    pid = {p.id: p for p in params}
    bl_cond = (lambda col: [col.in_([baseline, INDEPENDENT])]) if baseline is not None else (lambda col: [])
    counts = {}
    target = out if not isinstance(out, Path) else open(out, "wb")
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["time_utc", "code", "dims", "baseline", "value", "level", "entry_type", "source",
                        "entry_time_utc", "product"])
            n = 0
            q = select(Sample, Product.name).outerjoin(Product).where(
                Sample.parameter_id.in_(list(pid)), *tf(Sample.time), *bl_cond(Sample.baseline)).order_by(Sample.time)
            for smp, pname in s.execute(q):
                w.writerow([iso(smp.time), pid[smp.parameter_id].code, smp.dims, smp.baseline, f"{smp.value:.9g}",
                            LEVEL_NAMES[smp.level], smp.entry_type, smp.source, iso(smp.entry_time), pname or ""])
                n += 1
            z.writestr("samples.csv", buf.getvalue())
            counts["samples"] = n

            lines = []
            for vs in s.scalars(select(VectorSample).where(VectorSample.parameter_id.in_(list(pid)),
                                                           *tf(VectorSample.time), *bl_cond(VectorSample.baseline))
                                .order_by(VectorSample.time)):
                lines.append(json.dumps({"time": iso(vs.time), "code": pid[vs.parameter_id].code, "dims": vs.dims,
                                         "baseline": vs.baseline, "axis": json.loads(vs.axis),
                                         "values": json.loads(vs.values), "entry_type": vs.entry_type,
                                         "source": vs.source, "product_id": vs.product_id}))
            z.writestr("vector_samples.jsonl", "\n".join(lines))
            counts["vector_samples"] = len(lines)

            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["id", "category", "severity", "parameter", "dims", "baseline", "rule", "title", "time_utc",
                        "value", "limit", "cleared_utc", "status", "ack_by", "comment", "recommended_action"])
            evq = select(Event).options(selectinload(Event.parameter)).where(*tf(Event.time), *bl_cond(Event.baseline))
            n = 0
            for e in s.scalars(evq.order_by(Event.time)):
                if codes and (e.parameter is None or e.parameter.code not in codes):
                    continue
                w.writerow([e.id, e.category, e.severity, e.parameter.code if e.parameter else "", e.dims, e.baseline,
                            e.rule or "", e.title, iso(e.time), e.value, e.limit_text, iso(e.cleared_at), e.status,
                            e.ack_by or "", e.comment or "", e.recommended_action or ""])
                n += 1
            z.writestr("events.csv", buf.getvalue())
            counts["events"] = n

            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["id", "type", "title", "start_utc", "stop_utc", "source", "rule", "baseline", "author",
                        "entry_time_utc", "attributes"])
            n = 0
            for t in s.scalars(select(TimelineEntry).where(*tf(TimelineEntry.start)).order_by(TimelineEntry.start)):
                w.writerow([t.id, t.type, t.title, iso(t.start), iso(t.stop), t.source, t.rule or "", t.baseline,
                            t.author, iso(t.entry_time), t.attributes])
                n += 1
            z.writestr("timeline.csv", buf.getvalue())
            counts["timeline"] = n

            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["pdgs_id", "name", "type", "level", "baseline", "processor_version", "orbit", "sensing_start",
                        "sensing_stop", "publication", "status", "sha256", "error"])
            n = 0
            pq = select(Product).where(*tf(Product.sensing_start))
            if baseline is not None:
                pq = pq.where(Product.baseline.in_([baseline, INDEPENDENT]))
            for p in s.scalars(pq.order_by(Product.sensing_start)):
                w.writerow([p.pdgs_id, p.name, p.product_type, p.level, p.baseline, p.processor_version or "", p.orbit,
                            iso(p.sensing_start), iso(p.sensing_stop), iso(p.publication_date), p.status,
                            p.checksum or "", p.error or ""])
                n += 1
            z.writestr("products.csv", buf.getvalue())
            counts["products"] = n

            refs = [{"name": r.name, "dims": r.dims, "version": r.version, "unit": r.unit, "source": r.source,
                     "axis": json.loads(r.axis), "values": json.loads(r.values)}
                    for r in s.scalars(select(ReferenceData))]
            z.writestr("reference_data.json", json.dumps(refs))
            meta = {
                "generated_utc": iso(utcnow()), "filters": {"start": iso(start), "end": iso(end), "baseline": baseline,
                                                            "codes": codes},
                "schema_revision": current_revision(s.get_bind()), "counts": counts,
                "parameters": [{"code": p.code, "name": p.name, "unit": p.unit, "group": p.group, "kind": p.kind,
                                "dims": p.dims, "algorithm": p.algorithm, "origin": p.origin,
                                "limit": None if p.current_limit is None else {
                                    k: getattr(p.current_limit, k) for k in ("warn_lo", "warn_hi", "alarm_lo",
                                                                             "alarm_hi", "persistence")},
                                "requirement": None if p.current_requirement is None else {
                                    "lo": p.current_requirement.lo, "hi": p.current_requirement.hi,
                                    "text": p.current_requirement.text}} for p in params],
            }
            z.writestr("metadata.json", json.dumps(meta, indent=2))
    finally:
        if isinstance(out, Path):
            target.close()
    return counts


# =============================================================================== API tokens
def create_token(s: Session, name: str, role: str = "expert", author: str = "system") -> str:
    if role not in ("expert", "admin"):
        raise ValueError("role must be expert or admin")
    if s.scalar(select(ApiToken.id).where(ApiToken.name == name)):
        raise ValueError(f"Token {name} already exists")
    token = "imf_" + secrets.token_urlsafe(32)
    s.add(ApiToken(name=name, token_hash=sha256(token.encode()), role=role, created_by=author))
    return token


def verify_token(s: Session, token: str) -> ApiToken | None:
    t = s.scalar(select(ApiToken).where(ApiToken.token_hash == sha256(token.encode()), ApiToken.active.is_(True)))
    if t is not None:
        t.last_used = utcnow()
    return t
