"""PDGS simulator - OData-like product catalogue and download service.

Endpoints (subset of the Copernicus/ESA OData conventions):
  GET /odata/v1/Products?$filter=ProductType eq 'FTS_L1B' and PublicationDate gt 2026-01-01T00:00:00.000Z
                         [and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'baseline'
                              and att/OData.CSC.StringAttribute/Value eq 'B03')]
                         &$orderby=PublicationDate asc&$top=100&$expand=Attributes
  GET /odata/v1/Products({Id})/$value      -> HDF5 product (L1B, AUX_ANC); L0 / AUX_EFSI are header-only
  GET /status                              -> simulator clock

Product types: FTS_L0_BB / FTS_L0_DS / FTS_L0_EO (L0 views, header information as catalogue attributes),
AUX_ANC (ancillary data per orbit), AUX_EFSI (special calibration mode), FTS_L1B in the nominal baseline
B03 and the reprocessing baseline B04.

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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Response

from . import scenario as sc
from .generator import ancillary_name, generate_ancillary, generate_product, product_name


@dataclass(frozen=True)
class TypeSpec:
    code: int                       # id prefix
    latency_h: tuple[float, float]  # publication latency after sensing stop
    downloadable: bool
    level: str


TYPES = {
    "FTS_L1B": TypeSpec(0x5F1B, (1.5, 3.0), True, "L1"),
    "FTS_L0_BB": TypeSpec(0x5F0B, (0.3, 0.8), False, "L0"),
    "FTS_L0_DS": TypeSpec(0x5F0D, (0.3, 0.8), False, "L0"),
    "FTS_L0_EO": TypeSpec(0x5F0E, (0.3, 0.8), False, "L0"),
    "AUX_ANC": TypeSpec(0x0A0C, (0.5, 1.2), True, "AUX"),
    "AUX_EFSI": TypeSpec(0x0A0E, (1.0, 1.0), False, "AUX"),
}
BASELINE_CODE = {"B03": 0, "B04": 4}
REPRO_EXTRA_LATENCY_H = 1.5  # B04 products are published after the nominal ones
PRODUCT_TYPE = "FTS_L1B"


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
    def latency_s(index: int, ptype: str = PRODUCT_TYPE, baseline: str = "B03") -> float:
        lo, hi = TYPES[ptype].latency_h
        salt = TYPES[ptype].code if ptype != PRODUCT_TYPE else 0  # keeps the original L1 latencies
        u = float(np.random.default_rng(index + 99991 + salt).uniform(lo, hi))
        extra = REPRO_EXTRA_LATENCY_H if baseline == "B04" else 0.0
        # millisecond resolution, consistent with the ISO timestamps exposed in the catalogue
        return round((u + extra) * 3600.0, 3)

    def publication_of(self, index: int, ptype: str = PRODUCT_TYPE, baseline: str = "B03") -> datetime:
        return self.start_of(index) + timedelta(seconds=sc.ORBIT_PERIOD_S + self.latency_s(index, ptype, baseline))

    # --- special calibration mode (AUX_EFSI): one product per calibration -----
    def efsi_window(self, k: int) -> tuple[datetime, datetime]:
        start = self.mission_start + timedelta(days=sc.EFSI_DAYS[k])
        return start, start + timedelta(days=sc.EFSI_DURATION_D)

    def efsi_publication(self, k: int) -> datetime:
        return self.efsi_window(k)[1] + timedelta(hours=TYPES["AUX_EFSI"].latency_h[0])


def product_id(index: int, ptype: str = PRODUCT_TYPE, baseline: str = "B03") -> str:
    bl = BASELINE_CODE.get(baseline, 0) if ptype == PRODUCT_TYPE else 0
    return f"{TYPES[ptype].code:04x}{bl:04x}-0000-4000-8000-{index:012x}"


def parse_id(pid: str) -> tuple[str, str, int]:
    m = re.fullmatch(r"([0-9a-f]{4})([0-9a-f]{4})-0000-4000-8000-([0-9a-f]{12})", pid)
    if not m:
        raise ValueError(pid)
    code, bl, index = int(m.group(1), 16), int(m.group(2), 16), int(m.group(3), 16)
    ptype = next((t for t, spec in TYPES.items() if spec.code == code), None)
    baseline = next((b for b, c in BASELINE_CODE.items() if c == bl), None)
    if ptype is None or baseline is None:
        raise ValueError(pid)
    return ptype, baseline, index


def index_of(pid: str) -> int:
    return parse_id(pid)[2]


def _attributes(values: dict) -> list[dict]:
    out = []
    for k, v in values.items():
        vt = "Integer" if isinstance(v, int) else "Double" if isinstance(v, float) else "String"
        out.append({"@odata.type": f"#OData.CSC.{vt}Attribute", "Name": k, "Value": v, "ValueType": vt})
    return out


def create_app(clock: SimClock) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        clock.save()

    app = FastAPI(title="PDGS Simulator (FTS L0 / L1b / AUX)", version="2.0", lifespan=lifespan)

    def entry(ptype: str, baseline: str, i: int) -> dict:
        if ptype == "AUX_EFSI":
            start, stop = clock.efsi_window(i)
            name = f"FTS1_AUX_EFSI_{start:%Y%m%dT%H%M%S}_{stop:%Y%m%dT%H%M%S}_C{i + 1:03d}.h5"
            pub, orbit = clock.efsi_publication(i), None
            attrs = {"productType": ptype, "calibrationMode": "EFSI", "numberOfSequences": 12,
                     "calibrationId": i + 1}
        else:
            start = clock.start_of(i)
            stop = start + timedelta(seconds=sc.ORBIT_PERIOD_S)
            pub, orbit = clock.publication_of(i, ptype, baseline), i + 1
            attrs = {"productType": ptype, "orbitNumber": i + 1}
            if ptype == PRODUCT_TYPE:
                name = product_name(i, start, stop, baseline)
                attrs |= {"baseline": baseline, "processorVersion": sc.BASELINES[baseline], "productVersion": "01"}
            elif ptype == "AUX_ANC":
                name = ancillary_name(i, start, stop)
            else:
                view = sc.L0_TYPES[ptype]
                name = f"FTS1_L0_{view}_{start:%Y%m%dT%H%M%S}_{stop:%Y%m%dT%H%M%S}_O{i + 1:05d}.dat"
                attrs |= sc.l0_header(ptype, i)
        downloadable = TYPES[ptype].downloadable
        out = {
            "@odata.mediaContentType": "application/x-hdf5" if downloadable else "application/octet-stream",
            "Id": product_id(i, ptype, baseline),
            "Name": name,
            "ProductType": ptype,
            "ContentType": "application/x-hdf5" if downloadable else "application/octet-stream",
            "ContentLength": {"FTS_L1B": 160000, "AUX_ANC": 4000}.get(ptype, 25_000_000),
            "OriginDate": _iso(stop),
            "PublicationDate": _iso(pub),
            "Online": True,
            "ContentDate": {"Start": _iso(start), "End": _iso(stop)},
            "Attributes": _attributes(attrs),
        }
        if orbit is not None:
            out["OrbitNumber"] = orbit
        return out

    def exists(ptype: str, baseline: str, i: int) -> bool:
        if ptype == "AUX_EFSI":
            return 0 <= i < len(sc.EFSI_DAYS)
        if sc.is_gap(i):
            return False
        return not (ptype == PRODUCT_TYPE and sc.is_l1_gap(i, baseline))

    def published(ptype: str, baseline: str, after: datetime | None, now: datetime) -> list[int]:
        if ptype == "AUX_EFSI":
            return [k for k in range(len(sc.EFSI_DAYS))
                    if clock.efsi_publication(k) <= now and (after is None or clock.efsi_publication(k) > after)]
        period = sc.ORBIT_PERIOD_S
        lo, hi = TYPES[ptype].latency_h
        extra = REPRO_EXTRA_LATENCY_H if (ptype == PRODUCT_TYPE and baseline == "B04") else 0.0
        lo, hi = (lo + extra) * 3600, (hi + extra) * 3600
        i_max = int(((now - clock.mission_start).total_seconds() - period - lo) // period)
        if i_max < 0:
            return []
        i_min = 0
        if after is not None:
            i_min = max(0, int(((after - clock.mission_start).total_seconds() - period - hi) // period))
        out = []
        for i in range(i_min, i_max + 1):
            if not exists(ptype, baseline, i):
                continue
            pub = clock.publication_of(i, ptype, baseline)
            if pub <= now and (after is None or pub > after):
                out.append(i)
        return out

    @app.get("/status")
    def status() -> dict:
        now = clock.now()
        return {"sim_time": _iso(now), "mission_start": _iso(clock.mission_start), "accel": clock.accel,
                "mission_day": (now - clock.mission_start).total_seconds() / 86400.0,
                "product_types": list(TYPES), "baselines": list(BASELINE_CODE)}

    @app.get("/odata/v1/Products")
    def products(
        filter_: str | None = Query(None, alias="$filter"),
        top: int = Query(100, alias="$top", ge=1, le=1000),
        orderby: str = Query("PublicationDate asc", alias="$orderby"),
        expand: str | None = Query(None, alias="$expand"),
    ) -> dict:
        ptype, after, baselines = PRODUCT_TYPE, None, list(BASELINE_CODE)
        if filter_:
            m = re.search(r"ProductType\s+eq\s+'([^']+)'", filter_)
            if m:
                ptype = m.group(1)
            m = re.search(r"PublicationDate\s+gt\s+([0-9T:\.\-Z+]+)", filter_)
            if m:
                after = _parse(m.group(1))
            m = re.search(r"Name eq 'baseline' and att/OData\.CSC\.StringAttribute/Value eq '([^']+)'", filter_)
            if m:
                baselines = [m.group(1)]
        if ptype not in TYPES:
            return {"value": []}
        if ptype != PRODUCT_TYPE:
            baselines = ["B03"]
        now = clock.now()
        items = [(b, i) for b in baselines if b in BASELINE_CODE for i in published(ptype, b, after, now)]
        pub = (lambda bi: clock.efsi_publication(bi[1])) if ptype == "AUX_EFSI" else \
            (lambda bi: clock.publication_of(bi[1], ptype, bi[0]))
        items.sort(key=pub, reverse=orderby.strip().endswith("desc"))
        values = [entry(ptype, b, i) for b, i in items[:top]]
        if not (expand and "Attributes" in expand):
            for v in values:
                v.pop("Attributes")
        return {"@odata.context": "$metadata#Products", "value": values}

    @app.get("/odata/v1/Products({pid})/$value")
    def download(pid: str) -> Response:
        try:
            ptype, baseline, i = parse_id(pid.strip("'"))
        except ValueError:
            raise HTTPException(404, "Unknown product")
        if not TYPES[ptype].downloadable:
            raise HTTPException(404, "Header-only product: download not simulated")
        if not exists(ptype, baseline, i) or clock.publication_of(i, ptype, baseline) > clock.now():
            raise HTTPException(404, "Product not available")
        if ptype == PRODUCT_TYPE:
            data = generate_product(i, clock.start_of(i), baseline)
        else:
            data = generate_ancillary(i, clock.start_of(i))
        name = entry(ptype, baseline, i)["Name"]
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
