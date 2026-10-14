"""IMF command-line interface (operations / Linux service management).

    python -m imf.cli db migrate | check | repair | backup [--out DIR] | restore FILE --yes
    python -m imf.cli db export --out FILE.zip [--start T] [--end T] [--baseline B] [--codes A,B]
    python -m imf.cli db purge [--baseline B] [--product-type T] [--parameter P] [--start T] [--end T]
                               [--reset-watermarks] [--yes]        (dry run without --yes)
    python -m imf.cli import samples|timeline FILE  |  import reference FILE --name NAME [--unit U]
    python -m imf.cli parameter create CODE NAME [--unit U] [--group G] [--kind scalar|vector] [--dims D]
    python -m imf.cli campaign list | create NAME --baseline B [--type T] [--start T] [--end T] | run ID
    python -m imf.cli token create NAME [--role expert|admin]
    python -m imf.cli poll | analyse

All commands use the configuration in $IMF_CONFIG (default config/imf.yaml).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from sqlalchemy import select

from . import mdb
from .config import load_settings
from .db import Database, seed
from .models import AuditLog, Campaign
from .timeutil import parse_iso


def _db(settings, migrate: bool = True) -> Database:
    db = Database(settings.database.url)
    if migrate:
        db.migrate(settings.pdgs.baseline)
    return db


def _services(settings):
    from .services import Services
    return Services(settings, start_scheduler=False)


def _t(v: str | None):
    return parse_iso(v) if v else None


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="imf", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="configuration file (default: $IMF_CONFIG or config/imf.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    dbp = sub.add_parser("db", help="database management").add_subparsers(dest="action", required=True)
    dbp.add_parser("migrate")
    dbp.add_parser("check")
    dbp.add_parser("repair")
    b = dbp.add_parser("backup")
    b.add_argument("--out", type=Path)
    r = dbp.add_parser("restore")
    r.add_argument("file", type=Path)
    r.add_argument("--yes", action="store_true", help="really replace the database (IMF must be stopped)")
    e = dbp.add_parser("export")
    e.add_argument("--out", type=Path, required=True)
    for p in (e,):
        p.add_argument("--start")
        p.add_argument("--end")
        p.add_argument("--baseline")
        p.add_argument("--codes", default="")
    pg = dbp.add_parser("purge")
    pg.add_argument("--baseline")
    pg.add_argument("--product-type")
    pg.add_argument("--parameter")
    pg.add_argument("--start")
    pg.add_argument("--end")
    pg.add_argument("--reset-watermarks", action="store_true")
    pg.add_argument("--yes", action="store_true")

    imp = sub.add_parser("import", help="import CSV files into the MDB")
    imp.add_argument("what", choices=["samples", "timeline", "reference"])
    imp.add_argument("file", type=Path)
    imp.add_argument("--name")
    imp.add_argument("--unit", default="")
    imp.add_argument("--description", default="")

    par = sub.add_parser("parameter").add_subparsers(dest="action", required=True)
    pc = par.add_parser("create")
    pc.add_argument("code")
    pc.add_argument("name")
    pc.add_argument("--unit", default="")
    pc.add_argument("--group", default="Manual")
    pc.add_argument("--kind", default="scalar", choices=["scalar", "vector"])
    pc.add_argument("--dims", default="")
    pc.add_argument("--visibility", default="restricted", choices=["public", "restricted"])
    pc.add_argument("--description", default="")

    cp = sub.add_parser("campaign", help="reprocessing campaigns").add_subparsers(dest="action", required=True)
    cp.add_parser("list")
    cc = cp.add_parser("create")
    cc.add_argument("name")
    cc.add_argument("--baseline", required=True)
    cc.add_argument("--type", default="FTS_L1B")
    cc.add_argument("--start")
    cc.add_argument("--end")
    cr = cp.add_parser("run")
    cr.add_argument("id", type=int)

    tk = sub.add_parser("token").add_subparsers(dest="action", required=True)
    tc = tk.add_parser("create")
    tc.add_argument("name")
    tc.add_argument("--role", default="expert", choices=["expert", "admin"])

    sub.add_parser("poll", help="run one polling cycle")
    sub.add_parser("analyse", help="run windowed analyses and trend rules")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    settings = load_settings(args.config)
    user = "cli"

    if args.cmd == "db":
        if args.action == "restore":
            if not args.yes:
                print("Refusing to restore without --yes (stop the IMF first; the current database is replaced).")
                return 2
            mdb.restore(settings.database.url, args.file)
            _db(settings)  # migrate the restored database to the current schema if needed
            print(f"Restored {args.file}")
            return 0
        db = _db(settings)
        if args.action == "migrate":
            seed(db, settings)
            print("Database is at the latest schema revision")
        elif args.action == "check":
            res = mdb.check(db)
            _print(res)
            return 0 if res["ok"] else 1
        elif args.action == "repair":
            _print(mdb.repair(db))
        elif args.action == "backup":
            print(mdb.backup(db, args.out or settings.processing.backup_dir))
        elif args.action == "export":
            with db.session() as s:
                counts = mdb.export_zip(s, args.out, start=_t(args.start), end=_t(args.end), baseline=args.baseline,
                                        codes=[c for c in args.codes.split(",") if c] or None)
                s.add(AuditLog(username=user, action="mdb.export", target=str(args.out), details=json.dumps(counts)))
            _print({"file": str(args.out), **counts})
        elif args.action == "purge":
            f = mdb.PurgeFilter(baseline=args.baseline, product_type=args.product_type, parameter=args.parameter,
                                start=_t(args.start), end=_t(args.end))
            with db.session() as s:
                counts = mdb.purge(s, f, dry_run=not args.yes, reset_watermarks=args.reset_watermarks)
                if args.yes:
                    s.add(AuditLog(username=user, action="mdb.purge", target=f.describe(), details=json.dumps(counts)))
            _print({"filter": f.describe(), "dry_run": not args.yes, **counts})
        return 0

    if args.cmd == "import":
        db = _db(settings)
        data = args.file.read_bytes()
        with db.session() as s:
            if args.what == "samples":
                res = mdb.import_samples_csv(s, data, args.file.name, user)
                msg = res.summary()
            elif args.what == "timeline":
                res = mdb.import_timeline_csv(s, data, args.file.name, user)
                msg = res.summary()
            else:
                if not args.name:
                    print("--name is required for reference data")
                    return 2
                refs = mdb.import_reference_csv(s, data, args.name.upper(), unit=args.unit,
                                                description=args.description, author=user, filename=args.file.name)
                msg = f"{len(refs)} record(s), version {refs[0].version}"
            s.add(AuditLog(username=user, action=f"mdb.import.{args.what}", target=args.file.name, details=msg))
        print(msg)
        return 0

    if args.cmd == "parameter":
        db = _db(settings)
        with db.session() as s:
            p = mdb.create_parameter(s, args.code, args.name, unit=args.unit, group=args.group, kind=args.kind,
                                     dims=args.dims, visibility=args.visibility, description=args.description)
            s.add(AuditLog(username=user, action="parameter.create", target=p.code))
        print(f"Parameter {args.code.upper()} created")
        return 0

    if args.cmd == "token":
        db = _db(settings)
        with db.session() as s:
            token = mdb.create_token(s, args.name, args.role, user)
            s.add(AuditLog(username=user, action="token.create", target=args.name, details=f"role={args.role}"))
        print(token)
        return 0

    if args.cmd == "campaign":
        if args.action == "list":
            db = _db(settings)
            with db.session() as s:
                _print([{k: getattr(c, k) for k in ("id", "name", "baseline", "product_type", "status", "n_found",
                                                    "n_processed", "n_failed", "started_at", "finished_at")}
                        for c in s.scalars(select(Campaign).order_by(Campaign.id))])
            return 0
        svc = _services(settings)
        try:
            if args.action == "create":
                cid = svc.campaigns.create(args.name, args.baseline, args.type, _t(args.start), _t(args.end), user)
                print(f"Campaign {cid} created - run it with: python -m imf.cli campaign run {cid}")
            else:
                summary = svc.campaigns.run(args.id)
                print(summary)
                svc.analyses.run_once()
        finally:
            svc.stop()
        return 0

    if args.cmd in ("poll", "analyse"):
        svc = _services(settings)
        try:
            print(svc.poller.run_once(trigger="cli") if args.cmd == "poll" else svc.analyses.run_once())
        finally:
            svc.stop()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
