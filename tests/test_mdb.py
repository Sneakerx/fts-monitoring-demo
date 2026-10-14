"""MDB management: imports / manual population, purge, export, backup / restore, check / repair,
reprocessing campaigns and schema migrations."""
import io
import json
import sqlite3
import zipfile
from datetime import timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, select, text

from imf import mdb
from imf.db import Database
from imf.migrate import alembic_config, upgrade
from imf.models import Base, Event, Parameter, ParameterState, Product, Sample, TimelineEntry


def _code_id(s, code):
    return s.scalar(select(Parameter.id).where(Parameter.code == code))


def test_import_samples_timeline_reference(small_services):
    db = small_services.db
    with db.session() as s:
        mdb.create_parameter(s, "MISSION_PHASE", "Mission phase", group="Manual")
        res = mdb.import_samples_csv(s, "time,code,value\n2026-01-01T00:00:00Z,MISSION_PHASE,1\n"
                                        "2026-01-02T00:00:00Z,MISSION_PHASE,2\nbad,MISSION_PHASE,3\n"
                                        "2026-01-02T00:00:00Z,UNKNOWN,1\n", "phases.csv", "tester")
        assert res.n == 2 and len(res.errors) == 2
        smp = s.scalar(select(Sample).where(Sample.parameter_id == _code_id(s, "MISSION_PHASE")))
        assert smp.entry_type == "imported" and smp.source.startswith("file:phases.csv#") and smp.product_id is None
        res = mdb.import_timeline_csv(s, "type,title,start,stop\nOPERATIONS,Orbit manoeuvre,2026-01-01T10:00,"
                                         "2026-01-01T11:00\n", "tl.csv", "tester")
        assert res.n == 1 and s.scalar(select(TimelineEntry).where(TimelineEntry.title == "Orbit manoeuvre")).source == "imported"
        refs = mdb.import_reference_csv(s, "band,wavenumber,value\nB1,700,30\nB1,800,31\n", "NESR_TEMPLATE",
                                        filename="t2.csv")
        assert refs[0].version == 2  # version 1 imported at start-up from config/reference
        assert mdb.reference(s, "NESR_TEMPLATE", "band=B1").version == 2


def test_manual_sample_is_limit_checked(small_services):
    with small_services.db.session() as s:
        p = s.scalar(select(Parameter).where(Parameter.code == "T_DETECTOR"))
        last = s.scalar(select(func.max(Sample.time)).where(Sample.parameter_id == p.id))
        events = []
        for i in range(2):  # persistence 2
            events += mdb.add_sample(s, p, last + timedelta(hours=i + 1), 95.0, source="user:test")
        assert events and events[0].severity == "ALARM"
        # a historic value does not disturb the running state
        mdb.add_sample(s, p, last - timedelta(days=1), 95.0)
        st = s.scalar(select(ParameterState).where(ParameterState.parameter_id == p.id))
        assert st.last_value == 95.0 and st.last_time == last + timedelta(hours=2)


def test_export_zip(small_services):
    buf = io.BytesIO()
    with small_services.db.session() as s:
        counts = mdb.export_zip(s, buf, baseline="B03", codes=["SSD", "NESR_SPECTRAL", "L1_COMPLETENESS"])
    z = zipfile.ZipFile(buf)
    meta = json.loads(z.read("metadata.json"))
    assert counts["samples"] > 50 and counts["vector_samples"] > 50
    assert {p["code"] for p in meta["parameters"]} == {"SSD", "NESR_SPECTRAL", "L1_COMPLETENESS"}
    header = z.read("samples.csv").decode().splitlines()[0]
    assert "entry_type" in header and "source" in header and "entry_time_utc" in header


def test_purge_dry_run_then_real(small_services):
    db = small_services.db
    with db.session() as s:
        first = s.scalar(select(func.min(Product.sensing_start)))
    f = mdb.PurgeFilter(product_type="FTS_L1B", start=first, end=first + timedelta(days=1))
    with db.session() as s:
        n_before = s.scalar(select(func.count(Sample.id)))
        preview = mdb.purge(s, f, dry_run=True)
        assert preview["products"] > 10 and preview["samples"] > 100
        assert s.scalar(select(func.count(Sample.id))) == n_before
    with db.session() as s:
        done = mdb.purge(s, f, dry_run=False, reset_watermarks=True)
    assert done["products"] == preview["products"] and done["samples"] == preview["samples"]
    with db.session() as s:
        assert s.scalar(select(func.count(Sample.id))) == n_before - preview["samples"]
        assert s.scalar(select(func.count(Product.id)).where(*([Product.product_type == "FTS_L1B",
                                                                Product.sensing_start < first + timedelta(days=1)]))) == 0
    assert mdb.check(db)["ok"]
    # re-ingestion of the purged period (watermark reset)
    again = small_services.poller.run_once()
    assert again.processed == preview["products"]


