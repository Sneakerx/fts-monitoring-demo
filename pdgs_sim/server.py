"""PDGS simulator - OData-like product catalogue and download service.

Endpoints (subset of the Copernicus/ESA OData conventions):
  GET /odata/v1/Products?$filter=ProductType eq 'FTS_L1B' and PublicationDate gt 2026-01-01T00:00:00.000Z
                         &$orderby=PublicationDate asc&$top=100
  GET /odata/v1/Products({Id})/$value      -> HDF5 product
  GET /status                              -> simulator clock

Simulated time: on first start the mission begins `--days` days in the past, so a backlog of
products is immediately available (backfill). Afterwards the mission clock runs `--accel` times
faster than real time (default: one new orbit product every ~30 s). The clock is persisted, so
it pauses while the simulator is stopped.
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Response

from . import scenario as sc
from .generator import generate_product, product_name

PRODUCT_TYPE = "FTS_L1B"
ID_PREFIX = "5f1b0000-0000-4000-8000-"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


class SimClock:
    def __init__(self, state_file: Path, days: float, accel: float):
        self.state_file = state_file
        self.accel = accel
        self._lock = threading.Lock()
        if state_file.exists():
            st = json.loads(state_file.read_text())
            self.mission_start = _parse(st["mission_start"])
            anchor_sim = _parse(st["sim_now"])
        else:
            anchor_sim = _now().replace(microsecond=0)
            self.mission_start = (anchor_sim - timedelta(days=days)).replace(second=0)
        self.anchor_sim = anchor_sim
        self.anchor_real = time.monotonic()
        self.save()

    def now(self) -> datetime:
        return self.anchor_sim + timedelta(seconds=(time.monotonic() - self.anchor_real) * self.accel)

    def save(self) -> None:
        with self._lock:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(
                {"mission_start": _iso(self.mission_start), "sim_now": _iso(self.now()), "accel": self.accel}))

    # --- catalogue helpers ----------------------------------------------
    def start_of(self, index: int) -> datetime:
        return self.mission_start + timedelta(seconds=index * sc.ORBIT_PERIOD_S)

    @staticmethod
    def latency_s(index: int) -> float:
        # millisecond resolution, consistent with the ISO timestamps exposed in the catalogue
        return round(float(np.random.default_rng(index + 99991).uniform(1.5, 3.0) * 3600.0), 3)

    def publication_of(self, index: int) -> datetime:
        return self.start_of(index) + timedelta(seconds=sc.ORBIT_PERIOD_S + self.latency_s(index))


def product_id(index: int) -> str:
    return f"{ID_PREFIX}{index:012x}"


def index_of(pid: str) -> int:
    if not pid.startswith(ID_PREFIX):
        raise ValueError(pid)
    return int(pid[len(ID_PREFIX):], 16)


def create_app(clock: SimClock) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        clock.save()

    app = FastAPI(title="PDGS Simulator (FTS L1b)", version="1.0", lifespan=lifespan)

    def entry(i: int) -> dict:
        start = clock.start_of(i)
        stop = start + timedelta(seconds=sc.ORBIT_PERIOD_S)
        return {
            "@odata.mediaContentType": "application/x-hdf5",
            "Id": product_id(i),
            "Name": product_name(i, start, stop),
            "ProductType": PRODUCT_TYPE,
            "ContentType": "application/x-hdf5",
            "ContentLength": 95000,
            "OriginDate": _iso(stop),
            "PublicationDate": _iso(clock.publication_of(i)),
            "Online": True,
            "ContentDate": {"Start": _iso(start), "End": _iso(stop)},
            "OrbitNumber": i + 1,
        }

    def published_indices(after: datetime | None, now: datetime) -> list[int]:
        period = sc.ORBIT_PERIOD_S
        # publication = start + period + latency, latency in [1.5 h, 3 h]
        i_max = int(((now - clock.mission_start).total_seconds() - period - 1.5 * 3600) // period)
        if i_max < 0:
            return []
        i_min = 0
        if after is not None:
            i_min = max(0, int(((after - clock.mission_start).total_seconds() - period - 3.0 * 3600) // period))
        out = []
        for i in range(i_min, i_max + 1):
            if sc.is_gap(i):
                continue
            pub = clock.publication_of(i)
            if pub <= now and (after is None or pub > after):
                out.append(i)
        return out

    @app.get("/status")
    def status() -> dict:
        now = clock.now()
        return {"sim_time": _iso(now), "mission_start": _iso(clock.mission_start), "accel": clock.accel,
                "mission_day": (now - clock.mission_start).total_seconds() / 86400.0}

    @app.get("/odata/v1/Products")
    def products(
        filter_: str | None = Query(None, alias="$filter"),
        top: int = Query(100, alias="$top", ge=1, le=1000),
        orderby: str = Query("PublicationDate asc", alias="$orderby"),
    ) -> dict:
        after = None
        if filter_:
            m = re.search(r"ProductType\s+eq\s+'([^']+)'", filter_)
            if m and m.group(1) != PRODUCT_TYPE:
                return {"value": []}
            m = re.search(r"PublicationDate\s+gt\s+([0-9T:\.\-Z+]+)", filter_)
            if m:
                after = _parse(m.group(1))
        now = clock.now()
        idx = published_indices(after, now)
        idx.sort(key=clock.publication_of, reverse=orderby.strip().endswith("desc"))
        return {"@odata.context": "$metadata#Products", "value": [entry(i) for i in idx[:top]]}

    @app.get("/odata/v1/Products({pid})/$value")
    def download(pid: str) -> Response:
        try:
            i = index_of(pid.strip("'"))
        except ValueError:
            raise HTTPException(404, "Unknown product")
        if sc.is_gap(i) or clock.publication_of(i) > clock.now():
            raise HTTPException(404, "Product not available")
        data = generate_product(i, clock.start_of(i))
        name = entry(i)["Name"]
        return Response(data, media_type="application/x-hdf5",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    def _saver() -> None:
        while True:
            time.sleep(10)
            clock.save()

    threading.Thread(target=_saver, daemon=True).start()
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--days", type=float, default=90.0, help="mission history available at first start")
    ap.add_argument("--accel", type=float, default=sc.ORBIT_PERIOD_S / 30.0,
                    help="simulated seconds per real second (default: one orbit per 30 s)")
    ap.add_argument("--state", default="data/pdgs_sim/state.json")
    args = ap.parse_args()
    clock = SimClock(Path(args.state), args.days, args.accel)
    uvicorn.run(create_app(clock), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