def test_backup_restore_check_repair(small_services, tmp_path):
    db, settings = small_services.db, small_services.settings
    assert mdb.check(db)["ok"]
    backup = mdb.backup(db, settings.processing.backup_dir)
    assert json.loads(backup.with_suffix(".db.json").read_text())["schema_revision"] == "0002"
    with db.session() as s:
        n = s.scalar(select(func.count(Sample.id)))
        s.execute(text("DELETE FROM parameter_state"))  # simulate a damaged running state
    res = mdb.check(db)
    assert not res["ok"] and "series_without_state" in res["problems"]
    rep = mdb.repair(db)
    assert rep["states_rebuilt"] > 50 and mdb.check(db)["ok"]

    target = tmp_path / "restored.db"
    target.write_bytes(b"")
    mdb.restore(f"sqlite:///{target.as_posix()}", backup)
    con = sqlite3.connect(target)
    assert con.execute("select count(*) from samples").fetchone()[0] == n
    con.close()
    backup.write_bytes(backup.read_bytes()[:-10] + b"corrupted!")
    with pytest.raises(ValueError):
        mdb.restore(f"sqlite:///{target.as_posix()}", backup)


def test_reprocessing_campaign(small_services):
    svc = small_services
    with svc.db.session() as s:
        first = s.scalar(select(func.min(Product.sensing_start)))
    cid = svc.campaigns.create("REPRO-B04", "B04", start=first, end=first + timedelta(days=1))
    summary = svc.campaigns.run(cid)
    assert summary.error is None and summary.processed > 10 and summary.skipped > 0
    with svc.db.session() as s:
        b04 = s.scalars(select(Product).where(Product.baseline == "B04")).all()
        assert b04 and all(p.campaign_id == cid and p.processor_version == "04.00" for p in b04)
        smp = s.scalar(select(Sample).where(Sample.baseline == "B04"))
        assert smp.entry_type == "reprocessed"
        # B04 removes the constant spectral calibration offset (+0.6 ppm)
        p = _code_id(s, "SPEC_SHIFT")
        mean = lambda bl: s.scalar(select(func.avg(Sample.value)).join(Product).where(  # noqa: E731
            Sample.parameter_id == p, Product.baseline == bl, Product.sensing_start < first + timedelta(days=1)))
        assert mean("B03") - mean("B04") == pytest.approx(0.6, abs=0.1)
        states = {st.baseline for st in s.scalars(select(ParameterState).where(ParameterState.parameter_id == p))}
        assert states == {"B03", "B04"}
        assert not s.scalar(select(func.count(Event.id)).where(Event.baseline == "B04", Event.category == "action"))
    svc.analyses.run_once()
    with svc.db.session() as s:
        assert s.scalar(select(func.count(Sample.id)).where(Sample.parameter_id == _code_id(s, "L1_COMPLETENESS"),
                                                            Sample.baseline == "B04")) >= 1


def test_migrations_match_models_and_upgrade_prototype_db(tmp_path):
    """Fresh databases and prototype databases (created without Alembic) end up with the model schema."""
    from alembic import command
    for label in ("fresh", "prototype"):
        db = Database(f"sqlite:///{(tmp_path / f'{label}.db').as_posix()}")
        if label == "prototype":
            cfg = alembic_config(str(db.engine.url))
            with db.engine.begin() as conn:
                cfg.attributes["connection"] = conn
                command.upgrade(cfg, "0001")
                conn.execute(text("DROP TABLE alembic_version"))
                conn.execute(text("INSERT INTO parameters VALUES (1,'X','X','u','G','a','','public',0)"))
                conn.execute(text("INSERT INTO products VALUES (1,'p1','n','FTS_L1B',1,'2026-01-01','2026-01-01',"
                                  "'2026-01-01',1,'2026-01-01',1.0,'PROCESSED',NULL)"))
                conn.execute(text("INSERT INTO samples VALUES (1,1,1,'2026-01-01',1.5,1)"))
                conn.execute(text("INSERT INTO parameter_state VALUES (1,1,1,2,1.5,'2026-01-01')"))
                conn.execute(text("INSERT INTO system_state VALUES ('poll.watermark','2026-01-01T00:00:00.000Z')"))
        upgrade(db, "B03")
        with db.engine.connect() as conn:
            assert compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata) == []
            if label == "prototype":
                assert conn.execute(text("SELECT baseline, dims, entry_type FROM samples")).one() == ("B03", "", "derived")
                assert conn.execute(text("SELECT baseline, series, level FROM parameter_state")).one() == ("B03", "", 1)
                assert conn.execute(text("SELECT key FROM system_state")).scalar() == "poll.watermark.FTS_L1B"
        db.engine.dispose()
